"""Тексты и отправка напоминаний; кому и когда — в pipeline/reminders.py."""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from ..config import ReminderSettings
from ..db import Database
from ..models import ShootEvent
from ..pipeline.reminders import (
    PreShootReminder,
    ScheduleDigest,
    StageReminder,
    collect_pre_shoot_reminders,
    collect_schedule_digests,
    collect_stage_reminders,
    in_active_hours,
    STAGE_ACTIONS,
)
from ..pipeline.shoots import chat_id_by_name, photographer_chat_id
from ..timefmt import format_day, format_when
from .order_card import button

log = logging.getLogger(__name__)
MAX_ORDER_BUTTONS = 8


def orders_keyboard(order_ids_and_titles: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    rows, seen = [], set()
    for order_id, title in order_ids_and_titles:
        if order_id in seen:
            continue
        seen.add(order_id)
        rows.append([button(f"📋 №{order_id} {title}"[:60], f"ord:show:{order_id}")])
        if len(rows) == MAX_ORDER_BUTTONS:
            break
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def format_stage_reminder(items: list[StageReminder], deadlines: dict[str, int]) -> str:
    lines = ["⏰ Напоминание — ждут тебя:"]
    for item in items:
        order = item.order
        action = STAGE_ACTIONS.get(order.status, "сделать следующий шаг")
        line = f"• №{order.order_id} {order.school} — {order.status}, {item.days_in_stage} дн.: {action}"
        limit = deadlines.get(order.status)
        if limit and item.days_in_stage >= limit:
            line += " ⚠️ срок этапа истёк"
        lines.append(line)
    lines.append("\nЧастоту напоминаний можно поменять в «⚙️ Настройки».")
    return "\n".join(lines)


def _shoot_block(order, shoot, *, with_day: bool) -> list[str]:
    when = format_when(shoot.date, shoot.time) if with_day else (shoot.time or "время не указано")
    contact = ", ".join(part for part in (order.contact_name, order.contact_phone) if part)
    block = [f"• {when} — {shoot.location or 'локация не указана'}", f"   {order.school}"]
    if contact:
        block[-1] += f" · {contact}"
    return block


def format_schedule(digest: ScheduleDigest) -> str:
    if digest.weekly:
        title = f"📅 Твои съёмки на неделю {digest.day_from:%d.%m}–{digest.day_to:%d.%m}:"
    else:
        title = f"📅 Твои съёмки на завтра, {format_day(digest.day_from.isoformat())}:"
    lines = [title]
    for order, shoot in digest.items:
        lines += _shoot_block(order, shoot, with_day=digest.weekly)
    return "\n".join(lines)


def format_pre_shoot(reminder: PreShootReminder, hours: float) -> str:
    order, shoot = reminder.order, reminder.shoot
    hours_text = f"{hours:g}"
    lines = [f"⏰ Через {hours_text} ч съёмка"]
    lines += _shoot_block(order, shoot, with_day=True)
    if order.people_count:
        lines.append(f"   👥 {order.people_count} чел.")
    return "\n".join(lines)


async def _send(bot: Bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
    except Exception:
        log.exception("Не удалось отправить напоминание в чат %s", chat_id)


async def run_reminders(
    bot: Bot,
    db: Database,
    owner_chat_id: int,
    *,
    now: datetime,
    settings: ReminderSettings,
    deadlines: dict[str, int],
) -> None:
    if in_active_hours(now, settings):
        for user in await db.list_users():
            chat_id = user["chat_id"]
            hours = await db.get_reminder_hours(chat_id)
            interval = settings.default_interval_hours if hours is None else hours
            items = await collect_stage_reminders(
                db, chat_id, owner_chat_id, now=now, interval_hours=interval
            )
            if not items:
                continue
            keyboard = orders_keyboard([(item.order.order_id, item.order.school) for item in items])
            await _send(bot, chat_id, format_stage_reminder(items, deadlines), keyboard)
            for item in items:
                await db.set_last_reminder(chat_id, item.order.order_id, item.order.status, now.isoformat())

    for digest in await collect_schedule_digests(db, now=now, settings=settings):
        keyboard = orders_keyboard([(order.order_id, order.school) for order, _ in digest.items])
        await _send(bot, digest.chat_id, format_schedule(digest), keyboard)
        await db.mark_notice_sent(digest.notice_key)

    for reminder in await collect_pre_shoot_reminders(db, now=now, settings=settings):
        keyboard = orders_keyboard([(reminder.order.order_id, reminder.order.school)])
        await _send(bot, reminder.chat_id, format_pre_shoot(reminder, settings.pre_shoot_hours), keyboard)
        await db.mark_notice_sent(reminder.notice_key)


async def notify_shoot_changes(bot: Bot, db: Database, events: list[ShootEvent]) -> None:
    """Фотографу — если его съёмку перенесли, сменили место, отменили или передали."""
    for event in events:
        if event.previous is None:
            continue
        before, after = event.previous, event.shoot
        order = await db.get_cached_order(after.order_id)
        school = order.school if order else f"заказ №{after.order_id}"
        keyboard = orders_keyboard([(after.order_id, school)])

        if event.kind == "deleted":
            chat_id = await chat_id_by_name(db, before.photographer) if before.photographer else None
            if chat_id is not None:
                await _send(
                    bot, chat_id,
                    f"❌ Съёмка отменена: {school}\n📅 {format_when(before.date, before.time)}\n📍 {before.location}",
                    keyboard,
                )
            continue

        if after.photographer and (before.date, before.time, before.location) != (after.date, after.time, after.location):
            chat_id = await photographer_chat_id(db, after)
            if chat_id is not None:
                await _send(
                    bot, chat_id,
                    f"🔁 Съёмка изменилась: {school}\n"
                    f"было: {format_when(before.date, before.time)}, {before.location}\n"
                    f"стало: {format_when(after.date, after.time)}, {after.location}",
                    keyboard,
                )

        if before.photographer and after.photographer and before.photographer != after.photographer:
            chat_id = await chat_id_by_name(db, before.photographer)
            if chat_id is not None:
                await _send(
                    bot, chat_id,
                    f"↪️ Съёмку {school}, {format_when(after.date, after.time)} передали: {after.photographer}",
                    keyboard,
                )
