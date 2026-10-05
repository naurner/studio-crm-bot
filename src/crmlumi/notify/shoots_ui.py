"""Съёмки заказа в боте: список, карточка съёмки, добавление и перенос
(дата и время кнопками или текстом), локация, «Взять» / «Отказаться»,
назначение фотографа владельцем.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from ..db import Database
from ..host import RuntimeState
from ..models import Order, Shoot
from ..pipeline.shoots import (
    PHOTO_STAGES,
    add_shoot,
    assign_shoot,
    claim_shoot,
    delete_shoot,
    photographer_chat_id,
    release_shoot,
    sort_shoots,
    update_shoot,
)
from ..pipeline.stages import apply_order_update, get_order_access, shoots_problem, stage_index
from ..roles import ROLE_PHOTOGRAPHER
from ..sheet_source import SheetClient
from ..statuses import STATUS_AGREED, STATUS_DATE_LOCATION_SET, STATUS_SEEKING_PHOTOGRAPHER
from ..timefmt import format_day, format_short, format_when, parse_datetime_text, parse_time_text
from .access import reply_keyboard_for
from .claim_messages import broadcast_shoot_offer, finalize_shoot_broadcast
from .datetime_picker import calendar_keyboard, hours_keyboard, minutes_keyboard, parse_day_callback
from .menus import BTN_CANCEL
from .messaging import edit_message
from .order_card import button, merge_shoot, render_card

log = logging.getLogger(__name__)

WRITE_FAILED = "❌ Не получилось записать в таблицу. Попробуй ещё раз чуть позже."
DATE_PROMPT = (
    "Выбери день в календаре или напиши текстом: «12.10 10:00», «завтра 15:30», "
    "«12 октября в 10:00»."
)
STALE_MENU = "Это меню устарело — открой съёмку заново."


class ShootStates(StatesGroup):
    date = State()
    time = State()
    location = State()


def shoots_list_text(order: Order, shoots: list[Shoot]) -> str:
    lines = [f"📍 Съёмки заказа №{order.order_id} — {order.school}"]
    if not shoots:
        lines.append("Съёмок пока нет.")
    for i, shoot in enumerate(sort_shoots(shoots), start=1):
        lines.append(f"{i}. {format_when(shoot.date, shoot.time)}")
        lines.append(f"    📍 {shoot.location or 'локация не указана'} · 📸 {shoot.photographer or '❗ нет фотографа'}")
    if order.status == STATUS_AGREED and shoots:
        lines.append("\nВсе съёмки добавлены? Жми «✅ Готово».")
    return "\n".join(lines)


def shoots_list_keyboard(
    order: Order, shoots: list[Shoot], *, can_edit: bool, can_finish: bool
) -> InlineKeyboardMarkup:
    rows = []
    for i, shoot in enumerate(sort_shoots(shoots), start=1):
        label = f"{i}. {format_short(shoot.date, shoot.time)} · {shoot.location or '?'}"
        if not shoot.photographer:
            label += " ❗"
        rows.append([button(label[:60], f"sh:open:{shoot.shoot_id}")])
    if can_edit:
        rows.append([button("➕ Добавить съёмку", f"sh:add:{order.order_id}")])
    if can_finish:
        index = stage_index(order.status)
        rows.append([button("✅ Съёмки назначены", f"sh:done:{order.order_id}:{index}")])
    rows.append([button("← К заказу", f"ord:open:{order.order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shoot_card_text(order: Order, shoot: Shoot) -> str:
    return "\n".join(
        [
            f"📍 Съёмка {shoot.shoot_id} — заказ №{order.order_id}, {order.school}",
            f"📅 {format_when(shoot.date, shoot.time)}",
            f"📍 {shoot.location or 'локация не указана'}",
            f"📸 Фотограф: {shoot.photographer or '❗ ещё не назначен'}",
        ]
    )


def shoot_card_keyboard(
    shoot: Shoot,
    *,
    can_edit: bool,
    can_claim: bool,
    is_claimant: bool,
    is_owner: bool,
    searching: bool,
) -> InlineKeyboardMarkup:
    shoot_id = shoot.shoot_id
    rows = []
    if can_claim:
        rows.append([button("✋ Взять съёмку", f"sh:claim:{shoot_id}")])
    if is_claimant:
        rows.append([button("🚫 Отказаться", f"sh:decline:{shoot_id}")])
    if can_edit:
        rows.append(
            [button("📅 Дата и время", f"sh:when:{shoot_id}"), button("📍 Локация", f"sh:where:{shoot_id}")]
        )
    if is_owner:
        # Отдельными рядами: вдвоём в ряду на телефоне подписи не влезают.
        rows.append([button("👤 Назначить фотографа", f"sh:assign:{shoot_id}")])
        if searching and not shoot.photographer:
            rows.append([button("📣 Разослать фотографам", f"sh:rebr:{shoot_id}")])
    if can_edit:
        rows.append([button("🗑 Удалить съёмку", f"sh:del:{shoot_id}")])
    rows.append([button("← Съёмки", f"sh:list:{shoot.order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def location_keyboard(suggestions: list[str]) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=location)] for location in suggestions[:4]]
    rows.append([KeyboardButton(text=BTN_CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def location_suggestions(db: Database, order_id: str) -> list[str]:
    """Сначала локации этого заказа, потом недавние из других."""
    own, others = [], []
    for shoot in await db.list_cached_shoots():
        if not shoot.location:
            continue
        target = own if shoot.order_id == order_id else others
        if shoot.location not in own and shoot.location not in others:
            target.append(shoot.location)
    return own + others[::-1]


async def start_add_shoot(send, state: FSMContext, order_id: str, *, title: str | None = None) -> None:
    """Мастер новой съёмки: дата -> время -> локация.

    Два сообщения: у одного сообщения может быть только одна клавиатура, а
    нужны и «✖️ Отмена» внизу, и календарь в сообщении.
    """
    await state.set_state(ShootStates.date)
    await state.update_data(mode="add", order_id=order_id, shoot_id=None, date=None)
    today = date.today()
    await send(title or f"Новая съёмка заказа №{order_id}.", reply_markup=location_keyboard([]))
    await send(f"📅 Когда съёмка?\n{DATE_PROMPT}", reply_markup=calendar_keyboard(today.year, today.month, today=today))


async def _can_claim(db: Database, chat_id: int, order: Order, shoot: Shoot) -> bool:
    if order.status not in PHOTO_STAGES or shoot.photographer:
        return False
    if await db.get_shoot_claim(shoot.shoot_id) is not None:
        return False
    return chat_id in await db.get_role_assignees(ROLE_PHOTOGRAPHER)


async def render_shoots_list(
    db: Database, chat_id: int, owner_chat_id: int, order: Order, *, changed: Shoot | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    shoots = merge_shoot(await db.list_shoots_for_order(order.order_id), changed)
    access = await get_order_access(db, chat_id, owner_chat_id, order)
    can_finish = order.status == STATUS_AGREED and access.can_advance and bool(shoots)
    return shoots_list_text(order, shoots), shoots_list_keyboard(
        order, shoots, can_edit=access.can_edit_shoots, can_finish=can_finish
    )


async def render_shoot_card(
    db: Database, chat_id: int, owner_chat_id: int, order: Order, shoot: Shoot
) -> tuple[str, InlineKeyboardMarkup]:
    access = await get_order_access(db, chat_id, owner_chat_id, order)
    return shoot_card_text(order, shoot), shoot_card_keyboard(
        shoot,
        can_edit=access.can_edit_shoots,
        can_claim=await _can_claim(db, chat_id, order, shoot),
        is_claimant=await photographer_chat_id(db, shoot) == chat_id and bool(shoot.photographer),
        is_owner=access.is_owner,
        searching=order.status == STATUS_SEEKING_PHOTOGRAPHER,
    )


def build_shoots_router() -> Router:
    router = Router()

    async def load(callback: CallbackQuery, db: Database, shoot_id: str) -> tuple[Order, Shoot] | None:
        shoot = await db.get_cached_shoot(shoot_id)
        order = await db.get_cached_order(shoot.order_id) if shoot else None
        if shoot is None or order is None:
            await callback.answer(
                "Съёмка не найдена — если её только что добавили или удалили, подожди пару секунд.",
                show_alert=True,
            )
            return None
        return order, shoot

    async def load_order(callback: CallbackQuery, db: Database, order_id: str) -> Order | None:
        order = await db.get_cached_order(order_id)
        if order is None:
            await callback.answer("Заказ не найден.", show_alert=True)
        return order

    # --- Список и карточка ------------------------------------------------

    @router.callback_query(F.data.startswith("sh:list:"))
    async def on_list(callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext) -> None:
        await state.clear()
        order = await load_order(callback, db, callback.data.split(":")[2])
        if order is None:
            return
        text, keyboard = await render_shoots_list(db, callback.from_user.id, owner_chat_id, order)
        await edit_message(callback, text, keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("sh:open:"))
    async def on_open(callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext) -> None:
        await state.clear()
        loaded = await load(callback, db, callback.data.split(":", 2)[2])
        if loaded is None:
            return
        text, keyboard = await render_shoot_card(db, callback.from_user.id, owner_chat_id, *loaded)
        await edit_message(callback, text, keyboard)
        await callback.answer()

    # --- Добавление и перенос: дата -> время -> локация ------------------

    @router.callback_query(F.data.startswith("sh:add:"))
    async def on_add(callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext) -> None:
        order = await load_order(callback, db, callback.data.split(":")[2])
        if order is None:
            return
        access = await get_order_access(db, callback.from_user.id, owner_chat_id, order)
        if not access.can_edit_shoots:
            await callback.answer("Съёмки назначает ответственный за даты или владелец.", show_alert=True)
            return
        await callback.answer()
        await start_add_shoot(callback.message.answer, state, order.order_id)

    @router.callback_query(F.data.startswith("sh:when:"))
    async def on_when(callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext) -> None:
        loaded = await load(callback, db, callback.data.split(":", 2)[2])
        if loaded is None:
            return
        order, shoot = loaded
        if not (await get_order_access(db, callback.from_user.id, owner_chat_id, order)).can_edit_shoots:
            await callback.answer("Переносить съёмки может ответственный за даты или владелец.", show_alert=True)
            return
        await state.set_state(ShootStates.date)
        await state.update_data(mode="when", order_id=order.order_id, shoot_id=shoot.shoot_id, date=None)
        today = date.today()
        await callback.answer()
        await callback.message.answer(
            f"Перенос съёмки {shoot.shoot_id}. Сейчас: {format_when(shoot.date, shoot.time)}",
            reply_markup=location_keyboard([]),
        )
        await callback.message.answer(
            DATE_PROMPT, reply_markup=calendar_keyboard(today.year, today.month, today=today)
        )

    @router.callback_query(ShootStates.date, F.data.startswith("dp:cal:"))
    async def on_calendar_page(callback: CallbackQuery) -> None:
        raw = callback.data.rsplit(":", 1)[1]
        today = date.today()
        keyboard = calendar_keyboard(int(raw[:4]), int(raw[4:6]), today=today)
        await callback.message.edit_reply_markup(reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(ShootStates.date, F.data.startswith("dp:day:"))
    async def on_day(callback: CallbackQuery, state: FSMContext) -> None:
        day = parse_day_callback(callback.data)
        if day is None:
            await callback.answer()
            return
        await state.update_data(date=day.isoformat())
        await state.set_state(ShootStates.time)
        await edit_message(
            callback, f"📅 {format_day(day.isoformat())}\n🕐 Во сколько? Выбери час или напиши «10:30».",
            hours_keyboard(),
        )
        await callback.answer()

    @router.callback_query(ShootStates.time, F.data == "dp:back")
    async def on_back_to_calendar(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ShootStates.date)
        today = date.today()
        await edit_message(callback, DATE_PROMPT, calendar_keyboard(today.year, today.month, today=today))
        await callback.answer()

    @router.callback_query(ShootStates.time, F.data.startswith("dp:h:"))
    async def on_hour(callback: CallbackQuery) -> None:
        await callback.message.edit_reply_markup(reply_markup=minutes_keyboard(callback.data.rsplit(":", 1)[1]))
        await callback.answer()

    @router.callback_query(ShootStates.time, F.data == "dp:hours")
    async def on_hours(callback: CallbackQuery) -> None:
        await callback.message.edit_reply_markup(reply_markup=hours_keyboard())
        await callback.answer()

    async def time_chosen(
        send, chat_id: int, time_text: str, *, db, owner_chat_id, state, shoots_sheet, runtime
    ) -> None:
        data = await state.get_data()
        if data["mode"] == "add":
            await state.update_data(time=time_text)
            await state.set_state(ShootStates.location)
            await send(
                f"📅 {format_when(data['date'], time_text)}\n📍 Где съёмка? Напиши адрес или название места.",
                reply_markup=location_keyboard(await location_suggestions(db, data["order_id"])),
            )
            return

        await state.clear()
        keyboard = await reply_keyboard_for(db, chat_id, owner_chat_id)
        shoot = await db.get_cached_shoot(data["shoot_id"])
        order = await db.get_cached_order(data["order_id"])
        if shoot is None or order is None:
            await send("Съёмка не найдена — возможно, её удалили.", reply_markup=keyboard)
            return
        try:
            shoot = await update_shoot(shoots_sheet, shoot, date=data["date"], time=time_text)
        except Exception:
            log.exception("Не удалось перенести съёмку %s", shoot.shoot_id)
            await send(WRITE_FAILED, reply_markup=keyboard)
            return
        runtime.poll_now.set()
        note = "\nФотограф получит сообщение о переносе." if shoot.photographer else ""
        await send(f"✅ Перенесено на {format_when(shoot.date, shoot.time)}.{note}", reply_markup=keyboard)
        text, card = await render_shoot_card(db, chat_id, owner_chat_id, order, shoot)
        await send(text, reply_markup=card)

    @router.callback_query(ShootStates.time, F.data.startswith("dp:m:"))
    async def on_minute(
        callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext,
        shoots_sheet: SheetClient, runtime: RuntimeState,
    ) -> None:
        _, _, hour, minute = callback.data.split(":")
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await time_chosen(
            callback.message.answer, callback.from_user.id, f"{hour}:{minute}",
            db=db, owner_chat_id=owner_chat_id, state=state, shoots_sheet=shoots_sheet, runtime=runtime,
        )

    @router.callback_query(ShootStates.time, F.data == "dp:notime")
    async def on_no_time(
        callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext,
        shoots_sheet: SheetClient, runtime: RuntimeState,
    ) -> None:
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await time_chosen(
            callback.message.answer, callback.from_user.id, "",
            db=db, owner_chat_id=owner_chat_id, state=state, shoots_sheet=shoots_sheet, runtime=runtime,
        )

    @router.message(ShootStates.date, F.text)
    async def on_date_text(
        message: Message, db: Database, owner_chat_id: int, state: FSMContext,
        shoots_sheet: SheetClient, runtime: RuntimeState,
    ) -> None:
        day, clock = parse_datetime_text(message.text, today=date.today())
        if day is None:
            await message.answer(f"Не понял дату. {DATE_PROMPT}")
            return
        await state.update_data(date=day)
        if clock is None:
            await state.set_state(ShootStates.time)
            await message.answer(
                f"📅 {format_day(day)}\n🕐 Во сколько? Выбери час или напиши «10:30».",
                reply_markup=hours_keyboard(),
            )
            return
        await time_chosen(
            message.answer, message.chat.id, clock,
            db=db, owner_chat_id=owner_chat_id, state=state, shoots_sheet=shoots_sheet, runtime=runtime,
        )

    @router.message(ShootStates.time, F.text)
    async def on_time_text(
        message: Message, db: Database, owner_chat_id: int, state: FSMContext,
        shoots_sheet: SheetClient, runtime: RuntimeState,
    ) -> None:
        text = message.text.strip().lower()
        if text in ("без времени", "-"):
            clock = ""
        else:
            day, clock = parse_datetime_text(text, today=date.today())
            if clock is None:
                clock = parse_time_text(text)
            if clock is None:
                await message.answer("Не понял время. Напиши, например, «10:30» или выбери час кнопкой.")
                return
            if day is not None:
                await state.update_data(date=day)
        await time_chosen(
            message.answer, message.chat.id, clock,
            db=db, owner_chat_id=owner_chat_id, state=state, shoots_sheet=shoots_sheet, runtime=runtime,
        )

    @router.callback_query(F.data.startswith("sh:where:"))
    async def on_where(callback: CallbackQuery, db: Database, owner_chat_id: int, state: FSMContext) -> None:
        loaded = await load(callback, db, callback.data.split(":", 2)[2])
        if loaded is None:
            return
        order, shoot = loaded
        if not (await get_order_access(db, callback.from_user.id, owner_chat_id, order)).can_edit_shoots:
            await callback.answer("Менять съёмки может ответственный за даты или владелец.", show_alert=True)
            return
        await state.set_state(ShootStates.location)
        await state.update_data(mode="where", order_id=order.order_id, shoot_id=shoot.shoot_id)
        await callback.answer()
        await callback.message.answer(
            f"📍 Новая локация для съёмки {shoot.shoot_id}? Сейчас: {shoot.location or '—'}",
            reply_markup=location_keyboard(await location_suggestions(db, order.order_id)),
        )

    @router.message(ShootStates.location, F.text)
    async def on_location_text(
        message: Message, db: Database, owner_chat_id: int, state: FSMContext,
        shoots_sheet: SheetClient, runtime: RuntimeState,
    ) -> None:
        location = message.text.strip()
        data = await state.get_data()
        await state.clear()
        chat_id = message.chat.id
        keyboard = await reply_keyboard_for(db, chat_id, owner_chat_id)
        order = await db.get_cached_order(data["order_id"])
        if order is None:
            await message.answer("Заказ не найден — возможно, его удалили.", reply_markup=keyboard)
            return
        try:
            if data["mode"] == "add":
                shoot = await add_shoot(
                    shoots_sheet, order.order_id, date=data["date"], time=data["time"], location=location
                )
            else:
                shoot = await db.get_cached_shoot(data["shoot_id"])
                if shoot is None:
                    await message.answer("Съёмка не найдена — возможно, её удалили.", reply_markup=keyboard)
                    return
                shoot = await update_shoot(shoots_sheet, shoot, location=location)
        except Exception:
            log.exception("Не удалось записать съёмку заказа %s", order.order_id)
            await message.answer(WRITE_FAILED, reply_markup=keyboard)
            return
        runtime.poll_now.set()

        if data["mode"] == "add":
            await message.answer(f"✅ Съёмка добавлена: {format_when(shoot.date, shoot.time)}, {location}.", reply_markup=keyboard)
            text, markup = await render_shoots_list(db, chat_id, owner_chat_id, order, changed=shoot)
        else:
            await message.answer(f"✅ Локация: {location}.", reply_markup=keyboard)
            text, markup = await render_shoot_card(db, chat_id, owner_chat_id, order, shoot)
        await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data == "dp:noop")
    async def on_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(F.data.startswith("dp:"))
    async def on_stale_picker(callback: CallbackQuery) -> None:
        await callback.answer(STALE_MENU, show_alert=True)

    # --- Готово, удаление ------------------------------------------------

    @router.callback_query(F.data.startswith("sh:done:"))
    async def on_done(
        callback: CallbackQuery, db: Database, owner_chat_id: int, sheet: SheetClient,
        runtime: RuntimeState, deadlines: dict[str, int],
    ) -> None:
        _, _, order_id, from_index = callback.data.split(":")
        order = await load_order(callback, db, order_id)
        if order is None:
            return
        if str(stage_index(order.status)) != from_index:
            await callback.answer(f"Заказ уже на этапе «{order.status}».", show_alert=True)
            return
        if not (await get_order_access(db, callback.from_user.id, owner_chat_id, order)).can_advance:
            await callback.answer("Этот этап ведёт другой человек.", show_alert=True)
            return
        problem = shoots_problem(STATUS_DATE_LOCATION_SET, await db.list_shoots_for_order(order_id))
        if problem:
            await callback.answer(
                f"{problem}\nЕсли только что добавил — подожди пару секунд, таблица обновляется.",
                show_alert=True,
            )
            return
        try:
            order = await apply_order_update(db, sheet, order, status=STATUS_DATE_LOCATION_SET)
        except Exception:
            log.exception("Не удалось перевести заказ %s", order_id)
            await callback.answer(WRITE_FAILED, show_alert=True)
            return
        runtime.poll_now.set()
        text, keyboard = await render_card(db, callback.from_user.id, owner_chat_id, order, deadlines=deadlines)
        await edit_message(callback, text, keyboard)
        await callback.answer(f"Готово: «{STATUS_DATE_LOCATION_SET}»")

    @router.callback_query(F.data.startswith("sh:del:"))
    async def on_delete_ask(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        loaded = await load(callback, db, callback.data.split(":", 2)[2])
        if loaded is None:
            return
        order, shoot = loaded
        if not (await get_order_access(db, callback.from_user.id, owner_chat_id, order)).can_edit_shoots:
            await callback.answer("Удалять съёмки может ответственный за даты или владелец.", show_alert=True)
            return
        confirm = InlineKeyboardMarkup(
            inline_keyboard=[
                [button("🗑 Да, удалить", f"sh:delok:{shoot.shoot_id}"), button("Нет", f"sh:open:{shoot.shoot_id}")]
            ]
        )
        warning = "\nФотограф получит сообщение об отмене." if shoot.photographer else ""
        await edit_message(callback, f"Удалить съёмку {format_when(shoot.date, shoot.time)}, {shoot.location}?{warning}", confirm)
        await callback.answer()

    @router.callback_query(F.data.startswith("sh:delok:"))
    async def on_delete(
        callback: CallbackQuery, db: Database, owner_chat_id: int, sheet: SheetClient,
        shoots_sheet: SheetClient, runtime: RuntimeState,
    ) -> None:
        loaded = await load(callback, db, callback.data.split(":", 2)[2])
        if loaded is None:
            return
        order, shoot = loaded
        if not (await get_order_access(db, callback.from_user.id, owner_chat_id, order)).can_edit_shoots:
            await callback.answer("Удалять съёмки может ответственный за даты или владелец.", show_alert=True)
            return
        remaining = [s for s in await db.list_shoots_for_order(order.order_id) if s.shoot_id != shoot.shoot_id]
        try:
            await delete_shoot(db, shoots_sheet, sheet, shoot, order=order, remaining=remaining)
        except Exception:
            log.exception("Не удалось удалить съёмку %s", shoot.shoot_id)
            await callback.answer(WRITE_FAILED, show_alert=True)
            return
        runtime.poll_now.set()
        text = shoots_list_text(order, remaining)
        access = await get_order_access(db, callback.from_user.id, owner_chat_id, order)
        keyboard = shoots_list_keyboard(
            order, remaining, can_edit=access.can_edit_shoots,
            can_finish=order.status == STATUS_AGREED and access.can_advance and bool(remaining),
        )
        await edit_message(callback, text, keyboard)
        await callback.answer("Съёмка удалена")

    # --- Фотографы ---------------------------------------------------------

    async def do_claim(callback: CallbackQuery, db, owner_chat_id, shoots_sheet, runtime, shoot_id, *, from_card: bool):
        shoot = await db.get_cached_shoot(shoot_id)
        order = await db.get_cached_order(shoot.order_id) if shoot else None
        if shoot is None or order is None or not await _can_claim(db, callback.from_user.id, order, shoot):
            claimant = await db.get_shoot_claim(shoot_id) if shoot else None
            if claimant is not None and claimant != callback.from_user.id:
                name = await db.get_user_display_name(claimant)
                await callback.answer(f"Уже взял(а): {name}", show_alert=True)
            else:
                await callback.answer("Эта съёмка уже не ищет фотографа.", show_alert=True)
            return
        try:
            winner = await claim_shoot(db, shoots_sheet, shoot, callback.from_user.id)
        except Exception:
            log.exception("Не удалось записать claim съёмки %s", shoot_id)
            await db.clear_shoot_claim(shoot_id)
            await callback.answer(WRITE_FAILED, show_alert=True)
            return
        await finalize_shoot_broadcast(callback.bot, db, order, shoot, winner)
        runtime.poll_now.set()
        if winner != callback.from_user.id:
            await callback.answer(f"Уже взял(а): {await db.get_user_display_name(winner)}", show_alert=True)
            return
        await callback.answer("Съёмка твоя ✅")
        if from_card:
            taken = replace(shoot, photographer=await db.get_user_display_name(winner))
            text, keyboard = await render_shoot_card(db, callback.from_user.id, owner_chat_id, order, taken)
            await edit_message(callback, text, keyboard)

    @router.callback_query(F.data.startswith("claim:"))
    async def on_claim_from_offer(
        callback: CallbackQuery, db: Database, owner_chat_id: int, shoots_sheet: SheetClient, runtime: RuntimeState
    ) -> None:
        await do_claim(callback, db, owner_chat_id, shoots_sheet, runtime, callback.data.split(":", 1)[1], from_card=False)

    @router.callback_query(F.data.startswith("sh:claim:"))
    async def on_claim_from_card(
        callback: CallbackQuery, db: Database, owner_chat_id: int, shoots_sheet: SheetClient, runtime: RuntimeState
    ) -> None:
        await do_claim(callback, db, owner_chat_id, shoots_sheet, runtime, callback.data.split(":", 2)[2], from_card=True)

    @router.callback_query(F.data.startswith("skip:"))
    async def on_skip(callback: CallbackQuery) -> None:
        await edit_message(callback, "Хорошо, пропущено.")
        await callback.answer()

    @router.callback_query(F.data.startswith("sh:decline:"))
    async def on_decline(
        callback: CallbackQuery, db: Database, owner_chat_id: int, shoots_sheet: SheetClient, runtime: RuntimeState
    ) -> None:
        loaded = await load(callback, db, callback.data.split(":", 2)[2])
        if loaded is None:
            return
        order, shoot = loaded
        if not shoot.photographer or await photographer_chat_id(db, shoot) != callback.from_user.id:
            await callback.answer("Эта съёмка уже не твоя.", show_alert=True)
            return
        try:
            shoot = await release_shoot(db, shoots_sheet, shoot)
        except Exception:
            log.exception("Не удалось снять фотографа со съёмки %s", shoot.shoot_id)
            await callback.answer(WRITE_FAILED, show_alert=True)
            return
        # Рассылку другим фотографам сделает сверка после опроса.
        runtime.poll_now.set()
        text, keyboard = await render_shoot_card(db, callback.from_user.id, owner_chat_id, order, shoot)
        await edit_message(callback, text, keyboard)
        await callback.answer("Ты отказался — предложу съёмку другим фотографам.")

    @router.callback_query(F.data.startswith("sh:assign:"))
    async def on_assign_menu(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Это только для владельца.", show_alert=True)
            return
        loaded = await load(callback, db, callback.data.split(":", 2)[2])
        if loaded is None:
            return
        _, shoot = loaded
        photographers = await db.get_role_assignees(ROLE_PHOTOGRAPHER)
        if not photographers:
            await callback.answer("В роли «Фотограф» пока никого нет — назначь в 👥 Роли.", show_alert=True)
            return
        rows = [
            [button(await db.get_user_display_name(chat_id), f"sh:assignto:{shoot.shoot_id}:{chat_id}")]
            for chat_id in photographers
        ]
        rows.append([button("← К съёмке", f"sh:open:{shoot.shoot_id}")])
        await edit_message(
            callback, f"Кого поставить на съёмку {format_when(shoot.date, shoot.time)}?",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("sh:assignto:"))
    async def on_assign(
        callback: CallbackQuery, db: Database, owner_chat_id: int, shoots_sheet: SheetClient, runtime: RuntimeState
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Это только для владельца.", show_alert=True)
            return
        _, _, shoot_id, chat_id = callback.data.split(":")
        loaded = await load(callback, db, shoot_id)
        if loaded is None:
            return
        order, shoot = loaded
        try:
            shoot = await assign_shoot(db, shoots_sheet, shoot, int(chat_id))
        except Exception:
            log.exception("Не удалось назначить фотографа на съёмку %s", shoot_id)
            await callback.answer(WRITE_FAILED, show_alert=True)
            return
        runtime.poll_now.set()
        try:
            await callback.bot.send_message(
                int(chat_id),
                f"📸 Тебя поставили на съёмку: {order.school}\n📅 {format_when(shoot.date, shoot.time)}\n📍 {shoot.location}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[button("📋 Открыть заказ", f"ord:show:{order.order_id}")]]
                ),
            )
        except Exception:
            log.exception("Не удалось сообщить фотографу о назначении")
        text, keyboard = await render_shoot_card(db, callback.from_user.id, owner_chat_id, order, shoot)
        await edit_message(callback, text, keyboard)
        await callback.answer(f"Назначен(а): {shoot.photographer}")

    @router.callback_query(F.data.startswith("sh:rebr:"))
    async def on_rebroadcast(callback: CallbackQuery, db: Database, owner_chat_id: int) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Это только для владельца.", show_alert=True)
            return
        loaded = await load(callback, db, callback.data.split(":", 2)[2])
        if loaded is None:
            return
        order, shoot = loaded
        if order.status != STATUS_SEEKING_PHOTOGRAPHER or shoot.photographer:
            await callback.answer("Эта съёмка уже не ищет фотографа.", show_alert=True)
            return
        sent = await broadcast_shoot_offer(callback.bot, db, order, shoot)
        await callback.answer(f"Разослал фотографам: {sent}.")

    return router
