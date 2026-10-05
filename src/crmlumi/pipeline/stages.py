"""Этапы заказа: кто отвечает, куда двигать дальше, что спросить при переходе.

Логика без Telegram — хендлеры карточки заказа (notify/orders_ui.py) только
вызывают её. Бот пишет изменения в Google Таблицу и не трогает кэш заказов:
смену статуса подхватывает обычный опрос, и дальше всё как при правке
таблицы руками — уведомление следующей роли, рассылка фотографам, задачи
в To Do.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo

from ..db import Database
from ..models import FIELD_COLUMNS, Order, Shoot
from ..roles import (
    ROLE_DATES_LOCATIONS,
    ROLE_LAYOUT,
    ROLE_NEW_LEADS,
    ROLE_PHOTO_PROCESSING,
    ROLE_PHOTOGRAPHER,
    ROLE_PRINTING,
)
from ..sheet_source import SheetClient
from ..statuses import (
    FUNNEL_ORDER,
    STATUS_AGREED,
    STATUS_CLIENT_APPROVAL,
    STATUS_DATE_LOCATION_SET,
    STATUS_LAYOUT,
    STATUS_LEAD,
    STATUS_PHOTO_PROCESSING,
    STATUS_PRINTING,
    STATUS_SEEKING_PHOTOGRAPHER,
    STATUS_SHOOT_ASSIGNED,
)
from ..timefmt import format_date, format_day  # noqa: F401 — format_date нужен импортёрам
from .shoots import photographer_chat_id, release_shoot

# Кто отвечает за заказ, пока он на этом этапе. На «Выдано» — никто.
STAGE_ROLE: dict[str, str] = {
    STATUS_LEAD: ROLE_NEW_LEADS,
    STATUS_AGREED: ROLE_DATES_LOCATIONS,
    STATUS_DATE_LOCATION_SET: ROLE_DATES_LOCATIONS,
    STATUS_SEEKING_PHOTOGRAPHER: ROLE_PHOTOGRAPHER,
    STATUS_SHOOT_ASSIGNED: ROLE_PHOTOGRAPHER,
    STATUS_PHOTO_PROCESSING: ROLE_PHOTO_PROCESSING,
    STATUS_LAYOUT: ROLE_LAYOUT,
    STATUS_CLIENT_APPROVAL: ROLE_LAYOUT,
    STATUS_PRINTING: ROLE_PRINTING,
}

# Кнопка «дальше» на каждом этапе. Из «Ищем фотографа» дальше двигают
# фотографы, разбирая съёмки («Взять»).
ADVANCE_LABELS: dict[str, str] = {
    STATUS_LEAD: "✅ Договорились",
    STATUS_AGREED: "📅 Съёмки назначены",
    STATUS_DATE_LOCATION_SET: "📸 Искать фотографов",
    STATUS_SHOOT_ASSIGNED: "✅ Съёмки проведены",
    STATUS_PHOTO_PROCESSING: "✅ Фото обработаны",
    STATUS_LAYOUT: "✅ Альбом свёрстан",
    STATUS_CLIENT_APPROVAL: "✅ Клиент согласовал",
    STATUS_PRINTING: "✅ Выдано клиенту",
}

# Что спросить при переходе НА этап — только поля, которые ещё пустые.
# (поле, обязательное ли). Даты и локации — не поля, а съёмки (shoots_ui).
TRANSITION_INPUTS: dict[str, tuple[tuple[str, bool], ...]] = {
    STATUS_AGREED: (("contract_amount", False),),
    # Материалы передаются по цепочке: закрывая свой этап, человек оставляет
    # ссылку на диск, и её сразу получает следующий.
    STATUS_PHOTO_PROCESSING: (("raw_photos_link", False),),
    STATUS_LAYOUT: (("processed_photos_link", False),),
    STATUS_CLIENT_APPROVAL: (("layout_link", False),),
    STATUS_PRINTING: (("layout_link", False),),
}

LINK_FIELDS: tuple[str, ...] = ("raw_photos_link", "processed_photos_link", "layout_link")
LINK_LABELS: dict[str, str] = {
    "raw_photos_link": "📁 Исходники",
    "processed_photos_link": "🎨 Обработанные фото",
    "layout_link": "📖 Макет",
}
# Какие ссылки нужны тому, кто ведёт этап.
STAGE_LINKS: dict[str, tuple[str, ...]] = {
    STATUS_PHOTO_PROCESSING: ("raw_photos_link",),
    STATUS_LAYOUT: ("processed_photos_link",),
    STATUS_CLIENT_APPROVAL: ("layout_link",),
    STATUS_PRINTING: ("layout_link",),
}

EDITABLE_FIELDS: tuple[str, ...] = (
    "school",
    "contact_name",
    "contact_phone",
    "people_count",
    "contract_amount",
    "payment_status",
    "notes",
    *LINK_FIELDS,
)
PAYMENT_OPTIONS: tuple[str, ...] = ("Не оплачено", "Предоплата", "Оплачено полностью")


def stage_index(status: str) -> int | None:
    try:
        return FUNNEL_ORDER.index(status)
    except ValueError:
        return None


def next_status(status: str) -> str | None:
    index = stage_index(status)
    if index is None or index + 1 >= len(FUNNEL_ORDER):
        return None
    return FUNNEL_ORDER[index + 1]


def previous_status(status: str) -> str | None:
    index = stage_index(status)
    if index is None or index == 0:
        return None
    return FUNNEL_ORDER[index - 1]


def stage_role_for_status(status: str) -> str | None:
    return STAGE_ROLE.get(status)


def missing_inputs(order: Order, target_status: str) -> list[tuple[str, bool]]:
    return [
        (field, required)
        for field, required in TRANSITION_INPUTS.get(target_status, ())
        if not getattr(order, field)
    ]


def looks_like_link(text: str) -> bool:
    return "http://" in text or "https://" in text


def stage_links(order: Order) -> list[tuple[str, str]]:
    """(подпись, ссылка) для того, кто сейчас ведёт этап."""
    return [
        (LINK_LABELS[field], getattr(order, field))
        for field in STAGE_LINKS.get(order.status, ())
        if getattr(order, field)
    ]


def link_consumer_statuses(field: str) -> tuple[str, ...]:
    return tuple(status for status, fields in STAGE_LINKS.items() if field in fields)


def shoots_problem(target_status: str, shoots: list[Shoot]) -> str | None:
    """Почему нельзя перейти на этап из-за съёмок; None — можно."""
    if target_status not in (STATUS_DATE_LOCATION_SET, STATUS_SEEKING_PHOTOGRAPHER):
        return None
    if not shoots:
        return "Сначала добавь хотя бы одну съёмку: дату, время и локацию."
    if any(not shoot.date for shoot in shoots):
        return "У одной из съёмок нет даты — поставь её в «📍 Съёмки»."
    return None


def order_sort_key(order: Order) -> tuple:
    numeric_id = int(order.order_id) if order.order_id.isdigit() else 10**9
    return (order.shoot_date or "9999-99-99", numeric_id, order.order_id)


def stage_due_date(status: str, since_iso: str | None, deadlines: dict[str, int], tz: tzinfo) -> date | None:
    """Срок этапа: день начала этапа + N дней из настроек."""
    days = deadlines.get(status)
    if not days or not since_iso:
        return None
    since = datetime.fromisoformat(since_iso).astimezone(tz).date()
    return since + timedelta(days=days)


def deadline_text(due: date, today: date) -> str:
    left = (due - today).days
    if left > 0:
        return f"до {format_day(due.isoformat())} (осталось {left} дн.)"
    if left == 0:
        return f"сегодня, {format_day(due.isoformat())}"
    return f"⚠️ просрочен на {-left} дн. (был до {format_day(due.isoformat())})"


@dataclass(frozen=True)
class OrderAccess:
    is_owner: bool
    is_responsible: bool
    is_claimant: bool  # взял хотя бы одну съёмку этого заказа
    can_advance: bool
    can_edit_shoots: bool
    editable_fields: tuple[str, ...]


async def get_order_access(
    db: Database, chat_id: int, owner_chat_id: int, order: Order
) -> OrderAccess:
    is_owner = chat_id == owner_chat_id
    role = stage_role_for_status(order.status)
    in_role = role is not None and chat_id in await db.get_role_assignees(role)

    photographers = set()
    for shoot in await db.list_shoots_for_order(order.order_id):
        photographer = await photographer_chat_id(db, shoot)
        if photographer is not None:
            photographers.add(photographer)
    is_claimant = chat_id in photographers

    if order.status == STATUS_SHOOT_ASSIGNED:
        # «Съёмки проведены» отмечает тот, кто снимал; если фотографов вписали
        # без claim и узнать их не вышло — любой фотограф.
        is_responsible = is_claimant or (not photographers and in_role)
    else:
        is_responsible = in_role

    photo_stage = order.status in (STATUS_SEEKING_PHOTOGRAPHER, STATUS_SHOOT_ASSIGNED)
    in_dates_role = chat_id in await db.get_role_assignees(ROLE_DATES_LOCATIONS)
    editable = EDITABLE_FIELDS if is_owner or (is_responsible and not photo_stage) else ()
    if not editable and is_claimant:
        # Фотограф заказа может выложить исходники, не дожидаясь остальных съёмок.
        editable = ("raw_photos_link",)

    return OrderAccess(
        is_owner=is_owner,
        is_responsible=is_responsible,
        is_claimant=is_claimant,
        can_advance=order.status in ADVANCE_LABELS and (is_owner or is_responsible),
        # Даты переносятся на любом этапе — это дело ответственного за даты.
        can_edit_shoots=is_owner or in_dates_role or (is_responsible and not photo_stage),
        editable_fields=editable,
    )


async def orders_waiting_for(db: Database, chat_id: int, owner_chat_id: int) -> list[Order]:
    """Заказы, по которым сейчас нужно действие именно этого человека."""
    result = []
    for order in await db.list_cached_orders():
        access = await get_order_access(db, chat_id, owner_chat_id, order)
        if access.is_responsible:
            result.append(order)
    return sorted(result, key=order_sort_key)


async def apply_order_update(
    db: Database,
    sheet: SheetClient,
    order: Order,
    *,
    values: dict[str, str] | None = None,
    status: str | None = None,
    shoots_sheet: SheetClient | None = None,
) -> Order:
    """Пишет поля и/или новый статус в таблицу, возвращает заказ после правки."""
    changes = dict(values or {})
    release = False
    if status is not None and status != order.status:
        changes["status"] = status
        target = stage_index(status)
        seeking = stage_index(STATUS_SEEKING_PHOTOGRAPHER)
        # Возврат до поиска фотографов: прежние claim больше не действуют, иначе
        # заказ тут же вернулся бы в «Съёмка назначена», а новый «Взять»
        # упёрся бы в старую блокировку.
        if target is not None and target <= seeking:
            release = True
            if order.photographer:
                changes["photographer"] = ""

    if changes:
        updates = {FIELD_COLUMNS[field]: value for field, value in changes.items()}
        await asyncio.to_thread(sheet.update_order, order.order_id, updates)
    if release:
        for shoot in await db.list_shoots_for_order(order.order_id):
            taken = shoot.photographer or await db.get_shoot_claim(shoot.shoot_id) is not None
            if taken and shoots_sheet is not None:
                await release_shoot(db, shoots_sheet, shoot)
            elif taken:
                await db.clear_shoot_claim(shoot.shoot_id)
    return replace(order, **changes)
