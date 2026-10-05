"""Съёмки заказа: создание и правка, claim фотографа, сводка в строку заказа.

У заказа сколько угодно съёмок (дата + время + локация), каждую фотограф
берёт отдельно. Этап «Ищем фотографа» ⇄ «Съёмка назначена» выводится из
съёмок: когда у всех есть фотограф — назначена, освободилась хоть одна —
снова ищем.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime

from ..db import Database
from ..models import FIELD_COLUMNS, SHOOT_COLUMNS, Order, Shoot
from ..sheet_source import SheetClient
from ..statuses import STATUS_SEEKING_PHOTOGRAPHER, STATUS_SHOOT_ASSIGNED
from ..timefmt import combine, format_short, format_when, normalize_sheet_date, parse_iso_date

PHOTO_STAGES = (STATUS_SEEKING_PHOTOGRAPHER, STATUS_SHOOT_ASSIGNED)
SUMMARY_FIELDS = ("shoot_date", "locations", "photographer")


def _id_key(shoot_id: str) -> tuple:
    order_id, _, number = shoot_id.rpartition("-")
    return (order_id, int(number) if number.isdigit() else 0, shoot_id)


def sort_shoots(shoots: list[Shoot]) -> list[Shoot]:
    return sorted(
        shoots, key=lambda s: (s.date or "9999-99-99", s.time or "99:99", _id_key(s.shoot_id))
    )


def shoot_start(shoot: Shoot, tz) -> datetime | None:
    return combine(shoot.date, shoot.time, tz)


def shoot_finished(shoot: Shoot, now: datetime) -> bool:
    """Прошла ли съёмка: начало в прошлом, а без времени — день закончился."""
    start = shoot_start(shoot, now.tzinfo)
    if start is not None:
        return start <= now
    day = parse_iso_date(shoot.date)
    return day is not None and day < now.date()


def shoot_line(shoot: Shoot, *, with_photographer: bool = True) -> str:
    parts = [format_when(shoot.date, shoot.time), shoot.location or "локация не указана"]
    if with_photographer:
        parts.append(shoot.photographer or "❗ нет фотографа")
    return " — ".join(parts)


def next_shoot_id(order_id: str, rows: list[dict]) -> str:
    prefix = f"{order_id}-"
    numbers = []
    for row in rows:
        shoot_id = str(row.get(SHOOT_COLUMNS["shoot_id"], "")).strip()
        suffix = shoot_id[len(prefix):]
        if shoot_id.startswith(prefix) and suffix.isdigit():
            numbers.append(int(suffix))
    return f"{prefix}{max(numbers, default=0) + 1}"


def summarize_shoots(shoots: list[Shoot]) -> dict[str, str]:
    """Сводка для колонок заказа (ключи — поля Order)."""
    ordered = sort_shoots(shoots)
    dates = [shoot.date for shoot in ordered if parse_iso_date(shoot.date)]
    names: list[str] = []
    for shoot in ordered:
        if shoot.photographer and shoot.photographer not in names:
            names.append(shoot.photographer)
    return {
        "shoot_date": dates[0] if dates else "",
        "locations": "; ".join(
            f"{format_short(shoot.date, shoot.time)} {shoot.location}".strip() for shoot in ordered
        ),
        "photographer": ", ".join(names),
    }


def photo_stage_status(status: str, shoots: list[Shoot]) -> str | None:
    """Нужный статус заказа по съёмкам, если текущий ему не соответствует."""
    all_taken = bool(shoots) and all(shoot.photographer for shoot in shoots)
    if status == STATUS_SEEKING_PHOTOGRAPHER and all_taken:
        return STATUS_SHOOT_ASSIGNED
    if status == STATUS_SHOOT_ASSIGNED and not all_taken:
        return STATUS_SEEKING_PHOTOGRAPHER
    return None


def display_name(user: dict) -> str:
    """То же правило, что Database.get_user_display_name."""
    return user["display_name"] or (f"@{user['username']}" if user["username"] else str(user["chat_id"]))


async def chat_id_by_name(db: Database, name: str) -> int | None:
    for user in await db.list_users():
        if display_name(user) == name:
            return user["chat_id"]
    return None


async def photographer_chat_id(db: Database, shoot: Shoot) -> int | None:
    """Кому писать про съёмку: по claim, а если фотографа вписали руками — по имени."""
    claimant = await db.get_shoot_claim(shoot.shoot_id)
    if claimant is not None:
        return claimant
    if not shoot.photographer:
        return None
    return await chat_id_by_name(db, shoot.photographer)


async def add_shoot(
    sheet: SheetClient, order_id: str, *, date: str, time: str, location: str
) -> Shoot:
    # Номер берём из свежего листа, а не из кэша: две съёмки подряд до
    # следующего опроса иначе получили бы один ID.
    rows = await asyncio.to_thread(sheet.fetch_rows)
    shoot = Shoot(next_shoot_id(order_id, rows), order_id, date, time, location, "")
    await asyncio.to_thread(sheet.append_row, shoot.to_row())
    return shoot


async def update_shoot(sheet: SheetClient, shoot: Shoot, **changes: str) -> Shoot:
    updates = {SHOOT_COLUMNS[field]: value for field, value in changes.items()}
    await asyncio.to_thread(sheet.update_order, shoot.shoot_id, updates)
    return replace(shoot, **changes)


async def delete_shoot(
    db: Database,
    shoots_sheet: SheetClient,
    orders_sheet: SheetClient,
    shoot: Shoot,
    *,
    order: Order | None,
    remaining: list[Shoot],
) -> None:
    await asyncio.to_thread(shoots_sheet.delete_row, shoot.shoot_id)
    await db.clear_shoot_claim(shoot.shoot_id)
    await db.clear_claim_broadcasts(shoot.shoot_id)
    if not remaining and order is not None:
        # Сверка не трогает заказы без съёмок — сводку после последней чистим сами.
        cleared = {FIELD_COLUMNS[field]: "" for field in SUMMARY_FIELDS if getattr(order, field)}
        if cleared:
            await asyncio.to_thread(orders_sheet.update_order, order.order_id, cleared)


async def claim_shoot(db: Database, sheet: SheetClient, shoot: Shoot, chat_id: int) -> int:
    """Возвращает победителя — он может отличаться, если кто-то опередил."""
    winner = await db.try_claim_shoot(shoot.shoot_id, chat_id)
    if winner == chat_id:
        name = await db.get_user_display_name(chat_id)
        await asyncio.to_thread(
            sheet.update_order, shoot.shoot_id, {SHOOT_COLUMNS["photographer"]: name}
        )
    return winner


async def release_shoot(db: Database, sheet: SheetClient, shoot: Shoot) -> Shoot:
    await db.clear_shoot_claim(shoot.shoot_id)
    # Снова разошлётся фотографам при следующей сверке.
    await db.clear_claim_broadcasts(shoot.shoot_id)
    await asyncio.to_thread(sheet.update_order, shoot.shoot_id, {SHOOT_COLUMNS["photographer"]: ""})
    return replace(shoot, photographer="")


async def assign_shoot(db: Database, sheet: SheetClient, shoot: Shoot, chat_id: int) -> Shoot:
    """Владелец назначает фотографа сам, перебивая прежний claim."""
    await db.set_shoot_claim(shoot.shoot_id, chat_id)
    await db.clear_claim_broadcasts(shoot.shoot_id)
    name = await db.get_user_display_name(chat_id)
    await asyncio.to_thread(sheet.update_order, shoot.shoot_id, {SHOOT_COLUMNS["photographer"]: name})
    return replace(shoot, photographer=name)


async def shoots_by_order(db: Database) -> dict[str, list[Shoot]]:
    grouped: dict[str, list[Shoot]] = defaultdict(list)
    for shoot in await db.list_cached_shoots():
        grouped[shoot.order_id].append(shoot)
    return grouped


async def reconcile_orders_with_shoots(db: Database, orders_sheet: SheetClient) -> int:
    """Сводка съёмок в строке заказа и авто-переход между фото-этапами.

    Возвращает число заказов, в которые что-то записали.
    """
    grouped = await shoots_by_order(db)
    written = 0
    for order in await db.list_cached_orders():
        shoots = grouped.get(order.order_id, [])
        changes: dict[str, str] = {}
        if shoots:
            for field, value in summarize_shoots(shoots).items():
                if getattr(order, field) != value:
                    changes[field] = value
        target = photo_stage_status(order.status, shoots)
        if target is not None:
            changes["status"] = target
        if changes:
            updates = {FIELD_COLUMNS[field]: value for field, value in changes.items()}
            await asyncio.to_thread(orders_sheet.update_order, order.order_id, updates)
            written += 1
    return written


async def shoots_to_offer(db: Database, *, today: date) -> list[tuple[Order, Shoot]]:
    """Незанятые будущие съёмки заказов в поиске, которые ещё не рассылались.

    Заодно забывает рассылки по съёмкам, которые больше не ищут фотографа, —
    чтобы при возврате заказа в поиск предложение ушло заново.
    """
    orders = {order.order_id: order for order in await db.list_cached_orders()}
    result = []
    for shoot in sort_shoots(await db.list_cached_shoots()):
        order = orders.get(shoot.order_id)
        searching = order is not None and order.status == STATUS_SEEKING_PHOTOGRAPHER
        free = not shoot.photographer and await db.get_shoot_claim(shoot.shoot_id) is None
        if not (searching and free):
            await db.clear_claim_broadcasts(shoot.shoot_id)
            continue
        day = parse_iso_date(shoot.date)
        if day is not None and day < today:
            continue
        if await db.get_claim_broadcasts(shoot.shoot_id):
            continue
        result.append((order, shoot))
    return result


async def migrate_legacy_shoots(
    db: Database, order_rows: list[dict], shoots_sheet: SheetClient
) -> int:
    """Разовый перенос: дата/локация/фотограф из строки заказа -> съёмка `ID-1`."""
    migrated = 0
    for row in order_rows:
        order = Order.from_row(row)
        if not order.order_id or not (order.shoot_date or order.locations or order.photographer):
            continue
        day = normalize_sheet_date(order.shoot_date)
        location = order.locations
        if order.shoot_date and not parse_iso_date(day):
            location = f"{location} ({order.shoot_date})".strip()
            day = ""
        shoot = Shoot(f"{order.order_id}-1", order.order_id, day, "", location, order.photographer)
        await asyncio.to_thread(shoots_sheet.append_row, shoot.to_row())
        claimant = await db.get_claim(order.order_id)
        if claimant is not None:
            await db.set_shoot_claim(shoot.shoot_id, claimant)
        migrated += 1
    return migrated
