"""Карточка заказа: текст и кнопки. Общая для экранов заказов и съёмок."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..db import Database
from ..models import Order, Shoot
from ..pipeline.shoots import shoot_line, sort_shoots
from ..pipeline.stages import (
    ADVANCE_LABELS,
    LINK_FIELDS,
    LINK_LABELS,
    OrderAccess,
    deadline_text,
    get_order_access,
    link_consumer_statuses,
    next_status,
    previous_status,
    stage_due_date,
    stage_index,
    stage_role_for_status,
)
from ..statuses import STATUS_SEEKING_PHOTOGRAPHER, STATUS_SHOOT_ASSIGNED
from ..timefmt import local_now

log = logging.getLogger(__name__)


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def open_order_keyboard(order_id: str) -> InlineKeyboardMarkup:
    """Кнопка под уведомлением — открыть карточку новым сообщением."""
    return InlineKeyboardMarkup(inline_keyboard=[[button("📋 Открыть заказ", f"ord:show:{order_id}")]])


def merge_shoot(shoots: list[Shoot], changed: Shoot | None) -> list[Shoot]:
    """Кэш + только что записанная ботом съёмка (кэш обновится со следующим опросом)."""
    if changed is None:
        return shoots
    return [shoot for shoot in shoots if shoot.shoot_id != changed.shoot_id] + [changed]


def format_order_card(
    order: Order,
    *,
    shoots: list[Shoot],
    days_in_stage: int | None,
    responsible: str | None,
    deadline: str | None = None,
) -> str:
    stage = order.status or "не указан"
    if days_in_stage is not None:
        stage += " (сегодня)" if days_in_stage == 0 else f" ({days_in_stage} дн.)"
    lines = [f"📋 Заказ №{order.order_id} — {order.school or 'без названия'}", f"Этап: {stage}"]
    if deadline:
        lines.append(f"⏱ Срок этапа: {deadline}")
    if responsible:
        lines.append(f"Сейчас отвечает: {responsible}")
    if order.status == STATUS_SEEKING_PHOTOGRAPHER:
        lines.append("Дальше: этап закроется сам, когда фотографы разберут все съёмки")
    elif order.status in ADVANCE_LABELS:
        lines.append(f"Дальше: {next_status(order.status)}")
    contact = ", ".join(part for part in (order.contact_name, order.contact_phone) if part)
    lines += [
        "",
        f"Контакт: {contact or '—'}",
        f"Человек: {order.people_count or '—'}",
        f"Сумма контракта: {order.contract_amount or '—'}",
        f"Оплата: {order.payment_status or '—'}",
    ]
    if order.notes:
        lines.append(f"Примечания: {order.notes}")
    links = [(LINK_LABELS[field], getattr(order, field)) for field in LINK_FIELDS if getattr(order, field)]
    if links:
        lines.append("")
        lines += [f"{label}: {link}" for label, link in links]
    if order.vignette_progress or order.vignette_link:
        lines.append("")
        if order.vignette_progress:
            lines.append(f"🎓 Виньетки: {order.vignette_progress}")
        if order.vignette_link:
            lines.append(f"Ссылка для учеников: {order.vignette_link}")
    lines.append("")
    if shoots:
        lines.append(f"Съёмки ({len(shoots)}):")
        lines += [f"{i}. {shoot_line(shoot)}" for i, shoot in enumerate(sort_shoots(shoots), start=1)]
    else:
        lines.append("Съёмок пока нет.")
    return "\n".join(lines)


def order_card_keyboard(order: Order, access: OrderAccess, *, shoots_count: int) -> InlineKeyboardMarkup:
    order_id = order.order_id
    index = stage_index(order.status)
    rows = []
    if access.can_advance and index is not None:
        rows.append([button(ADVANCE_LABELS[order.status], f"ord:adv:{order_id}:{index}")])
    rows.append([button(f"📍 Съёмки ({shoots_count})", f"sh:list:{order_id}")])

    edit_row = []
    if any(field != "payment_status" for field in access.editable_fields):
        edit_row.append(button("✏️ Изменить", f"ord:edit:{order_id}"))
    if "payment_status" in access.editable_fields:
        edit_row.append(button("💰 Оплата", f"ord:pay:{order_id}"))
    if edit_row:
        rows.append(edit_row)

    if access.is_owner:
        owner_row = []
        if index is not None and previous_status(order.status) is not None:
            owner_row.append(button("⬅️ Этап назад", f"ord:back:{order_id}:{index}"))
        owner_row.append(button("🔀 Выбрать этап", f"ord:pick:{order_id}"))
        rows.append(owner_row)
        rows.append([button("🗑 Удалить заказ", f"ord:del:{order_id}")])

    rows.append([button("🔄 Обновить", f"ord:open:{order_id}"), button("← Заказы", "ord:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_link_added(bot, db: Database, order: Order, field: str, *, sender_chat_id: int) -> int:
    """Ссылку добавили, когда заказ уже на этапе, которому она нужна, —
    сразу отправить её тем, кто этот этап ведёт. Возвращает, скольким ушло."""
    if order.status not in link_consumer_statuses(field) or not getattr(order, field):
        return 0
    role = stage_role_for_status(order.status)
    sent = 0
    for chat_id in await db.get_role_assignees(role) if role else []:
        if chat_id == sender_chat_id:
            continue
        try:
            await bot.send_message(
                chat_id,
                f"🔗 Заказ №{order.order_id}, {order.school}\n{LINK_LABELS[field]}: {getattr(order, field)}",
                reply_markup=open_order_keyboard(order.order_id),
            )
            sent += 1
        except Exception:
            log.exception("Не удалось отправить ссылку по заказу %s в чат %s", order.order_id, chat_id)
    return sent


async def responsible_text(db: Database, order: Order) -> str | None:
    if order.status == STATUS_SHOOT_ASSIGNED and order.photographer:
        return order.photographer
    role = stage_role_for_status(order.status)
    if role is None:
        return None
    names = [await db.get_user_display_name(chat_id) for chat_id in await db.get_role_assignees(role)]
    if not names:
        return f"никто — роль «{role}» не назначена"
    return f"{', '.join(names)} («{role}»)"


async def render_card(
    db: Database,
    chat_id: int,
    owner_chat_id: int,
    order: Order,
    *,
    deadlines: dict[str, int] | None = None,
    changed_shoot: Shoot | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    days = None
    deadline = None
    since = await db.get_status_since(order.order_id)
    if since is not None and since[0] == order.status:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(since[1])).days
        if deadlines:
            now = local_now()
            due = stage_due_date(order.status, since[1], deadlines, now.tzinfo)
            deadline = deadline_text(due, now.date()) if due else None
    shoots = merge_shoot(await db.list_shoots_for_order(order.order_id), changed_shoot)
    access = await get_order_access(db, chat_id, owner_chat_id, order)
    text = format_order_card(
        order,
        shoots=shoots,
        days_in_stage=days,
        responsible=await responsible_text(db, order),
        deadline=deadline,
    )
    return text, order_card_keyboard(order, access, shoots_count=len(shoots))
