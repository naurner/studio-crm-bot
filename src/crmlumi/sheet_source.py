"""Опрос источника заказов и сравнение со старым снимком.

Не знает, что источник — Google Таблица: работает через протокол
SheetClient, поэтому тестируется без сети и без gspread.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from .db import Database
from .models import Order, OrderEvent, Shoot, ShootEvent


class SheetClient(Protocol):
    def fetch_rows(self) -> list[dict[str, str]]: ...
    def update_order(self, order_id: str, updates: dict[str, str]) -> None: ...
    def append_row(self, row: dict[str, str]) -> None: ...
    def delete_row(self, row_id: str) -> None: ...


def _diff(order: Order, previous: Order | None, timestamp: datetime) -> OrderEvent | None:
    """Определяет тип события по разнице между новым и старым снимком.

    Порядок проверок задаёт приоритет: смена статуса важнее, чем то, что
    заодно поменялась ещё и дата — иначе на смену статуса при одновременном
    редактировании нескольких полей мог бы уйти "updated" вместо
    "status_changed", а именно смена статуса запускает следующий этап
    воронки.
    """
    if previous is None:
        return OrderEvent(order.order_id, "created", order, None, timestamp)
    if order.status != previous.status:
        return OrderEvent(order.order_id, "status_changed", order, previous, timestamp)
    if (order.shoot_date, order.locations) != (previous.shoot_date, previous.locations):
        return OrderEvent(order.order_id, "date_location_set", order, previous, timestamp)
    if order != previous:
        return OrderEvent(order.order_id, "updated", order, previous, timestamp)
    return None


async def poll_once(sheet: SheetClient, db: Database) -> list[OrderEvent]:
    """Один цикл опроса: читает все строки, сравнивает с кэшем, обновляет кэш."""
    events: list[OrderEvent] = []
    timestamp = datetime.now(timezone.utc)
    seen: set[str] = set()
    for row in sheet.fetch_rows():
        order = Order.from_row(row)
        if not order.order_id:
            # Пустая строка в таблице (или строка без ID) — не заказ.
            continue
        seen.add(order.order_id)
        previous = await db.get_cached_order(order.order_id)
        event = _diff(order, previous, timestamp)
        if event is not None:
            events.append(event)
            if event.kind in ("created", "status_changed"):
                await db.set_status_since(order.order_id, order.status, timestamp.isoformat())
        await db.save_order(order)
    # Строку заказа удалили из таблицы руками — убираем и из кэша, иначе заказ
    # так и висел бы в списках бота.
    for cached in await db.list_cached_orders():
        if cached.order_id not in seen:
            await db.delete_cached_order(cached.order_id)
    return events


async def poll_shoots(sheet: SheetClient, db: Database) -> list[ShootEvent]:
    """Опрос листа «Съёмки»: новые, изменённые и пропавшие съёмки."""
    events: list[ShootEvent] = []
    seen: set[str] = set()
    for row in sheet.fetch_rows():
        shoot = Shoot.from_row(row)
        if not shoot.shoot_id:
            continue
        seen.add(shoot.shoot_id)
        previous = await db.get_cached_shoot(shoot.shoot_id)
        if previous is None:
            events.append(ShootEvent("created", shoot, None))
        elif previous != shoot:
            events.append(ShootEvent("changed", shoot, previous))
        else:
            continue
        await db.save_shoot(shoot)
    for cached in await db.list_cached_shoots():
        if cached.shoot_id not in seen:
            await db.delete_cached_shoot(cached.shoot_id)
            events.append(ShootEvent("deleted", cached, cached))
    return events
