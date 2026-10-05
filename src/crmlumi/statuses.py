"""Статусы заказа: единый список этапов воронки (§4 дизайн-документа)."""

from __future__ import annotations

STATUS_LEAD = "Лид"
STATUS_AGREED = "Договорились"
STATUS_DATE_LOCATION_SET = "Дата и локация назначены"
STATUS_SEEKING_PHOTOGRAPHER = "Ищем фотографа"
STATUS_SHOOT_ASSIGNED = "Съёмка назначена"
STATUS_PHOTO_PROCESSING = "Обработка фото"
STATUS_LAYOUT = "Вёрстка"
STATUS_CLIENT_APPROVAL = "Согласование с клиентом"
STATUS_PRINTING = "Печать"
STATUS_DELIVERED = "Выдано"

FUNNEL_ORDER: tuple[str, ...] = (
    STATUS_LEAD,
    STATUS_AGREED,
    STATUS_DATE_LOCATION_SET,
    STATUS_SEEKING_PHOTOGRAPHER,
    STATUS_SHOOT_ASSIGNED,
    STATUS_PHOTO_PROCESSING,
    STATUS_LAYOUT,
    STATUS_CLIENT_APPROVAL,
    STATUS_PRINTING,
    STATUS_DELIVERED,
)
