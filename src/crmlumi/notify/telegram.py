"""Telegram-бот: кнопки, роли, приём заказов, claim фотографов, меню хоста.

Формат сообщений и клавиатур — чистые функции (часть живёт в menus.py),
тестируются без сети. aiogram-хендлеры — тонкая обвязка поверх них и
поверх Database; их проводка проверяется через Dispatcher с фейковой
сессией (tests/notify/test_bot_flows.py).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..db import Database
from ..host import RuntimeState, current_commit, format_status, tail_text, update_from_git
from ..config import ReminderSettings
from ..models import Order, Shoot
from ..pipeline.new_order import build_new_order_row, next_order_id
from ..pipeline.stages import STAGE_LINKS, stage_links
from ..roles import ROLE_NEW_LEADS, role_names
from ..service import err_log_path
from ..setup_wizard import write_invite_link
from ..sheet_source import SheetClient
from ..timefmt import format_when
from ..todo.graph_client import GraphTodoClient
from ..todo.oauth import DeviceCodeFlow
from .access import can_add_order, inline_menu_for, reply_keyboard_for
from .menus import (
    BTN_CANCEL,
    BTN_HELP,
    BTN_HOST,
    BTN_NEW_ORDER,
    BTN_ORDERS,
    BTN_ROLES,
    BTN_SETTINGS,
    BTN_TODO,
    back_to_menu_keyboard,
    bot_commands,
    cancel_reply_keyboard,
    device_login_keyboard,
    device_login_message,
    help_text,
    host_menu_keyboard,
    invite_links_editor_keyboard,
    todo_links_keyboard,
)
from .messaging import edit_message
from .order_card import open_order_keyboard
from .orders_ui import build_orders_router, send_orders_home
from .settings_ui import build_settings_router, settings_view
from .shoots_ui import build_shoots_router

__all__ = ["can_add_order"]

log = logging.getLogger(__name__)

WELCOME_OWNER = (
    "Привет! Ты владелец CRM Lumi.\n\n"
    "Всё управление — кнопками внизу 👇\n"
    f"{BTN_ROLES} — кто за какой этап отвечает.\n"
    f"{BTN_HOST} — статус, обновление и перезапуск бота."
)
WELCOME_MEMBER = (
    "Привет! Я бот CRM Lumi.\n\n"
    "Ты зарегистрирован — владелец назначит тебе роль, и начнут приходить уведомления.\n"
    "Всё управление — кнопками внизу 👇"
)

OWNER_ONLY = "Это только для владельца."
NOT_ALLOWED_TO_ADD_ORDER = "Заявки может добавлять владелец или тот, кто в роли «Приём заявок»."
HOST_MENU_TEXT = "🖥 Меню хоста"
TODO_LINKS_TEXT = "Списки задач в Microsoft To Do — нажми на список, чтобы присоединиться:"
INVITE_EDITOR_TEXT = (
    "Ссылки-приглашения на списки To Do.\n"
    "В приложении To Do: открой список → «Поделиться» → создай ссылку-приглашение → "
    "скопируй её и пришли сюда.\n\n"
    "Выбери список:"
)

# Фоновые задачи (ожидание входа в Microsoft) — держим ссылки, иначе их может собрать GC.
_background_tasks: set[asyncio.Task] = set()


def _run_in_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class NewOrderStates(StatesGroup):
    school = State()
    contact_name = State()
    contact_phone = State()
    people_count = State()


class InviteLinkStates(StatesGroup):
    link = State()


def format_new_lead_message(order: Order) -> str:
    lines = [
        "Новый лид",
        f"Школа/класс: {order.school}",
        f"Контакт: {order.contact_name}, {order.contact_phone}",
    ]
    if order.people_count:
        lines.append(f"Кол-во человек: {order.people_count}")
    return "\n".join(lines)


def roles_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"role:{i}")]
        for i, name in enumerate(role_names())
    ]
    buttons.append([InlineKeyboardButton(text="← Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _display(user: dict) -> str:
    if user["display_name"]:
        return user["display_name"]
    if user["username"]:
        return f"@{user['username']}"
    return str(user["chat_id"])


def role_status_text(role_name: str, assignees: list[dict]) -> str:
    if not assignees:
        return f"Роль «{role_name}»: сейчас никто не назначен."
    names = ", ".join(_display(u) for u in assignees)
    return f"Роль «{role_name}»: {names}"


def user_picker_keyboard(
    role_index: int, users: list[dict], current_chat_ids: set[int]
) -> InlineKeyboardMarkup:
    buttons = []
    for user in users:
        mark = "✅ " if user["chat_id"] in current_chat_ids else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{_display(user)}",
                    callback_data=f"assign:{role_index}:{user['chat_id']}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="— никто —", callback_data=f"clear:{role_index}")]
    )
    buttons.append([InlineKeyboardButton(text="← Роли", callback_data="menu:roles")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def todo_screen(
    invite_links: dict[str, str], ms_client_id: str, *, is_owner: bool
) -> tuple[str, InlineKeyboardMarkup]:
    if invite_links:
        return TODO_LINKS_TEXT, todo_links_keyboard(
            invite_links, can_link_account=bool(ms_client_id)
        )
    if is_owner:
        return (
            "Ссылок на списки пока нет — добавь их, и участники смогут вступать одной кнопкой.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔗 Добавить ссылки", callback_data="host:invites")],
                    [InlineKeyboardButton(text="← Меню", callback_data="menu:main")],
                ]
            ),
        )
    return "Владелец ещё не добавил ссылки на списки To Do.", back_to_menu_keyboard()


async def set_chat_commands(bot: Bot, db: Database, chat_id: int, owner_chat_id: int) -> None:
    """Меню «/» конкретного человека — только то, что ему доступно."""
    commands = bot_commands(
        is_owner=chat_id == owner_chat_id,
        can_add_order=await can_add_order(db, chat_id, owner_chat_id),
    )
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception:
        log.exception("Не удалось выставить меню команд для чата %s", chat_id)


async def sync_bot_commands(bot: Bot, db: Database, owner_chat_id: int) -> None:
    try:
        await bot.set_my_commands(
            bot_commands(is_owner=False, can_add_order=False), scope=BotCommandScopeDefault()
        )
    except Exception:
        log.exception("Не удалось выставить меню команд")
        return
    chat_ids = {user["chat_id"] for user in await db.list_users()} | {owner_chat_id}
    for chat_id in sorted(chat_ids):
        await set_chat_commands(bot, db, chat_id, owner_chat_id)


async def notify_role_change(
    bot: Bot, db: Database, owner_chat_id: int, chat_id: int, role_name: str, *, added: bool
) -> None:
    """Человек сразу узнаёт о новой роли и получает обновлённые кнопки."""
    text = (
        f"Тебе назначена роль «{role_name}» — уведомления по этому этапу будут приходить сюда."
        if added
        else f"Тебя сняли с роли «{role_name}»."
    )
    try:
        await bot.send_message(
            chat_id, text, reply_markup=await reply_keyboard_for(db, chat_id, owner_chat_id)
        )
    except Exception:
        log.exception("Не удалось сообщить чату %s об изменении роли", chat_id)
    await set_chat_commands(bot, db, chat_id, owner_chat_id)


async def apply_role_changes(
    bot: Bot,
    db: Database,
    owner_chat_id: int,
    role_name: str,
    before: set[int],
    after: set[int],
) -> None:
    for chat_id in sorted(after - before):
        if chat_id != owner_chat_id:
            await notify_role_change(bot, db, owner_chat_id, chat_id, role_name, added=True)
    for chat_id in sorted(before - after):
        if chat_id != owner_chat_id:
            await notify_role_change(bot, db, owner_chat_id, chat_id, role_name, added=False)


async def notify_owner_new_member(bot: Bot, owner_chat_id: int, name: str) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👥 Назначить роль", callback_data="menu:roles")]]
    )
    try:
        await bot.send_message(
            owner_chat_id, f"В бота пришёл новый участник: {name}.", reply_markup=keyboard
        )
    except Exception:
        log.exception("Не удалось сообщить владельцу о новом участнике")


async def notify_startup(bot: Bot, db: Database, owner_chat_id: int, commit: str | None) -> None:
    try:
        await bot.send_message(
            owner_chat_id,
            f"✅ CRM Lumi запущен (версия {commit or '?'})",
            reply_markup=await reply_keyboard_for(db, owner_chat_id, owner_chat_id),
        )
    except Exception:
        log.exception("Не удалось сообщить владельцу о запуске")


async def broadcast_menu(
    bot: Bot,
    db: Database,
    owner_chat_id: int,
    invite_links: dict[str, str],
    ms_client_id: str,
) -> int:
    """Всем зарегистрированным — свежие кнопки и ссылки на списки To Do."""
    sent = 0
    for user in await db.list_users():
        chat_id = user["chat_id"]
        is_owner = chat_id == owner_chat_id
        try:
            await bot.send_message(
                chat_id,
                "Меню бота обновлено — всё кнопками внизу 👇",
                reply_markup=await reply_keyboard_for(db, chat_id, owner_chat_id),
            )
            if invite_links and not is_owner:
                text, keyboard = todo_screen(invite_links, ms_client_id, is_owner=False)
                await bot.send_message(chat_id, text, reply_markup=keyboard)
            sent += 1
        except Exception:
            log.exception("Не удалось разослать меню в чат %s", chat_id)
        await set_chat_commands(bot, db, chat_id, owner_chat_id)
    return sent


async def start_microsoft_link(send, db: Database, chat_id: int, ms_client_id: str) -> None:
    if not ms_client_id:
        await send("Интеграция с Microsoft ещё не настроена.")
        return

    flow = DeviceCodeFlow(ms_client_id)
    try:
        info = await asyncio.to_thread(flow.start)
    except Exception as exc:
        await send(f"Не получилось начать вход: {exc}")
        return
    await send(
        device_login_message(info.user_code),
        reply_markup=device_login_keyboard(info.verification_uri),
        parse_mode="HTML",
    )

    async def wait_and_save() -> None:
        try:
            result = await asyncio.to_thread(flow.wait_for_result)
            graph = GraphTodoClient(lambda: result["access_token"])
            me = await asyncio.to_thread(graph.get_me)
        except Exception as exc:
            await send(f"Вход не получился: {exc}\nПопробуй ещё раз: {BTN_TODO} → «Привязать аккаунт».")
            return
        await db.save_ms_account(chat_id, me["id"], me.get("mail"))
        await send("✅ Аккаунт Microsoft привязан.")

    _run_in_background(wait_and_save())


def _clip(text: str, limit: int = 3000) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def build_router() -> Router:
    router = Router()

    # Порядок регистрации важен: отмена, команды и кнопки главного меню идут
    # раньше обработчиков диалогов, иначе нажатие кнопки посреди диалога
    # было бы принято за ответ на вопрос.

    @router.message(Command("cancel", "отмена"))
    @router.message(F.text == BTN_CANCEL)
    async def on_cancel(
        message: Message, db: Database, owner_chat_id: int, state: FSMContext
    ) -> None:
        had_dialog = await state.get_state() is not None
        await state.clear()
        await message.answer(
            "Отменено." if had_dialog else "Нечего отменять.",
            reply_markup=await reply_keyboard_for(db, message.chat.id, owner_chat_id),
        )

    @router.message(CommandStart())
    async def on_start(
        message: Message,
        db: Database,
        owner_chat_id: int,
        state: FSMContext,
        invite_links: dict[str, str],
        ms_client_id: str,
    ) -> None:
        await state.clear()
        chat_id = message.chat.id
        user = message.from_user
        name = user.full_name if user else str(chat_id)
        is_new = await db.register_user(
            chat_id, username=user.username if user else None, display_name=name
        )
        is_owner = chat_id == owner_chat_id
        await message.answer(
            WELCOME_OWNER if is_owner else WELCOME_MEMBER,
            reply_markup=await reply_keyboard_for(db, chat_id, owner_chat_id),
        )
        await set_chat_commands(message.bot, db, chat_id, owner_chat_id)
        if is_new and not is_owner:
            if invite_links:
                text, keyboard = todo_screen(invite_links, ms_client_id, is_owner=False)
                await message.answer(text, reply_markup=keyboard)
            await notify_owner_new_member(message.bot, owner_chat_id, name)

    @router.message(Command("menu", "меню"))
    async def on_menu(
        message: Message, db: Database, owner_chat_id: int, state: FSMContext
    ) -> None:
        await state.clear()
        await message.answer(
            "Главное меню:", reply_markup=await inline_menu_for(db, message.chat.id, owner_chat_id)
        )

    @router.callback_query(F.data == "menu:main")
    async def on_menu_main(
        callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext
    ) -> None:
        await state.clear()
        await edit_message(
            callback,
            "Главное меню:",
            await inline_menu_for(db, callback.from_user.id, owner_chat_id),
        )
        await callback.answer()

    @router.message(Command("help", "помощь"))
    @router.message(F.text == BTN_HELP)
    async def on_help(
        message: Message, db: Database, owner_chat_id: int, state: FSMContext
    ) -> None:
        await state.clear()
        chat_id = message.chat.id
        await message.answer(
            help_text(
                is_owner=chat_id == owner_chat_id,
                can_add_order=await can_add_order(db, chat_id, owner_chat_id),
            )
        )

    @router.callback_query(F.data == "menu:help")
    async def on_menu_help(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        chat_id = callback.from_user.id
        await edit_message(
            callback,
            help_text(
                is_owner=chat_id == owner_chat_id,
                can_add_order=await can_add_order(db, chat_id, owner_chat_id),
            ),
            back_to_menu_keyboard(),
        )
        await callback.answer()

    # Сама карточка и переходы — в orders_ui.py; кнопка открытия здесь, чтобы
    # срабатывать раньше обработчиков диалогов (см. комментарий выше).
    @router.message(Command("orders", "my_orders", "заказы", "мои_заказы"))
    @router.message(F.text == BTN_ORDERS)
    async def on_orders(
        message: Message, db: Database, owner_chat_id: int, state: FSMContext
    ) -> None:
        await state.clear()
        await send_orders_home(message.answer, db, message.chat.id, owner_chat_id)

    @router.message(Command("settings", "настройки"))
    @router.message(F.text == BTN_SETTINGS)
    async def on_settings(
        message: Message,
        db: Database,
        owner_chat_id: int,
        state: FSMContext,
        reminder_settings: ReminderSettings,
    ) -> None:
        await state.clear()
        text, keyboard = await settings_view(db, message.chat.id, owner_chat_id, reminder_settings)
        await message.answer(text, reply_markup=keyboard)

    @router.message(Command("todo"))
    @router.message(F.text == BTN_TODO)
    async def on_todo(
        message: Message,
        owner_chat_id: int,
        state: FSMContext,
        invite_links: dict[str, str],
        ms_client_id: str,
    ) -> None:
        await state.clear()
        text, keyboard = todo_screen(
            invite_links, ms_client_id, is_owner=message.chat.id == owner_chat_id
        )
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data == "menu:todo")
    async def on_menu_todo(
        callback: CallbackQuery,
        owner_chat_id: int,
        invite_links: dict[str, str],
        ms_client_id: str,
    ) -> None:
        text, keyboard = todo_screen(
            invite_links, ms_client_id, is_owner=callback.from_user.id == owner_chat_id
        )
        await edit_message(callback, text, keyboard)
        await callback.answer()

    @router.message(Command("link_microsoft", "связать_microsoft"))
    async def on_link_microsoft(message: Message, db: Database, ms_client_id: str) -> None:
        await start_microsoft_link(message.answer, db, message.chat.id, ms_client_id)

    @router.callback_query(F.data == "todo:link")
    async def on_todo_link(callback: CallbackQuery, db: Database, ms_client_id: str) -> None:
        await callback.answer()
        await start_microsoft_link(
            callback.message.answer, db, callback.from_user.id, ms_client_id
        )

    @router.message(Command("roles", "роли"))
    @router.message(F.text == BTN_ROLES)
    async def on_roles(message: Message, owner_chat_id: int, state: FSMContext) -> None:
        await state.clear()
        if message.chat.id != owner_chat_id:
            await message.answer(OWNER_ONLY)
            return
        await message.answer("Выбери роль:", reply_markup=roles_menu_keyboard())

    @router.callback_query(F.data == "menu:roles")
    async def on_menu_roles(callback: CallbackQuery, owner_chat_id: int) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        await edit_message(callback, "Выбери роль:", roles_menu_keyboard())
        await callback.answer()

    @router.message(Command("host"))
    @router.message(F.text == BTN_HOST)
    async def on_host(
        message: Message, owner_chat_id: int, state: FSMContext, runtime: RuntimeState
    ) -> None:
        await state.clear()
        if message.chat.id != owner_chat_id:
            await message.answer(OWNER_ONLY)
            return
        await message.answer(
            HOST_MENU_TEXT, reply_markup=host_menu_keyboard(supervised=runtime.supervised)
        )

    @router.callback_query(F.data == "menu:host")
    async def on_menu_host(
        callback: CallbackQuery, owner_chat_id: int, state: FSMContext, runtime: RuntimeState
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        await state.clear()
        await edit_message(callback, HOST_MENU_TEXT, host_menu_keyboard(supervised=runtime.supervised))
        await callback.answer()

    async def _begin_new_order(send, state: FSMContext) -> None:
        await state.set_state(NewOrderStates.school)
        await send("Новый заказ.\nШкола/класс?", reply_markup=cancel_reply_keyboard())

    @router.message(Command("new_order", "новый_заказ"))
    @router.message(F.text == BTN_NEW_ORDER)
    async def on_new_order(
        message: Message, db: Database, owner_chat_id: int, state: FSMContext
    ) -> None:
        await state.clear()
        if not await can_add_order(db, message.chat.id, owner_chat_id):
            await message.answer(NOT_ALLOWED_TO_ADD_ORDER)
            return
        await _begin_new_order(message.answer, state)

    @router.callback_query(F.data == "menu:new_order")
    async def on_menu_new_order(
        callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext
    ) -> None:
        if not await can_add_order(db, callback.from_user.id, owner_chat_id):
            await callback.answer(NOT_ALLOWED_TO_ADD_ORDER, show_alert=True)
            return
        await callback.answer()
        await _begin_new_order(callback.message.answer, state)

    @router.message(NewOrderStates.school, F.text)
    async def on_new_order_school(message: Message, state: FSMContext) -> None:
        await state.update_data(school=message.text)
        await state.set_state(NewOrderStates.contact_name)
        await message.answer("Контактное лицо (ФИО)?")

    @router.message(NewOrderStates.contact_name, F.text)
    async def on_new_order_contact_name(message: Message, state: FSMContext) -> None:
        await state.update_data(contact_name=message.text)
        await state.set_state(NewOrderStates.contact_phone)
        await message.answer("Телефон?")

    @router.message(NewOrderStates.contact_phone, F.text)
    async def on_new_order_contact_phone(message: Message, state: FSMContext) -> None:
        await state.update_data(contact_phone=message.text)
        await state.set_state(NewOrderStates.people_count)
        await message.answer("Количество человек?")

    @router.message(NewOrderStates.people_count, F.text)
    async def on_new_order_people_count(
        message: Message,
        db: Database,
        owner_chat_id: int,
        state: FSMContext,
        sheet: SheetClient,
        runtime: RuntimeState,
    ) -> None:
        data = await state.get_data()
        await state.clear()
        keyboard = await reply_keyboard_for(db, message.chat.id, owner_chat_id)
        try:
            rows = await asyncio.to_thread(sheet.fetch_rows)
            order_id = next_order_id(rows)
            row = build_new_order_row(
                order_id,
                school=data["school"],
                contact_name=data["contact_name"],
                contact_phone=data["contact_phone"],
                people_count=message.text,
            )
            await asyncio.to_thread(sheet.append_row, row)
        except Exception:
            log.exception("Не удалось добавить заказ в таблицу")
            await message.answer(
                "❌ Не получилось записать заказ в таблицу. Попробуй ещё раз чуть позже.",
                reply_markup=keyboard,
            )
            return
        runtime.poll_now.set()
        await message.answer(
            f"✅ Заказ №{order_id} создан: {data['school']}.\n"
            f"Он уже в таблице со статусом «Лид» — карточка в {BTN_ORDERS}.",
            reply_markup=keyboard,
        )

    @router.callback_query(F.data == "host:status")
    async def on_host_status(
        callback: CallbackQuery,
        db: Database,
        owner_chat_id: int,
        runtime: RuntimeState,
        graph: "GraphTodoClient | None",
        invite_links: dict[str, str],
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        await callback.answer()
        commit = await asyncio.to_thread(current_commit, runtime.project_dir)
        text = format_status(
            runtime,
            now=datetime.now(timezone.utc),
            commit=commit,
            users_count=len(await db.list_users()),
            todo_configured=graph is not None,
            invite_links_count=len(invite_links),
            lists_count=len(role_names()),
        )
        await edit_message(callback, text, host_menu_keyboard(supervised=runtime.supervised))

    @router.callback_query(F.data == "host:update")
    async def on_host_update(
        callback: CallbackQuery, owner_chat_id: int, runtime: RuntimeState
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        in_docker = runtime.kind == "docker"
        await callback.answer("Обновляю…")
        await edit_message(callback, "⏳ Обновляю: скачиваю код и устанавливаю…")
        result = await asyncio.to_thread(
            update_from_git, runtime.project_dir, sys.executable, install=not in_docker
        )
        keyboard = host_menu_keyboard(supervised=runtime.supervised)
        if not result.ok:
            await edit_message(callback, f"❌ Не получилось обновить:\n{_clip(result.output)}", keyboard)
            return
        if not result.changed:
            await edit_message(callback, f"Уже последняя версия ({result.commit}).", keyboard)
            return
        if in_docker and result.deps_changed:
            # Зависимости живут в образе — их не подтянуть на лету.
            await edit_message(
                callback,
                f"✅ Код обновлён до {result.commit}, но изменились зависимости — нужен пересбор.\n"
                "На Mac в папке проекта выполни:\ndocker compose up -d --build",
                keyboard,
            )
            return
        if runtime.supervised:
            await edit_message(
                callback, f"✅ Обновлено до {result.commit}. Перезапускаюсь — напишу, когда поднимусь."
            )
            runtime.restart_requested.set()
            return
        await edit_message(
            callback,
            f"✅ Код обновлён до {result.commit}, но бот запущен вручную из терминала — "
            "перезапусти его там или включи автозапуск: python -m crmlumi install-service",
            keyboard,
        )

    @router.callback_query(F.data == "host:restart")
    async def on_host_restart(
        callback: CallbackQuery, owner_chat_id: int, runtime: RuntimeState
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        if not runtime.supervised:
            await callback.answer(
                "Бот запущен вручную из терминала — перезапуск отсюда недоступен.",
                show_alert=True,
            )
            return
        await callback.answer()
        await edit_message(callback, "🔁 Перезапускаюсь — напишу, когда поднимусь.")
        runtime.restart_requested.set()

    @router.callback_query(F.data == "host:logs")
    async def on_host_logs(
        callback: CallbackQuery, owner_chat_id: int, runtime: RuntimeState
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        await callback.answer()
        text = tail_text(err_log_path(runtime.project_dir))
        if text is None:
            await callback.message.answer(
                "Лог-файла нет — бот запущен не через автозапуск, лог виден в окне терминала."
            )
            return
        await callback.message.answer(text or "Лог пуст.")

    @router.callback_query(F.data == "host:invites")
    async def on_host_invites(
        callback: CallbackQuery, owner_chat_id: int, invite_links: dict[str, str]
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        await edit_message(callback, INVITE_EDITOR_TEXT, invite_links_editor_keyboard(invite_links))
        await callback.answer()

    @router.callback_query(F.data == "host:broadcast")
    async def on_host_broadcast(
        callback: CallbackQuery,
        db: Database,
        owner_chat_id: int,
        invite_links: dict[str, str],
        ms_client_id: str,
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        await callback.answer("Рассылаю…")
        sent = await broadcast_menu(callback.bot, db, owner_chat_id, invite_links, ms_client_id)
        await callback.message.answer(f"📣 Меню разослано: {sent} чел.")

    @router.callback_query(F.data.startswith("invite:"))
    async def on_invite_pick(
        callback: CallbackQuery, owner_chat_id: int, state: FSMContext
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        index = int(callback.data.split(":", 1)[1])
        await state.set_state(InviteLinkStates.link)
        await state.update_data(role_index=index)
        await callback.answer()
        await callback.message.answer(
            f"Пришли ссылку-приглашение для списка «{role_names()[index]}».",
            reply_markup=cancel_reply_keyboard(),
        )

    @router.message(InviteLinkStates.link, F.text)
    async def on_invite_link(
        message: Message,
        db: Database,
        owner_chat_id: int,
        state: FSMContext,
        config_path: Path,
        invite_links: dict[str, str],
    ) -> None:
        link = message.text.strip()
        if not link.startswith("https://"):
            await message.answer(
                f"Это не похоже на ссылку — пришли ссылку, которая начинается с https://, "
                f"или нажми «{BTN_CANCEL}»."
            )
            return
        data = await state.get_data()
        await state.clear()
        role_name = role_names()[data["role_index"]]
        write_invite_link(config_path, role_name, link)
        invite_links[role_name] = link
        await message.answer(
            f"✅ Ссылка для «{role_name}» сохранена.",
            reply_markup=await reply_keyboard_for(db, message.chat.id, owner_chat_id),
        )
        await message.answer(INVITE_EDITOR_TEXT, reply_markup=invite_links_editor_keyboard(invite_links))

    @router.callback_query(F.data.startswith("role:"))
    async def on_role_picked(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        index = int(callback.data.split(":", 1)[1])
        role_name = role_names()[index]
        users = await db.list_users()
        current = set(await db.get_role_assignees(role_name))
        await edit_message(
            callback,
            f"Кто в роли «{role_name}»?\nНажми на человека, чтобы добавить или убрать — "
            "можно сколько угодно людей.",
            user_picker_keyboard(index, users, current),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("assign:"))
    async def on_user_picked(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        _, index_str, chat_id_str = callback.data.split(":", 2)
        index, chat_id = int(index_str), int(chat_id_str)
        role_name = role_names()[index]

        before = set(await db.get_role_assignees(role_name))
        await db.toggle_role_member(role_name, chat_id)

        users = await db.list_users()
        current = set(await db.get_role_assignees(role_name))
        await edit_message(
            callback,
            role_status_text(role_name, [u for u in users if u["chat_id"] in current]),
            user_picker_keyboard(index, users, current),
        )
        await callback.answer()
        await apply_role_changes(callback.bot, db, owner_chat_id, role_name, before, current)

    @router.callback_query(F.data.startswith("clear:"))
    async def on_role_cleared(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer(OWNER_ONLY, show_alert=True)
            return
        index = int(callback.data.split(":", 1)[1])
        role_name = role_names()[index]
        before = set(await db.get_role_assignees(role_name))
        await db.clear_role(role_name)
        users = await db.list_users()
        await edit_message(
            callback,
            role_status_text(role_name, []),
            user_picker_keyboard(index, users, set()),
        )
        await callback.answer()
        await apply_role_changes(callback.bot, db, owner_chat_id, role_name, before, set())

    router.include_router(build_orders_router())
    router.include_router(build_shoots_router())
    router.include_router(build_settings_router())
    return router


async def notify_new_lead(bot: Bot, db: Database, order: Order) -> None:
    assignees = await db.get_role_assignees(ROLE_NEW_LEADS)
    if not assignees:
        log.warning(
            "Новый лид %s, но роль %r никому не назначена", order.order_id, ROLE_NEW_LEADS
        )
        return
    text = format_new_lead_message(order)
    keyboard = open_order_keyboard(order.order_id)
    for chat_id in assignees:
        try:
            await bot.send_message(chat_id, text, reply_markup=keyboard)
        except Exception:
            log.exception(
                "Не удалось отправить уведомление о лиде %s в чат %s", order.order_id, chat_id
            )


def format_stage_message(order: Order, role_name: str) -> str:
    lines = [
        f"Новая задача: {role_name}",
        f"Заказ №{order.order_id}: {order.school}",
        f"Этап: {order.status}",
    ]
    if order.locations:
        lines.append(f"Съёмки: {order.locations}")
    links = stage_links(order)
    if links:
        lines.append("")
        lines += [f"{label}: {link}" for label, link in links]
    elif order.status in STAGE_LINKS:
        lines.append("\nСсылку на материалы ещё не прислали — она придёт сюда, как только появится.")
    return "\n".join(lines)


async def notify_stage_role(bot: Bot, db: Database, order: Order, role_name: str) -> None:
    assignees = await db.get_role_assignees(role_name)
    if not assignees:
        log.warning(
            "Заказ %s перешёл в статус, требующий роль %r, но она никому не назначена",
            order.order_id,
            role_name,
        )
        return
    text = format_stage_message(order, role_name)
    keyboard = open_order_keyboard(order.order_id)
    for chat_id in assignees:
        try:
            await bot.send_message(chat_id, text, reply_markup=keyboard)
        except Exception:
            log.exception(
                "Не удалось отправить уведомление по заказу %s в чат %s", order.order_id, chat_id
            )


def format_daily_digest(
    overdue_tasks: list[dict],
    shoots_without_photographer: list[tuple[Order, Shoot]],
    stalled_orders: list[tuple[Order, int]],
) -> str | None:
    if not overdue_tasks and not shoots_without_photographer and not stalled_orders:
        return None

    sections = ["Ежедневная сводка"]
    if shoots_without_photographer:
        sections.append("\nСъёмки без фотографа, дата близко:")
        for order, shoot in shoots_without_photographer:
            sections.append(
                f"- №{order.order_id} {order.school}: {format_when(shoot.date, shoot.time)}, {shoot.location}"
            )
    if stalled_orders:
        sections.append("\nЗависли в статусе:")
        for order, days in stalled_orders:
            sections.append(f"- {order.school}: «{order.status}» уже {days} дн.")
    if overdue_tasks:
        sections.append("\nПросроченные задачи в To Do:")
        for task in overdue_tasks:
            sections.append(f"- [{task['role']}] {task['title']}")
    return "\n".join(sections)


async def send_daily_digest(bot: Bot, owner_chat_id: int, text: str) -> None:
    try:
        await bot.send_message(owner_chat_id, text)
    except Exception:
        log.exception("Не удалось отправить ежедневную сводку владельцу")
