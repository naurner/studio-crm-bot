"""Определяет, какая роль реагирует на событие заказа — уведомлением и,
начиная с Фазы 4, задачей в соответствующем списке To Do."""

from __future__ import annotations

from ..models import OrderEvent
from ..roles import (
    ROLE_DATES_LOCATIONS,
    ROLE_LAYOUT,
    ROLE_NEW_LEADS,
    ROLE_PHOTO_PROCESSING,
    ROLE_PHOTOGRAPHER,
    ROLE_PRINTING,
)
from ..statuses import (
    STATUS_AGREED,
    STATUS_CLIENT_APPROVAL,
    STATUS_LAYOUT,
    STATUS_LEAD,
    STATUS_PHOTO_PROCESSING,
    STATUS_PRINTING,
    STATUS_SEEKING_PHOTOGRAPHER,
)

# «Вёрстка» и «Согласование с клиентом» — разные статусы, но один и тот же
# ответственный и один и тот же список To Do («Вёрстка и согласование»).
STATUS_ROLE_MAP: dict[str, str] = {
    STATUS_AGREED: ROLE_DATES_LOCATIONS,
    STATUS_SEEKING_PHOTOGRAPHER: ROLE_PHOTOGRAPHER,
    STATUS_PHOTO_PROCESSING: ROLE_PHOTO_PROCESSING,
    STATUS_LAYOUT: ROLE_LAYOUT,
    STATUS_CLIENT_APPROVAL: ROLE_LAYOUT,
    STATUS_PRINTING: ROLE_PRINTING,
}


def role_for_event(event: OrderEvent) -> str | None:
    if event.kind == "created":
        return ROLE_NEW_LEADS
    if event.kind == "status_changed":
        return STATUS_ROLE_MAP.get(event.order.status)
    return None


def task_role_for_status(status: str) -> str | None:
    """В чей список To Do нужна задача, пока заказ в этом статусе.

    «Фотограф» — не здесь: его задача создаётся при claim, с датой съёмки и
    именем того, кто взял заказ (pipeline/claims.py).
    """
    if status == STATUS_LEAD:
        return ROLE_NEW_LEADS
    role = STATUS_ROLE_MAP.get(status)
    return None if role == ROLE_PHOTOGRAPHER else role
