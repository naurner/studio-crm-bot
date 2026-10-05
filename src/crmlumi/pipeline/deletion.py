"""Удаление заказа целиком: строка в «Заказах», его съёмки, задачи To Do, данные бота."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..db import Database
from ..models import SHOOT_COLUMNS, Shoot
from ..sheet_source import SheetClient
from .shoots import photographer_chat_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeletionResult:
    shoots_deleted: int
    # Кому сообщить об отмене: фотографы, уже взявшие съёмки.
    notify: list[tuple[int, Shoot]]


async def delete_order(
    db: Database,
    orders_sheet: SheetClient,
    shoots_sheet: SheetClient,
    order_id: str,
    *,
    graph=None,
) -> DeletionResult:
    cached_shoots = await db.list_shoots_for_order(order_id)
    notify = []
    for shoot in cached_shoots:
        chat_id = await photographer_chat_id(db, shoot) if shoot.photographer else None
        if chat_id is not None:
            notify.append((chat_id, shoot))

    # Съёмки ищем в свежем листе: там могут быть и те, что ещё не попали в кэш.
    rows = await asyncio.to_thread(shoots_sheet.fetch_rows)
    sheet_shoot_ids = [
        str(row.get(SHOOT_COLUMNS["shoot_id"], "")).strip()
        for row in rows
        if str(row.get(SHOOT_COLUMNS["order_id"], "")).strip() == order_id
    ]
    for shoot_id in sheet_shoot_ids:
        if shoot_id:
            await asyncio.to_thread(shoots_sheet.delete_row, shoot_id)
    try:
        await asyncio.to_thread(orders_sheet.delete_row, order_id)
    except ValueError:
        pass  # строку уже удалили руками — дочищаем остальное

    shoot_ids = sorted({shoot.shoot_id for shoot in cached_shoots} | set(filter(None, sheet_shoot_ids)))
    tasks = await db.delete_order_data(order_id, shoot_ids)
    if graph is not None:
        for list_id, task_id in tasks:
            try:
                await asyncio.to_thread(graph.delete_task, list_id, task_id)
            except Exception:
                log.exception("Не удалось удалить задачу To Do %s удалённого заказа %s", task_id, order_id)
    return DeletionResult(shoots_deleted=len(shoot_ids), notify=notify)
