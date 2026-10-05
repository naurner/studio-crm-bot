"""Ежедневные проверки: просроченные задачи, заказы без фотографа при
близкой дате съёмки, заказы, зависшие в одном статусе.

Только сбор данных — форматирование сообщения и отправка в notify/telegram.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..db import Database
from ..models import Order, Shoot
from ..statuses import STATUS_DATE_LOCATION_SET, STATUS_DELIVERED, STATUS_SEEKING_PHOTOGRAPHER
from ..timefmt import parse_iso_date
from .shoots import sort_shoots


def _parse_graph_datetime(value: str) -> datetime:
    """Graph API отдаёт время с 7 знаками дробной секунды — больше, чем
    поддерживает datetime.fromisoformat (максимум 6), поэтому обрезаем."""
    value = value.replace("Z", "+00:00")
    if "." in value:
        base, rest = value.split(".", 1)
        digits = ""
        i = 0
        while i < len(rest) and rest[i].isdigit():
            digits += rest[i]
            i += 1
        tz_part = rest[i:]
        value = f"{base}.{digits[:6].ljust(6, '0')}{tz_part}"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def find_shoots_without_photographer_near_date(
    db: Database, *, now: datetime, near_days: int = 3
) -> list[tuple[Order, Shoot]]:
    """Съёмки без фотографа, до которых осталось near_days дней или меньше."""
    orders = {order.order_id: order for order in await db.list_cached_orders()}
    today = now.date()
    result = []
    for shoot in sort_shoots(await db.list_cached_shoots()):
        order = orders.get(shoot.order_id)
        if order is None or order.status not in (STATUS_DATE_LOCATION_SET, STATUS_SEEKING_PHOTOGRAPHER):
            continue
        if shoot.photographer:
            continue
        day = parse_iso_date(shoot.date)
        if day is not None and 0 <= (day - today).days <= near_days:
            result.append((order, shoot))
    return result


async def find_stalled_orders(
    db: Database, *, now: datetime, stalled_days: int = 5
) -> list[tuple[Order, int]]:
    """Возвращает (заказ, сколько дней в текущем статусе) для зависших заказов."""
    result = []
    for order in await db.list_cached_orders():
        if order.status == STATUS_DELIVERED:
            continue
        since = await db.get_status_since(order.order_id)
        if since is None:
            continue
        _, since_iso = since
        since_dt = datetime.fromisoformat(since_iso)
        days = (now - since_dt).days
        if days >= stalled_days:
            result.append((order, days))
    return result


def find_overdue_tasks(graph, list_ids: dict[str, str], *, now: datetime) -> list[dict]:
    """Возвращает просроченные незавершённые задачи по всем переданным спискам."""
    overdue = []
    for role_name, list_id in list_ids.items():
        for task in graph.list_tasks(list_id):
            if task.get("status") == "completed":
                continue
            due = (task.get("dueDateTime") or {}).get("dateTime")
            if not due:
                continue
            # Срок в To Do — это день: просрочена задача со следующего дня.
            if _parse_graph_datetime(due).date() < now.date():
                overdue.append({"role": role_name, "title": task.get("title", "")})
    return overdue
