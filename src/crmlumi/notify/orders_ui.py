"""Заказы в боте: список по этапам, карточка, следующий этап, правка полей.

Весь цикл заказа проходится отсюда, без открытия таблицы. Правила — кто
что может, куда двигать, что спросить — в pipeline/stages.py; съёмки заказа —
в shoots_ui.py; здесь только экраны заказа и кнопки.
"""

from __future__ import annotations

import logging
from collections import Counter

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..db import Database
from ..host import RuntimeState
from ..models import FIELD_COLUMNS, Order
from ..pipeline.deletion import delete_order
from ..pipeline.stages import (
    EDITABLE_FIELDS,
    LINK_FIELDS,
    PAYMENT_OPTIONS,
    apply_order_update,
    format_date,
    get_order_access,
    looks_like_link,
    missing_inputs,
    next_status,
    order_sort_key,
    orders_waiting_for,
    previous_status,
    shoots_problem,
    stage_index,
)
from ..sheet_source import SheetClient
from ..statuses import FUNNEL_ORDER, STATUS_DATE_LOCATION_SET, STATUS_DELIVERED
from ..timefmt import format_when
from ..todo.graph_client import GraphTodoClient
from .access import reply_keyboard_for
from .menus import BTN_CANCEL, BTN_SKIP, input_reply_keyboard
from .messaging import edit_message
from .order_card import button, notify_link_added, render_card
from .shoots_ui import start_add_shoot

log = logging.getLogger(__name__)

PAGE_SIZE = 8
OTHER_STAGE = "o"
WRITE_FAILED = "❌ Не получилось записать в таблицу. Попробуй ещё раз чуть позже."

FIELD_PROMPTS: dict[str, str] = {
    "contract_amount": "Сумма контракта?",
    "people_count": "Сколько человек?",
    "raw_photos_link": "📁 Ссылка на диск с исходниками? Её сразу получит обработка фото.",
    "processed_photos_link": "🎨 Ссылка на обработанные фото? Её сразу получит вёрстка.",
    "layout_link": "📖 Ссылка на макет альбома? Её получат согласование и печать.",
}


class OrderInputStates(StatesGroup):
    value = State()


def order_button_text(order: Order) -> str:
    parts = [f"№{order.order_id}", order.school or "без названия"]
    if order.shoot_date:
        parts.append(format_date(order.shoot_date))
    text = " · ".join(parts)
    return text if len(text) <= 60 else text[:59] + "…"


def orders_home_text(orders: list[Order]) -> str:
    if not orders:
        return "Заказов пока нет."
    active = sum(1 for order in orders if order.status != STATUS_DELIVERED)
    return f"📂 Заказы — в работе: {active}\nВыбери этап:"


def orders_home_keyboard(orders: list[Order], *, waiting_count: int) -> InlineKeyboardMarkup:
    counts = Counter(order.status for order in orders)
    rows = []
    if waiting_count:
        rows.append([button(f"🔥 Ждут меня ({waiting_count})", "ord:mine:0")])
    for index, status in enumerate(FUNNEL_ORDER):
        if counts[status]:
            rows.append([button(f"{status} ({counts[status]})", f"ord:st:{index}:0")])
    other = sum(count for status, count in counts.items() if status not in FUNNEL_ORDER)
    if other:
        rows.append([button(f"❓ Без этапа ({other})", f"ord:st:{OTHER_STAGE}:0")])
    rows.append([button("← Меню", "menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def orders_list_text(title: str, total: int, page: int) -> str:
    pages = max(1, -(-total // PAGE_SIZE))
    text = f"{title} — {total} шт."
    if pages > 1:
        text += f" (стр. {page + 1} из {pages})"
    return text if total else f"{title} — пусто."


def orders_list_keyboard(orders: list[Order], *, page: int, page_prefix: str) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    rows = [
        [button(order_button_text(order), f"ord:open:{order.order_id}")]
        for order in orders[start : start + PAGE_SIZE]
    ]
    nav = []
    if page > 0:
        nav.append(button("« Назад", f"{page_prefix}:{page - 1}"))
    if start + PAGE_SIZE < len(orders):
        nav.append(button("Дальше »", f"{page_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("← Заказы", "ord:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Подписи кнопок «✏️ Изменить»: по две в ряд, на телефоне влезает ~15 символов.
FIELD_BUTTON_LABELS: dict[str, str] = {
    "school": "Школа/класс",
    "contact_name": "Контакт",
    "contact_phone": "Телефон",
    "people_count": "Кол-во человек",
    "contract_amount": "Сумма",
    "notes": "Примечания",
    "raw_photos_link": "📁 Исходники",
    "processed_photos_link": "🎨 Обработанные",
    "layout_link": "📖 Макет",
}


def edit_fields_keyboard(order_id: str, fields: tuple[str, ...]) -> InlineKeyboardMarkup:
    buttons = [
        button(
            FIELD_BUTTON_LABELS.get(field, FIELD_COLUMNS[field]),
            f"ord:field:{order_id}:{EDITABLE_FIELDS.index(field)}",
        )
        for field in fields
        if field != "payment_status"
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([button("← К заказу", f"ord:open:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(order_id: str, current: str) -> InlineKeyboardMarkup:
    rows = [
        [button(f"{'✅ ' if option == current else ''}{option}", f"ord:payset:{order_id}:{i}")]
        for i, option in enumerate(PAYMENT_OPTIONS)
    ]
    rows.append([button("← К заказу", f"ord:open:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stage_picker_keyboard(order_id: str, current: str) -> InlineKeyboardMarkup:
    rows = [
        [button(f"{'✅ ' if status == current else ''}{status}", f"ord:set:{order_id}:{i}")]
        for i, status in enumerate(FUNNEL_ORDER)
    ]
    rows.append([button("← К заказу", f"ord:open:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def field_prompt(field: str, current: str | None = None) -> str:
    prompt = FIELD_PROMPTS.get(field, f"{FIELD_COLUMNS[field]}?")
    if current is not None:
        prompt += f"\nСейчас: {current or '—'}\n(«-» — очистить)"
    return prompt


async def orders_home_view(
    db: Database, chat_id: int, owner_chat_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    orders = await db.list_cached_orders()
    waiting = await orders_waiting_for(db, chat_id, owner_chat_id)
    return orders_home_text(orders), orders_home_keyboard(orders, waiting_count=len(waiting))


async def send_orders_home(send, db: Database, chat_id: int, owner_chat_id: int) -> None:
    text, keyboard = await orders_home_view(db, chat_id, owner_chat_id)
    await send(text, reply_markup=keyboard)


def build_orders_router() -> Router:
    router = Router()

    async def load_or_alert(callback: CallbackQuery, db: Database, order_id: str) -> Order | None:
        order = await db.get_cached_order(order_id)
        if order is None:
            await callback.answer(
                "Заказ не найден — если его только что создали, подожди пару секунд.",
                show_alert=True,
            )
        return order

    async def show_card(callback: CallbackQuery, db: Database, owner_chat_id: int, order: Order, deadlines) -> None:
        text, keyboard = await render_card(
            db, callback.from_user.id, owner_chat_id, order, deadlines=deadlines
        )
        await edit_message(callback, text, keyboard)

    async def write(callback: CallbackQuery, db, sheet, shoots_sheet, runtime, order, **changes) -> Order | None:
        try:
            updated = await apply_order_update(db, sheet, order, shoots_sheet=shoots_sheet, **changes)
        except Exception:
            log.exception("Не удалось записать заказ %s в таблицу", order.order_id)
            await callback.answer(WRITE_FAILED, show_alert=True)
            return None
        runtime.poll_now.set()
        return updated

    @router.callback_query(F.data == "ord:home")
    async def on_home(callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext) -> None:
        await state.clear()
        text, keyboard = await orders_home_view(db, callback.from_user.id, owner_chat_id)
        await edit_message(callback, text, keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("ord:mine:"))
    async def on_mine(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        page = int(callback.data.rsplit(":", 1)[1])
        orders = await orders_waiting_for(db, callback.from_user.id, owner_chat_id)
        await edit_message(
            callback,
            orders_list_text("🔥 Ждут меня", len(orders), page),
            orders_list_keyboard(orders, page=page, page_prefix="ord:mine"),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ord:st:"))
    async def on_stage(callback: CallbackQuery, db: Database) -> None:
        _, _, stage, page_text = callback.data.split(":")
        page = int(page_text)
        orders = await db.list_cached_orders()
        if stage == OTHER_STAGE:
            title = "❓ Без этапа"
            selected = [order for order in orders if order.status not in FUNNEL_ORDER]
        else:
            title = FUNNEL_ORDER[int(stage)]
            selected = [order for order in orders if order.status == title]
        selected.sort(key=order_sort_key)
        await edit_message(
            callback,
            orders_list_text(title, len(selected), page),
            orders_list_keyboard(selected, page=page, page_prefix=f"ord:st:{stage}"),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ord:open:"))
    async def on_open(
        callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext, deadlines: dict[str, int]
    ) -> None:
        await state.clear()
        order = await load_or_alert(callback, db, callback.data.split(":")[2])
        if order is None:
            return
        await show_card(callback, db, owner_chat_id, order, deadlines)
        await callback.answer()

    @router.callback_query(F.data.startswith("ord:show:"))
    async def on_show(
        callback: CallbackQuery, db: Database, owner_chat_id: int, deadlines: dict[str, int]
    ) -> None:
        order = await load_or_alert(callback, db, callback.data.split(":")[2])
        if order is None:
            return
        await callback.answer()
        text, keyboard = await render_card(
            db, callback.from_user.id, owner_chat_id, order, deadlines=deadlines
        )
        await callback.message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("ord:adv:"))
    async def on_advance(
        callback: CallbackQuery,
        db: Database,
        owner_chat_id: int,
        sheet: SheetClient,
        shoots_sheet: SheetClient,
        runtime: RuntimeState,
        state: FSMContext,
        deadlines: dict[str, int],
    ) -> None:
        _, _, order_id, from_index = callback.data.split(":")
        order = await load_or_alert(callback, db, order_id)
        if order is None:
            return
        if stage_index(order.status) != int(from_index):
            await callback.answer(f"Заказ уже на этапе «{order.status}».", show_alert=True)
            await show_card(callback, db, owner_chat_id, order, deadlines)
            return
        access = await get_order_access(db, callback.from_user.id, owner_chat_id, order)
        if not access.can_advance:
            await callback.answer("Этот этап ведёт другой человек.", show_alert=True)
            return

        target = next_status(order.status)
        shoots = await db.list_shoots_for_order(order_id)
        problem = shoots_problem(target, shoots)
        if problem and target == STATUS_DATE_LOCATION_SET and not shoots:
            # Съёмок ещё нет — сразу ведём в мастер, после него «✅ Готово».
            await callback.answer()
            await start_add_shoot(
                callback.message.answer,
                state,
                order_id,
                title=f"Назначаем съёмки заказа №{order_id}. Добавь первую — потом можно ещё.",
            )
            return
        if problem:
            await callback.answer(problem, show_alert=True)
            return

        inputs = missing_inputs(order, target)
        if inputs:
            await state.set_state(OrderInputStates.value)
            await state.update_data(
                order_id=order_id,
                target=target,
                from_index=int(from_index),
                pending=[list(item) for item in inputs],
                values={},
            )
            await callback.answer()
            field, required = inputs[0]
            await callback.message.answer(
                f"Переводим №{order_id} на «{target}».\n{field_prompt(field)}",
                reply_markup=input_reply_keyboard(optional=not required),
            )
            return

        updated = await write(callback, db, sheet, shoots_sheet, runtime, order, status=target)
        if updated is None:
            return
        await show_card(callback, db, owner_chat_id, updated, deadlines)
        await callback.answer(f"Готово: «{target}»")

    async def owner_set_status(callback, db, owner_chat_id, sheet, shoots_sheet, runtime, order, status, deadlines) -> None:
        updated = await write(callback, db, sheet, shoots_sheet, runtime, order, status=status)
        if updated is None:
            return
        await show_card(callback, db, owner_chat_id, updated, deadlines)
        await callback.answer(f"Этап: «{status}»")

    @router.callback_query(F.data.startswith("ord:back:"))
    async def on_back(
        callback: CallbackQuery, db: Database, owner_chat_id: int, sheet: SheetClient,
        shoots_sheet: SheetClient, runtime: RuntimeState, deadlines: dict[str, int],
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Это только для владельца.", show_alert=True)
            return
        _, _, order_id, from_index = callback.data.split(":")
        order = await load_or_alert(callback, db, order_id)
        if order is None:
            return
        if stage_index(order.status) != int(from_index):
            await callback.answer(f"Заказ уже на этапе «{order.status}».", show_alert=True)
            await show_card(callback, db, owner_chat_id, order, deadlines)
            return
        await owner_set_status(
            callback, db, owner_chat_id, sheet, shoots_sheet, runtime, order,
            previous_status(order.status), deadlines,
        )

    @router.callback_query(F.data.startswith("ord:pick:"))
    async def on_pick(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Это только для владельца.", show_alert=True)
            return
        order = await load_or_alert(callback, db, callback.data.split(":")[2])
        if order is None:
            return
        await edit_message(
            callback,
            f"Заказ №{order.order_id} — на какой этап поставить?",
            stage_picker_keyboard(order.order_id, order.status),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ord:set:"))
    async def on_set(
        callback: CallbackQuery, db: Database, owner_chat_id: int, sheet: SheetClient,
        shoots_sheet: SheetClient, runtime: RuntimeState, deadlines: dict[str, int],
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Это только для владельца.", show_alert=True)
            return
        _, _, order_id, index = callback.data.split(":")
        order = await load_or_alert(callback, db, order_id)
        if order is None:
            return
        await owner_set_status(
            callback, db, owner_chat_id, sheet, shoots_sheet, runtime, order,
            FUNNEL_ORDER[int(index)], deadlines,
        )

    @router.callback_query(F.data.startswith("ord:edit:"))
    async def on_edit(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        order = await load_or_alert(callback, db, callback.data.split(":")[2])
        if order is None:
            return
        access = await get_order_access(db, callback.from_user.id, owner_chat_id, order)
        if not access.editable_fields:
            await callback.answer("Менять этот заказ может ответственный за этап.", show_alert=True)
            return
        await edit_message(
            callback,
            f"Заказ №{order.order_id} — что изменить? Дата, время и локации — в «📍 Съёмки».",
            edit_fields_keyboard(order.order_id, access.editable_fields),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ord:field:"))
    async def on_field(
        callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext
    ) -> None:
        _, _, order_id, field_index = callback.data.split(":")
        field = EDITABLE_FIELDS[int(field_index)]
        order = await load_or_alert(callback, db, order_id)
        if order is None:
            return
        access = await get_order_access(db, callback.from_user.id, owner_chat_id, order)
        if field not in access.editable_fields:
            await callback.answer("Это поле тебе менять нельзя.", show_alert=True)
            return
        await state.set_state(OrderInputStates.value)
        await state.update_data(
            order_id=order_id, target=None, from_index=None, pending=[[field, True]], values={}
        )
        await callback.answer()
        await callback.message.answer(
            f"Заказ №{order_id}. {field_prompt(field, getattr(order, field))}",
            reply_markup=input_reply_keyboard(optional=False),
        )

    @router.callback_query(F.data.startswith("ord:pay:"))
    async def on_payment(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        order = await load_or_alert(callback, db, callback.data.split(":")[2])
        if order is None:
            return
        access = await get_order_access(db, callback.from_user.id, owner_chat_id, order)
        if "payment_status" not in access.editable_fields:
            await callback.answer("Оплату отмечает ответственный за этап.", show_alert=True)
            return
        await edit_message(
            callback,
            f"Заказ №{order.order_id} — оплата:",
            payment_keyboard(order.order_id, order.payment_status),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ord:payset:"))
    async def on_payment_set(
        callback: CallbackQuery, db: Database, owner_chat_id: int, sheet: SheetClient,
        shoots_sheet: SheetClient, runtime: RuntimeState, deadlines: dict[str, int],
    ) -> None:
        _, _, order_id, option_index = callback.data.split(":")
        order = await load_or_alert(callback, db, order_id)
        if order is None:
            return
        access = await get_order_access(db, callback.from_user.id, owner_chat_id, order)
        if "payment_status" not in access.editable_fields:
            await callback.answer("Оплату отмечает ответственный за этап.", show_alert=True)
            return
        option = PAYMENT_OPTIONS[int(option_index)]
        updated = await write(
            callback, db, sheet, shoots_sheet, runtime, order, values={"payment_status": option}
        )
        if updated is None:
            return
        await show_card(callback, db, owner_chat_id, updated, deadlines)
        await callback.answer(f"Оплата: {option}")

    @router.callback_query(F.data.startswith("ord:del:"))
    async def on_delete_ask(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Удалять заказы может только владелец.", show_alert=True)
            return
        order = await load_or_alert(callback, db, callback.data.split(":")[2])
        if order is None:
            return
        shoots = await db.list_shoots_for_order(order.order_id)
        taken = sum(1 for shoot in shoots if shoot.photographer)
        warning = f"\nФотографам ({taken}) придёт сообщение об отмене съёмок." if taken else ""
        confirm = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button("🗑 Да, удалить", f"ord:delok:{order.order_id}"),
                    button("Нет", f"ord:open:{order.order_id}"),
                ]
            ]
        )
        await edit_message(
            callback,
            f"Удалить заказ №{order.order_id} — {order.school}?\n"
            f"Удалятся строка в таблице, съёмки ({len(shoots)}) и задачи в To Do. "
            f"Отменить это нельзя.{warning}",
            confirm,
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ord:delok:"))
    async def on_delete(
        callback: CallbackQuery,
        db: Database,
        owner_chat_id: int,
        sheet: SheetClient,
        shoots_sheet: SheetClient,
        runtime: RuntimeState,
        graph: "GraphTodoClient | None",
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Удалять заказы может только владелец.", show_alert=True)
            return
        order_id = callback.data.split(":")[2]
        order = await db.get_cached_order(order_id)
        school = order.school if order else ""
        try:
            result = await delete_order(db, sheet, shoots_sheet, order_id, graph=graph)
        except Exception:
            log.exception("Не удалось удалить заказ %s", order_id)
            await callback.answer(WRITE_FAILED, show_alert=True)
            return
        runtime.poll_now.set()
        for chat_id, shoot in result.notify:
            if chat_id == callback.from_user.id:
                continue
            try:
                await callback.bot.send_message(
                    chat_id,
                    f"❌ Заказ отменён, съёмки не будет: {school}\n"
                    f"📅 {format_when(shoot.date, shoot.time)}\n📍 {shoot.location}",
                )
            except Exception:
                log.exception("Не удалось сообщить фотографу об удалении заказа %s", order_id)
        text, keyboard = await orders_home_view(db, callback.from_user.id, owner_chat_id)
        await edit_message(callback, f"🗑 Заказ №{order_id} удалён.\n\n{text}", keyboard)
        await callback.answer("Заказ удалён")

    @router.message(OrderInputStates.value, F.text)
    async def on_value(
        message: Message,
        db: Database,
        owner_chat_id: int,
        sheet: SheetClient,
        shoots_sheet: SheetClient,
        runtime: RuntimeState,
        state: FSMContext,
        deadlines: dict[str, int],
    ) -> None:
        data = await state.get_data()
        pending = data["pending"]
        field, required = pending[0]
        editing = data["target"] is None
        text = message.text.strip()

        if text == BTN_SKIP:
            if required:
                await message.answer(f"Это нужно заполнить — напиши значение или нажми «{BTN_CANCEL}».")
                return
            value = None
        elif editing and text == "-":
            value = ""
        elif field in LINK_FIELDS and not looks_like_link(text):
            skip_hint = "" if editing else f", нажми «{BTN_SKIP}»"
            await message.answer(
                f"Это не похоже на ссылку — пришли ссылку на диск (https://…){skip_hint} или «{BTN_CANCEL}»."
            )
            return
        else:
            value = text

        values = dict(data["values"])
        if value is not None:
            values[field] = value
        pending = pending[1:]
        if pending:
            await state.update_data(pending=pending, values=values)
            next_field, next_required = pending[0]
            await message.answer(
                field_prompt(next_field),
                reply_markup=input_reply_keyboard(optional=not next_required),
            )
            return

        await state.clear()
        chat_id = message.chat.id
        keyboard = await reply_keyboard_for(db, chat_id, owner_chat_id)
        order = await db.get_cached_order(data["order_id"])
        if order is None:
            await message.answer("Заказ не найден — возможно, его удалили из таблицы.", reply_markup=keyboard)
            return
        if not editing and stage_index(order.status) != data["from_index"]:
            await message.answer(
                f"Пока ты отвечал, заказ перевели на «{order.status}» — ничего не менял.",
                reply_markup=keyboard,
            )
        else:
            try:
                order = await apply_order_update(
                    db, sheet, order, values=values, status=data["target"], shoots_sheet=shoots_sheet
                )
            except Exception:
                log.exception("Не удалось записать заказ %s в таблицу", order.order_id)
                await message.answer(WRITE_FAILED, reply_markup=keyboard)
                return
            runtime.poll_now.set()
            done = "✅ Сохранено." if editing else f"✅ Заказ переведён на «{data['target']}»."
            if not editing and any(field in values for field in LINK_FIELDS):
                done += "\nСсылка уйдёт следующему этапу вместе с уведомлением."
            if editing and field in LINK_FIELDS and values.get(field):
                sent = await notify_link_added(
                    message.bot, db, order, field, sender_chat_id=chat_id
                )
                if sent:
                    done += f"\nСсылку получили: {sent} чел."
            await message.answer(done, reply_markup=keyboard)

        text, card_keyboard = await render_card(db, chat_id, owner_chat_id, order, deadlines=deadlines)
        await message.answer(text, reply_markup=card_keyboard)

    return router
