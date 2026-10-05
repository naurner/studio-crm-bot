"""Кнопки и команды бота — чтобы ничего не приходилось набирать руками.

Три слоя, одно и то же содержимое:
- постоянная клавиатура под полем ввода (ReplyKeyboard) — основной способ;
- меню команд «/» в Telegram (setMyCommands) — имена только латиницей,
  кириллические команды Telegram не подсвечивает и не принимает в меню;
- инлайн-меню внутри сообщений — для подменю с кнопкой «назад».
"""

from __future__ import annotations

import html

from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from ..roles import role_names

BTN_ORDERS = "📂 Заказы"
BTN_NEW_ORDER = "➕ Новый заказ"
BTN_TODO = "📋 Списки To Do"
BTN_HELP = "❓ Помощь"
BTN_ROLES = "👥 Роли"
BTN_HOST = "🖥 Хост"
BTN_CANCEL = "✖️ Отмена"
BTN_SKIP = "⏭ Пропустить"
BTN_SETTINGS = "⚙️ Настройки"


def main_reply_keyboard(*, is_owner: bool, can_add_order: bool) -> ReplyKeyboardMarkup:
    first_row = [KeyboardButton(text=BTN_ORDERS)]
    if can_add_order:
        first_row.append(KeyboardButton(text=BTN_NEW_ORDER))
    # Не больше двух длинных кнопок в ряду: на телефоне в треть ширины
    # «📋 Списки To Do» уже не влезает.
    rows = [first_row, [KeyboardButton(text=BTN_TODO), KeyboardButton(text=BTN_SETTINGS)]]
    if is_owner:
        rows.append(
            [KeyboardButton(text=BTN_ROLES), KeyboardButton(text=BTN_HOST), KeyboardButton(text=BTN_HELP)]
        )
    else:
        rows.append([KeyboardButton(text=BTN_HELP)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие кнопкой",
    )


def cancel_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True, is_persistent=True
    )


def input_reply_keyboard(*, optional: bool) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(text=BTN_SKIP)] if optional else []
    row.append(KeyboardButton(text=BTN_CANCEL))
    return ReplyKeyboardMarkup(keyboard=[row], resize_keyboard=True, is_persistent=True)


def main_menu_keyboard(*, is_owner: bool, can_add_order: bool) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=BTN_ORDERS, callback_data="ord:home")]]
    if can_add_order:
        buttons.append([InlineKeyboardButton(text=BTN_NEW_ORDER, callback_data="menu:new_order")])
    buttons.append([InlineKeyboardButton(text=BTN_TODO, callback_data="menu:todo")])
    buttons.append([InlineKeyboardButton(text=BTN_SETTINGS, callback_data="set:menu")])
    if is_owner:
        buttons.append([InlineKeyboardButton(text=BTN_ROLES, callback_data="menu:roles")])
        buttons.append([InlineKeyboardButton(text=BTN_HOST, callback_data="menu:host")])
    buttons.append([InlineKeyboardButton(text=BTN_HELP, callback_data="menu:help")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def bot_commands(*, is_owner: bool, can_add_order: bool) -> list[BotCommand]:
    commands = [
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="orders", description="Заказы по этапам"),
    ]
    if can_add_order:
        commands.append(BotCommand(command="new_order", description="Добавить заказ"))
    commands.append(BotCommand(command="todo", description="Списки Microsoft To Do"))
    commands.append(BotCommand(command="settings", description="Частота напоминаний"))
    if is_owner:
        commands.append(BotCommand(command="roles", description="Кто за что отвечает"))
        commands.append(BotCommand(command="host", description="Статус, обновление, перезапуск"))
    commands.append(BotCommand(command="cancel", description="Отменить ввод"))
    commands.append(BotCommand(command="help", description="Что умеет бот"))
    commands.append(BotCommand(command="start", description="Вернуть кнопки"))
    return commands


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Меню", callback_data="menu:main")]]
    )


def help_text(*, is_owner: bool, can_add_order: bool) -> str:
    lines = [
        "Всё управление — кнопками внизу 👇",
        "",
        f"{BTN_ORDERS} — все заказы по этапам. В карточке: следующий этап, сумма, "
        "оплата и «📍 Съёмки» — у заказа сколько угодно съёмок, у каждой своя дата, "
        "время и локация. «🔥 Ждут меня» — заказы, где сейчас нужно твоё действие",
    ]
    if can_add_order:
        lines.append(f"{BTN_NEW_ORDER} — бот задаст пару вопросов и сам внесёт заказ в таблицу")
    lines.append(f"{BTN_TODO} — присоединиться к общим спискам задач")
    lines.append(f"{BTN_SETTINGS} — как часто напоминать о твоих делах")
    if is_owner:
        lines.append(f"{BTN_ROLES} — кто за какой этап отвечает")
        lines.append(f"{BTN_HOST} — статус бота, обновление, сроки этапов, лог")
    lines += [
        "",
        "Дату и время можно выбрать кнопками или написать: «12.10 10:00», «завтра 15:30».",
        "Когда приходит «Нужен фотограф» — жми «Взять» прямо в сообщении: каждая съёмка берётся отдельно.",
        "Фотографам: вечером — расписание на завтра, в воскресенье — на неделю, за 2 ч — напоминание.",
        "Пропали кнопки — нажми /start.",
    ]
    return "\n".join(lines)


def host_menu_keyboard(*, supervised: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📊 Статус", callback_data="host:status")],
        [InlineKeyboardButton(text="🔄 Обновить с GitHub", callback_data="host:update")],
    ]
    if supervised:
        buttons.append([InlineKeyboardButton(text="🔁 Перезапустить", callback_data="host:restart")])
    buttons += [
        [InlineKeyboardButton(text="⏱ Сроки этапов", callback_data="dl:menu")],
        [InlineKeyboardButton(text="🔗 Ссылки To Do", callback_data="host:invites")],
        [InlineKeyboardButton(text="📣 Разослать меню всем", callback_data="host:broadcast")],
        [InlineKeyboardButton(text="📜 Лог", callback_data="host:logs")],
        [InlineKeyboardButton(text="← Меню", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def todo_links_keyboard(
    invite_links: dict[str, str], *, can_link_account: bool
) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"➡️ {role}", url=invite_links[role])]
        for role in role_names()
        if invite_links.get(role)
    ]
    if can_link_account:
        buttons.append(
            [InlineKeyboardButton(text="🔐 Привязать аккаунт Microsoft", callback_data="todo:link")]
        )
    buttons.append([InlineKeyboardButton(text="← Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def invite_links_editor_keyboard(invite_links: dict[str, str]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if invite_links.get(role) else '➕'} {role}",
                callback_data=f"invite:{index}",
            )
        ]
        for index, role in enumerate(role_names())
    ]
    buttons.append([InlineKeyboardButton(text="← Хост", callback_data="menu:host")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def device_login_message(user_code: str) -> str:
    """HTML: код в <code> — в Telegram он копируется одним нажатием."""
    return (
        "Вход в Microsoft:\n"
        "1. Нажми «Открыть страницу входа»\n"
        f"2. Вставь код: <code>{html.escape(user_code)}</code> (нажми на код — скопируется)\n"
        "3. Войди в свой аккаунт Microsoft\n\n"
        "Я напишу, когда всё получится."
    )


def device_login_keyboard(verification_uri: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Открыть страницу входа", url=verification_uri)]
        ]
    )
