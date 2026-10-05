"""Каталог ролей: кто за какой этап воронки отвечает. Без I/O.

Роли соответствуют шести спискам Microsoft To Do из дизайн-документа (§4).
В любой роли может быть сколько угодно людей.
"""

from __future__ import annotations

ROLE_NEW_LEADS = "Приём заявок"
ROLE_DATES_LOCATIONS = "Даты и локации"
ROLE_PHOTOGRAPHER = "Фотограф"
ROLE_PHOTO_PROCESSING = "Обработка фото"
ROLE_LAYOUT = "Вёрстка и согласование"
ROLE_PRINTING = "Печать и выдача"

ROLE_NAMES: tuple[str, ...] = (
    ROLE_NEW_LEADS,
    ROLE_DATES_LOCATIONS,
    ROLE_PHOTOGRAPHER,
    ROLE_PHOTO_PROCESSING,
    ROLE_LAYOUT,
    ROLE_PRINTING,
)


def role_names() -> list[str]:
    return list(ROLE_NAMES)
