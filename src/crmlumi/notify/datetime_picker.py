"""Кнопки выбора даты и времени: календарь, часы, минуты.

Только клавиатуры; что делать с выбором — решает shoots_ui.py.
callback_data: dp:cal:YYYYMM (листать), dp:day:YYYYMMDD, dp:h:HH, dp:m:HH:MM,
dp:notime, dp:hours (назад к часам), dp:back (назад к календарю), dp:noop.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..timefmt import MONTHS_NOMINATIVE

WEEKDAY_HEADER = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
HOURS = range(7, 22)
MINUTES = ("00", "15", "30", "45")


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def calendar_keyboard(year: int, month: int, *, today: date) -> InlineKeyboardMarkup:
    prev_year, prev_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)
    can_go_back = (prev_year, prev_month) >= (today.year, today.month)
    rows = [
        [
            _button("«", f"dp:cal:{prev_year:04d}{prev_month:02d}") if can_go_back else _button("·", "dp:noop"),
            # Короткое имя: средняя кнопка из трёх на телефоне вмещает ~8 символов.
            _button(f"{MONTHS_NOMINATIVE[month - 1][:3]} {year}", "dp:noop"),
            _button("»", f"dp:cal:{next_year:04d}{next_month:02d}"),
        ],
        [_button(day, "dp:noop") for day in WEEKDAY_HEADER],
    ]
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(_button("·", "dp:noop"))
                continue
            current = date(year, month, day)
            if current < today:
                row.append(_button("·", "dp:noop"))
            else:
                label = f"[{day}]" if current == today else str(day)
                row.append(_button(label, f"dp:day:{current:%Y%m%d}"))
        rows.append(row)
    tomorrow = today + timedelta(days=1)
    rows.append(
        [
            _button("Сегодня", f"dp:day:{today:%Y%m%d}"),
            _button("Завтра", f"dp:day:{tomorrow:%Y%m%d}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hours_keyboard(*, allow_no_time: bool = True) -> InlineKeyboardMarkup:
    hours = [_button(f"{hour:02d}", f"dp:h:{hour:02d}") for hour in HOURS]
    rows = [hours[i : i + 5] for i in range(0, len(hours), 5)]
    extra = [_button("« Другая дата", "dp:back")]
    if allow_no_time:
        extra.append(_button("Без времени", "dp:notime"))
    rows.append(extra)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def minutes_keyboard(hour: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(f"{hour}:{minute}", f"dp:m:{hour}:{minute}") for minute in MINUTES],
            [_button("« Другой час", "dp:hours")],
        ]
    )


def parse_day_callback(data: str) -> date | None:
    raw = data.rsplit(":", 1)[1]
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except (ValueError, IndexError):
        return None
