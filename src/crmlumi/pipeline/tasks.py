"""Задачи в Microsoft To Do: по этапам заказа и по каждой взятой съёмке.

Всё идемпотентно и работает сверкой после каждого опроса таблицы, а не по
событиям: задача появится, даже если событие было пропущено (To Do был
недоступен, заказ внесли сразу в середину воронки). Первая же ошибка Graph
API прерывает проход — скорее всего, недоступен весь сервис; остальное
доделается на следующем круге.
"""

from __future__ import annotations

import json
from datetime import timedelta, tzinfo

from ..db import Database
from ..models import Order
from ..statuses import STATUS_SHOOT_ASSIGNED
from ..timefmt import format_short
from .router import task_role_for_status
from .shoots import PHOTO_STAGES, shoot_start
from .stages import stage_due_date, stage_index, stage_links, stage_role_for_status

TASK_TITLES: dict[str, str] = {
    "Приём заявок": "Перезвонить по заказу",
    "Даты и локации": "Назначить съёмки",
    "Обработка фото": "Обработать фото",
    "Вёрстка и согласование": "Свести альбом",
    "Печать и выдача": "Отправить в печать",
}

# Подпись задачи съёмки, которая уже закрыта: больше её не трогаем.
DONE = "done"


async def create_stage_task(
    db: Database,
    graph,
    list_id: str,
    order: Order,
    role_name: str,
    *,
    due_date: str | None = None,
) -> str | None:
    """Создаёт задачу в списке роли, если она ещё не создана для заказа+роли."""
    title_prefix = TASK_TITLES.get(role_name)
    if title_prefix is None:
        return None

    existing = await db.get_created_task(order.order_id, role_name)
    if existing is not None:
        return existing

    title = f"{title_prefix}: {order.school}"
    links = stage_links(order)
    if links:
        note = "\n".join(f"{label}: {link}" for label, link in links)
        task_id = graph.create_task(list_id, title, due_date=due_date, note=note)
    else:
        task_id = graph.create_task(list_id, title, due_date=due_date)
    await db.save_created_task(order.order_id, role_name, list_id, task_id)
    return task_id


async def sync_stage_tasks(
    db: Database,
    graph,
    list_ids: dict[str, str],
    *,
    deadlines: dict[str, int] | None = None,
    tz: tzinfo | None = None,
) -> int:
    """Досоздаёт задачу текущего этапа (со сроком) для заказов, где её нет."""
    created = 0
    for order in await db.list_cached_orders():
        role_name = task_role_for_status(order.status)
        if role_name is None:
            continue
        list_id = list_ids.get(role_name)
        if not list_id:
            continue
        if await db.get_created_task(order.order_id, role_name) is not None:
            continue
        due = None
        if deadlines and tz is not None:
            since = await db.get_status_since(order.order_id)
            since_iso = since[1] if since and since[0] == order.status else None
            due_day = stage_due_date(order.status, since_iso, deadlines, tz)
            due = due_day.isoformat() if due_day else None
        await create_stage_task(db, graph, list_id, order, role_name, due_date=due)
        created += 1
    return created


async def complete_finished_tasks(db: Database, graph) -> int:
    """Закрывает в To Do задачи этапов, которые заказ уже прошёл.

    Задача остаётся открытой, пока за заказ отвечает та же роль (например,
    «Вёрстка» -> «Согласование с клиентом»). Заказ, пропавший из таблицы,
    не трогаем — непонятно, выполнена ли работа. Возвращает число закрытых.
    """
    completed = 0
    for order_id, role_name, list_id, task_id in await db.list_open_tasks():
        order = await db.get_cached_order(order_id)
        if order is None or stage_role_for_status(order.status) == role_name:
            continue
        graph.complete_task(list_id, task_id)
        await db.mark_task_completed(order_id, role_name)
        completed += 1
    return completed


def shoot_task_fields(order: Order, shoot, *, tz: tzinfo, pre_shoot_hours: float):
    title = (
        f"Съёмка: {order.school} — {shoot.location or 'локация не указана'}, "
        f"{format_short(shoot.date, shoot.time)} ({shoot.photographer})"
    )
    start = shoot_start(shoot, tz)
    remind_at = start - timedelta(hours=pre_shoot_hours) if start is not None else None
    due_date = shoot.date or None
    signature = json.dumps(
        [title, due_date, remind_at.isoformat() if remind_at else None], ensure_ascii=False
    )
    return title, due_date, remind_at, signature


async def sync_shoot_tasks(
    db: Database, graph, list_id: str, *, tz: tzinfo, pre_shoot_hours: float
) -> int:
    """Задача фотографу на каждую взятую съёмку: срок — день съёмки,
    напоминание To Do — за pre_shoot_hours до начала.

    Съёмку перенесли — задача обновляется; фотограф отказался или съёмку
    удалили — задача удаляется; заказ ушёл дальше съёмок — закрывается.
    Возвращает число изменений.
    """
    orders = {order.order_id: order for order in await db.list_cached_orders()}
    shoots = {shoot.shoot_id: shoot for shoot in await db.list_cached_shoots()}
    assigned_index = stage_index(STATUS_SHOOT_ASSIGNED)
    changes = 0

    for shoot in shoots.values():
        order = orders.get(shoot.order_id)
        if order is None:
            continue
        record = await db.get_shoot_task(shoot.shoot_id)
        if record is not None and record[2] == DONE:
            continue

        if shoot.photographer and order.status in PHOTO_STAGES:
            title, due_date, remind_at, signature = shoot_task_fields(
                order, shoot, tz=tz, pre_shoot_hours=pre_shoot_hours
            )
            if record is None:
                task_id = graph.create_task(list_id, title, due_date=due_date, remind_at=remind_at)
                await db.save_shoot_task(shoot.shoot_id, list_id, task_id, signature)
                changes += 1
            elif record[2] != signature:
                graph.update_task(
                    record[0], record[1], title=title, due_date=due_date, remind_at=remind_at
                )
                await db.save_shoot_task(shoot.shoot_id, record[0], record[1], signature)
                changes += 1
        elif record is not None:
            index = stage_index(order.status)
            if shoot.photographer and index is not None and index > assigned_index:
                graph.complete_task(record[0], record[1])
                await db.save_shoot_task(shoot.shoot_id, record[0], record[1], DONE)
            else:
                graph.delete_task(record[0], record[1])
                await db.delete_shoot_task(shoot.shoot_id)
            changes += 1

    for shoot_id in await db.list_shoot_task_ids():
        if shoot_id in shoots:
            continue
        record = await db.get_shoot_task(shoot_id)
        if record is not None and record[2] != DONE:
            graph.delete_task(record[0], record[1])
            await db.delete_shoot_task(shoot_id)
            changes += 1
    return changes
