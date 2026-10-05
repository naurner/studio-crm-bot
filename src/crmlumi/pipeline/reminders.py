"""Кому и когда напоминать. Только решения — тексты и отправка в notify/reminders.py.

- напоминания о делах: заказы, где ждут человека дольше его интервала;
- фотографам: расписание на завтра вечером, по воскресеньям — на неделю;
- фотографу: за pre_shoot_hours до съёмки.

`now` — всегда осознанное локальное время (timefmt.local_now()).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..config import ReminderSettings
from ..db import Database
from ..models import Order, Shoot
from ..statuses import STATUS_SEEKING_PHOTOGRAPHER, STATUS_SHOOT_ASSIGNED
from ..timefmt import parse_iso_date
from .shoots import PHOTO_STAGES, photographer_chat_id, shoot_finished, shoot_start, sort_shoots
from .stages import orders_waiting_for

# Варианты интервала в настройках, часы; 0 — выключено.
REMINDER_CHOICES: tuple[int, ...] = (3, 6, 12, 24, 48, 72, 0)

# Что человеку сделать на этапе — строка в напоминании.
STAGE_ACTIONS: dict[str, str] = {
    "Лид": "позвонить клиенту",
    "Договорились": "назначить съёмки",
    "Дата и локация назначены": "запустить поиск фотографов",
    "Съёмка назначена": "отметить, что съёмки проведены",
    "Обработка фото": "обработать фото",
    "Вёрстка": "свёрстать альбом",
    "Согласование с клиентом": "получить согласование клиента",
    "Печать": "напечатать и выдать альбом",
}


def interval_label(hours: int) -> str:
    if hours == 0:
        return "выключены"
    if hours % 24 == 0:
        days = hours // 24
        return "раз в день" if days == 1 else f"раз в {days} дня"
    return f"раз в {hours} ч"


def in_active_hours(now: datetime, settings: ReminderSettings) -> bool:
    return settings.quiet_end <= now.hour < settings.quiet_start


@dataclass(frozen=True)
class StageReminder:
    order: Order
    days_in_stage: int


async def collect_stage_reminders(
    db: Database, chat_id: int, owner_chat_id: int, *, now: datetime, interval_hours: int
) -> list[StageReminder]:
    """Заказы, о которых пора напомнить: этап длится дольше интервала и с
    прошлого напоминания по этому этапу прошёл интервал."""
    if interval_hours <= 0:
        return []
    interval = timedelta(hours=interval_hours)
    due = []
    for order in await orders_waiting_for(db, chat_id, owner_chat_id):
        if order.status == STATUS_SEEKING_PHOTOGRAPHER:
            continue  # фотографы разбирают съёмки из рассылки, торопить некого
        if order.status == STATUS_SHOOT_ASSIGNED:
            shoots = await db.list_shoots_for_order(order.order_id)
            if not shoots or not all(shoot_finished(shoot, now) for shoot in shoots):
                continue
        since = await db.get_status_since(order.order_id)
        if since is None or since[0] != order.status:
            continue
        since_at = datetime.fromisoformat(since[1])
        if now - since_at < interval:
            continue
        last = await db.get_last_reminder(chat_id, order.order_id, order.status)
        if last is not None and now - datetime.fromisoformat(last) < interval:
            continue
        due.append(StageReminder(order, (now - since_at).days))
    return due


async def claimed_shoots_by_chat(
    db: Database, *, day_from: date, day_to: date
) -> dict[int, list[tuple[Order, Shoot]]]:
    """Взятые съёмки в диапазоне дней (включительно) по фотографам."""
    orders = {order.order_id: order for order in await db.list_cached_orders()}
    grouped: dict[int, list[tuple[Order, Shoot]]] = defaultdict(list)
    for shoot in sort_shoots(await db.list_cached_shoots()):
        order = orders.get(shoot.order_id)
        day = parse_iso_date(shoot.date)
        if order is None or order.status not in PHOTO_STAGES or day is None or not shoot.photographer:
            continue
        if not day_from <= day <= day_to:
            continue
        chat_id = await photographer_chat_id(db, shoot)
        if chat_id is not None:
            grouped[chat_id].append((order, shoot))
    return grouped


@dataclass(frozen=True)
class ScheduleDigest:
    chat_id: int
    weekly: bool
    day_from: date
    day_to: date
    items: list[tuple[Order, Shoot]]
    notice_key: str


async def collect_schedule_digests(
    db: Database, *, now: datetime, settings: ReminderSettings
) -> list[ScheduleDigest]:
    """Вечером — съёмки на завтра; в день weekly_weekday — на неделю вперёд."""
    if now.hour < settings.evening_hour:
        return []
    today = now.date()
    weekly = today.weekday() == settings.weekly_weekday
    day_from = today + timedelta(days=1)
    day_to = today + timedelta(days=7 if weekly else 1)
    kind = "week" if weekly else "day"
    digests = []
    grouped = await claimed_shoots_by_chat(db, day_from=day_from, day_to=day_to)
    for chat_id, items in grouped.items():
        key = f"{kind}:{chat_id}:{day_from.isoformat()}"
        if await db.was_notice_sent(key):
            continue
        digests.append(ScheduleDigest(chat_id, weekly, day_from, day_to, items, key))
    return digests


@dataclass(frozen=True)
class PreShootReminder:
    chat_id: int
    order: Order
    shoot: Shoot
    notice_key: str


async def collect_pre_shoot_reminders(
    db: Database, *, now: datetime, settings: ReminderSettings
) -> list[PreShootReminder]:
    """Съёмки, до начала которых осталось не больше pre_shoot_hours.

    Ключ включает время начала: съёмку перенесли — напомним заново. Если бот
    был выключен и съёмка уже началась — не шлём.
    """
    orders = {order.order_id: order for order in await db.list_cached_orders()}
    lead = timedelta(hours=settings.pre_shoot_hours)
    result = []
    for shoot in sort_shoots(await db.list_cached_shoots()):
        order = orders.get(shoot.order_id)
        start = shoot_start(shoot, now.tzinfo)
        if order is None or order.status not in PHOTO_STAGES or start is None:
            continue
        if not (start - lead <= now < start):
            continue
        chat_id = await photographer_chat_id(db, shoot)
        if chat_id is None:
            continue
        key = f"pre:{shoot.shoot_id}:{shoot.date}T{shoot.time}"
        if await db.was_notice_sent(key):
            continue
        result.append(PreShootReminder(chat_id, order, shoot, key))
    return result
