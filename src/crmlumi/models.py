"""Доменные модели заказа. Без I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .timefmt import normalize_sheet_date, normalize_sheet_time

EventKind = Literal["created", "status_changed", "date_location_set", "updated"]

# Поле Order -> заголовок колонки листа «Заказы». Контракт с реальной таблицей
# (§4 дизайн-документа): по нему и читаем строки, и пишем изменения из бота.
FIELD_COLUMNS: dict[str, str] = {
    "order_id": "ID",
    "school": "Школа/класс",
    "contact_name": "Контактное лицо",
    "contact_phone": "Телефон",
    "people_count": "Кол-во человек",
    "shoot_date": "Дата съёмки",
    "locations": "Локации",
    "photographer": "Ответственный фотограф",
    "status": "Статус",
    "contract_amount": "Сумма контракта",
    "payment_status": "Оплата",
    "notes": "Примечания",
    # Передача материалов по цепочке; колонок может не быть в старой таблице —
    # бот дописывает их в заголовок при запуске (ensure_columns).
    "raw_photos_link": "Ссылка на исходники",
    "processed_photos_link": "Ссылка на обработанные",
    "layout_link": "Ссылка на макет",
    # Пишет бот виньеток (Vinyetka): прогресс учеников и ссылка-приглашение класса.
    "vignette_progress": "Виньетки",
    "vignette_link": "Виньетки: ссылка для учеников",
}


@dataclass(frozen=True)
class Order:
    """Одна строка листа «Заказы» в приведённом к типам виде."""

    order_id: str
    school: str
    contact_name: str
    contact_phone: str
    people_count: str
    shoot_date: str
    locations: str
    photographer: str
    status: str
    contract_amount: str
    payment_status: str
    notes: str
    # Со значениями по умолчанию: в кэше SQLite лежат снимки без этих полей.
    raw_photos_link: str = ""
    processed_photos_link: str = ""
    layout_link: str = ""
    vignette_progress: str = ""
    vignette_link: str = ""

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Order":
        """Строит Order из словаря "заголовок колонки -> значение".

        Названия колонок — контракт с реальным листом «Заказы» в Google
        Таблице (см. §4 дизайн-документа). Отсутствующая колонка не считается
        ошибкой — просто пустая строка, чтобы частично заполненные заказы не
        валили опрос.
        """
        return cls(
            **{
                field: str(row.get(column, "")).strip()
                for field, column in FIELD_COLUMNS.items()
            }
        )


SHOOTS_WORKSHEET = "Съёмки"

# Поле Shoot -> заголовок колонки листа «Съёмки» (лист создаёт сам бот).
SHOOT_COLUMNS: dict[str, str] = {
    "shoot_id": "ID съёмки",
    "order_id": "ID заказа",
    "date": "Дата",
    "time": "Время",
    "location": "Локация",
    "photographer": "Фотограф",
}


@dataclass(frozen=True)
class Shoot:
    """Одна съёмка заказа: дата, время, локация — отдельная работа фотографа."""

    shoot_id: str
    order_id: str
    date: str
    time: str
    location: str
    photographer: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Shoot":
        values = {field: str(row.get(column, "")).strip() for field, column in SHOOT_COLUMNS.items()}
        values["date"] = normalize_sheet_date(values["date"])
        values["time"] = normalize_sheet_time(values["time"])
        return cls(**values)

    def to_row(self) -> dict[str, str]:
        return {column: getattr(self, field) for field, column in SHOOT_COLUMNS.items()}


@dataclass(frozen=True)
class OrderEvent:
    """Событие, полученное сравнением нового снимка заказа со старым."""

    order_id: str
    kind: EventKind
    order: Order
    previous: Order | None
    timestamp: datetime


ShootEventKind = Literal["created", "changed", "deleted"]


@dataclass(frozen=True)
class ShootEvent:
    kind: ShootEventKind
    shoot: Shoot
    previous: Shoot | None
