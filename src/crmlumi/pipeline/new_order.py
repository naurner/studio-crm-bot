"""Создание нового заказа из бота: генерация ID и сборка строки для Таблицы.

Только сборка данных — сама запись в Google Таблицу (append_row) и диалог
сбора полей — в notify/telegram.py.
"""

from __future__ import annotations

from ..statuses import STATUS_LEAD

COLUMN_ID = "ID"
COLUMN_SCHOOL = "Школа/класс"
COLUMN_CONTACT_NAME = "Контактное лицо"
COLUMN_CONTACT_PHONE = "Телефон"
COLUMN_PEOPLE_COUNT = "Кол-во человек"
COLUMN_STATUS = "Статус"


def next_order_id(existing_rows: list[dict[str, str]]) -> str:
    """Следующий числовой ID — максимум среди существующих плюс один."""
    max_id = 0
    for row in existing_rows:
        raw = str(row.get(COLUMN_ID, "")).strip()
        if raw.isdigit():
            max_id = max(max_id, int(raw))
    return str(max_id + 1)


def build_new_order_row(
    order_id: str,
    *,
    school: str,
    contact_name: str,
    contact_phone: str,
    people_count: str,
) -> dict[str, str]:
    return {
        COLUMN_ID: order_id,
        COLUMN_SCHOOL: school,
        COLUMN_CONTACT_NAME: contact_name,
        COLUMN_CONTACT_PHONE: contact_phone,
        COLUMN_PEOPLE_COUNT: people_count,
        COLUMN_STATUS: STATUS_LEAD,
    }
