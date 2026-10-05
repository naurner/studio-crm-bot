"""Даты и время: разбор того, что человек написал, и единый формат показа.

Хранение — ISO (`2026-10-12`) и `HH:MM`: одинаково читается ботом, таблицей и
людьми. Показ — `пт, 12.10.2026 · 10:00`. Без I/O.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, tzinfo

WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS_NOMINATIVE = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)

_MONTH_STEMS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_WEEKDAY_WORDS = {
    "пн": 0, "пон": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "воскресенье": 6,
}
_RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


def local_now() -> datetime:
    """Текущее время в часовом поясе Mac — по нему живут напоминания."""
    return datetime.now().astimezone()


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _upcoming(month: int, day: int, today: date) -> date | None:
    """Дата без года — ближайшая такая, не в прошлом."""
    candidate = _safe_date(today.year, month, day)
    if candidate is None:
        # 29 февраля в невисокосный год — возможно, в следующем.
        return _safe_date(today.year + 1, month, day)
    if candidate < today:
        return _safe_date(today.year + 1, month, day)
    return candidate


def parse_time_text(text: str) -> str | None:
    """«10:00», «9:30», «10.30», «10» -> «10:00»."""
    match = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?(?:\s*ч)?\s*", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _parse_date_part(text: str, today: date) -> date | None:
    text = text.strip(" ,.")
    if not text:
        return None
    if text in _RELATIVE_DAYS:
        return today + timedelta(days=_RELATIVE_DAYS[text])

    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        return _safe_date(*(int(part) for part in iso.groups()))

    dotted = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?", text)
    if dotted:
        day, month = int(dotted.group(1)), int(dotted.group(2))
        if dotted.group(3) is None:
            return _upcoming(month, day, today)
        year_text = dotted.group(3)
        year = int(year_text) + (2000 if len(year_text) == 2 else 0)
        return _safe_date(year, month, day)

    worded = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", text)
    if worded:
        month = _MONTH_STEMS.get(worded.group(2)[:3])
        if month is None:
            return None
        day = int(worded.group(1))
        if worded.group(3):
            return _safe_date(int(worded.group(3)), month, day)
        return _upcoming(month, day, today)

    weekday = _WEEKDAY_WORDS.get(text)
    if weekday is not None:
        return today + timedelta(days=(weekday - today.weekday()) % 7)
    return None


def parse_datetime_text(text: str, *, today: date) -> tuple[str | None, str | None]:
    """Разбирает дату и/или время из одной строки.

    Возвращает (дата ISO или None, время HH:MM или None). Если в строке есть
    что-то, что не удалось понять, — (None, None), чтобы не угадывать.
    """
    rest = " " + text.lower().replace("ё", "е").strip() + " "
    rest = re.sub(r"\s+в\s+", " ", rest)

    parsed_time = None
    colon = re.search(r"\s(\d{1,2}:\d{2})\s", rest)
    if colon:
        parsed_time = parse_time_text(colon.group(1))
        if parsed_time is None:
            return None, None
        rest = rest[: colon.start()] + " " + rest[colon.end():]
    else:
        hours = re.search(r"\s(\d{1,2})\s*ч\s", rest)
        if hours:
            parsed_time = parse_time_text(hours.group(1))
            rest = rest[: hours.start()] + " " + rest[hours.end():]

    rest = rest.strip()
    if not rest:
        return None, parsed_time
    parsed_date = _parse_date_part(rest, today)
    if parsed_date is None:
        # «12.10 10» — последний кусок может быть часом.
        head, _, tail = rest.rpartition(" ")
        tail_time = parse_time_text(tail) if head and parsed_time is None else None
        parsed_date = _parse_date_part(head, today) if tail_time else None
        if parsed_date is None:
            only_time = parse_time_text(rest) if parsed_time is None else None
            return (None, only_time) if only_time else (None, None)
        parsed_time = tail_time
    return parsed_date.isoformat(), parsed_time


def normalize_sheet_date(value: str) -> str:
    """Дата из ячейки таблицы -> ISO; непонятное оставляем как есть."""
    value = str(value).strip()
    if not value:
        return ""
    head = value.split(" ")[0]
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", head)
    if iso:
        parsed = _safe_date(*(int(part) for part in iso.groups()))
        return parsed.isoformat() if parsed else value
    dotted = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", head)
    if dotted:
        day, month, year = (int(part) for part in dotted.groups())
        parsed = _safe_date(year, month, day)
        return parsed.isoformat() if parsed else value
    return value


def normalize_sheet_time(value: str) -> str:
    value = str(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", value)
    if match and int(match.group(1)) <= 23 and int(match.group(2)) <= 59:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return value


def parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def parse_hhmm(value: str) -> time | None:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value or "")
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def combine(date_iso: str, time_hhmm: str, tz: tzinfo) -> datetime | None:
    """Момент начала: нужны и дата, и время."""
    day, clock = parse_iso_date(date_iso), parse_hhmm(time_hhmm)
    if day is None or clock is None:
        return None
    return datetime.combine(day, clock, tzinfo=tz)


def format_date(value: str) -> str:
    """«2026-10-12» -> «12.10.2026»; непонятное — как есть."""
    day = parse_iso_date(value[:10]) if value else None
    return day.strftime("%d.%m.%Y") if day else value


def format_day(value: str) -> str:
    """«2026-10-12» -> «пн, 12.10.2026»."""
    day = parse_iso_date(value)
    if day is None:
        return value
    return f"{WEEKDAYS_SHORT[day.weekday()]}, {day.strftime('%d.%m.%Y')}"


def format_when(date_iso: str, time_hhmm: str) -> str:
    if not date_iso:
        return "дата не назначена"
    text = format_day(date_iso)
    return f"{text} · {time_hhmm}" if time_hhmm else f"{text} · время не указано"


def format_short(date_iso: str, time_hhmm: str) -> str:
    """Компактно для списков и заголовков задач: «пн 12.10 10:00»."""
    day = parse_iso_date(date_iso)
    if day is None:
        return date_iso or "без даты"
    text = f"{WEEKDAYS_SHORT[day.weekday()]} {day.strftime('%d.%m')}"
    return f"{text} {time_hhmm}" if time_hhmm else text
