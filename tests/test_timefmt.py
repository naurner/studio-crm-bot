from datetime import date, datetime, timedelta, timezone

import pytest

from crmlumi.timefmt import (
    combine,
    format_date,
    format_day,
    format_short,
    format_when,
    normalize_sheet_date,
    normalize_sheet_time,
    parse_datetime_text,
    parse_time_text,
)

TODAY = date(2026, 9, 14)  # понедельник


@pytest.mark.parametrize(
    "text, expected",
    [
        ("12.10", ("2026-10-12", None)),
        ("01.02", ("2027-02-01", None)),
        ("12.10.2027", ("2027-10-12", None)),
        ("12/10/27", ("2027-10-12", None)),
        ("2026-10-12", ("2026-10-12", None)),
        ("12.10 10:00", ("2026-10-12", "10:00")),
        ("12.10 в 9:30", ("2026-10-12", "09:30")),
        ("12.10 10", ("2026-10-12", "10:00")),
        ("12 октября 10:00", ("2026-10-12", "10:00")),
        ("12 окт", ("2026-10-12", None)),
        ("3 мая 2027", ("2027-05-03", None)),
        ("завтра 15:30", ("2026-09-15", "15:30")),
        ("Сегодня в 18:00", ("2026-09-14", "18:00")),
        ("послезавтра", ("2026-09-16", None)),
        ("пт 11:00", ("2026-09-18", "11:00")),
        ("в пятницу в 11:00", ("2026-09-18", "11:00")),
        ("понедельник", ("2026-09-14", None)),
        ("10:30", (None, "10:30")),
        ("10.30", (None, "10:30")),
        ("31.02", (None, None)),
        ("срочно", (None, None)),
        ("12.10 25:00", (None, None)),
    ],
)
def test_parse_datetime_text(text, expected):
    assert parse_datetime_text(text, today=TODAY) == expected


@pytest.mark.parametrize(
    "text, expected",
    [("10:00", "10:00"), ("9", "09:00"), ("9.30", "09:30"), ("24:00", None), ("abc", None)],
)
def test_parse_time_text(text, expected):
    assert parse_time_text(text) == expected


def test_normalize_sheet_values_typed_by_hand():
    assert normalize_sheet_date("12.10.2026") == "2026-10-12"
    assert normalize_sheet_date("2026-10-12") == "2026-10-12"
    assert normalize_sheet_date("когда-нибудь") == "когда-нибудь"
    assert normalize_sheet_time("10:00:00") == "10:00"
    assert normalize_sheet_time("9:05") == "09:05"


def test_combine_needs_both_date_and_time():
    tz = timezone(timedelta(hours=5))
    assert combine("2026-10-12", "10:00", tz) == datetime(2026, 10, 12, 10, 0, tzinfo=tz)
    assert combine("2026-10-12", "", tz) is None
    assert combine("", "10:00", tz) is None


def test_formats():
    assert format_date("2026-10-12") == "12.10.2026"
    assert format_day("2026-10-12") == "пн, 12.10.2026"
    assert format_when("2026-10-12", "10:00") == "пн, 12.10.2026 · 10:00"
    assert format_when("2026-10-12", "") == "пн, 12.10.2026 · время не указано"
    assert format_when("", "") == "дата не назначена"
    assert format_short("2026-10-12", "10:00") == "пн 12.10 10:00"
