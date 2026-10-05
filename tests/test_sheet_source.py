from datetime import datetime, timezone

import pytest

from crmlumi.db import Database
from crmlumi.models import Order
from crmlumi.sheet_source import _diff, poll_once, poll_shoots


def make_order(**overrides) -> Order:
    base = dict(
        order_id="1",
        school="11А",
        contact_name="Иванова",
        contact_phone="+7999",
        people_count="24",
        shoot_date="",
        locations="",
        photographer="",
        status="Лид",
        contract_amount="",
        payment_status="",
        notes="",
    )
    base.update(overrides)
    return Order(**base)


def _row(**overrides):
    row = {
        "ID": "1",
        "Школа/класс": "11А",
        "Контактное лицо": "Иванова",
        "Телефон": "+7999",
        "Кол-во человек": "24",
        "Дата съёмки": "",
        "Локации": "",
        "Ответственный фотограф": "",
        "Статус": "Лид",
        "Сумма контракта": "",
        "Оплата": "",
        "Примечания": "",
    }
    row.update(overrides)
    return row


class FakeSheetClient:
    def __init__(self, rows):
        self.rows = rows

    def fetch_rows(self):
        return self.rows


def test_diff_new_order_is_created():
    event = _diff(make_order(), None, datetime.now(timezone.utc))
    assert event.kind == "created"
    assert event.previous is None


def test_diff_status_change_wins_over_other_changes():
    previous = make_order(status="Лид", notes="старое")
    current = make_order(status="Договорились", notes="новое")
    event = _diff(current, previous, datetime.now(timezone.utc))
    assert event.kind == "status_changed"


def test_diff_date_or_location_change():
    previous = make_order(shoot_date="", locations="")
    current = make_order(shoot_date="2026-10-01", locations="Парк Горького")
    event = _diff(current, previous, datetime.now(timezone.utc))
    assert event.kind == "date_location_set"


def test_diff_other_field_change_is_updated():
    previous = make_order(contract_amount="")
    current = make_order(contract_amount="50000")
    event = _diff(current, previous, datetime.now(timezone.utc))
    assert event.kind == "updated"


def test_diff_no_change_returns_none():
    order = make_order()
    assert _diff(order, order, datetime.now(timezone.utc)) is None


@pytest.mark.asyncio
async def test_poll_once_emits_created_for_new_rows(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        sheet = FakeSheetClient([_row()])
        events = await poll_once(sheet, db)
        assert len(events) == 1
        assert events[0].kind == "created"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_poll_once_emits_nothing_when_unchanged(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        sheet = FakeSheetClient([_row()])
        await poll_once(sheet, db)
        events = await poll_once(sheet, db)
        assert events == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_poll_once_detects_status_change_only_for_changed_order(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        sheet = FakeSheetClient([_row(ID="1"), _row(ID="2")])
        await poll_once(sheet, db)
        sheet.rows = [_row(ID="1", Статус="Договорились"), _row(ID="2")]
        events = await poll_once(sheet, db)
        assert len(events) == 1
        assert events[0].order_id == "1"
        assert events[0].kind == "status_changed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_poll_once_skips_rows_without_id(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        sheet = FakeSheetClient([_row(ID="")])
        events = await poll_once(sheet, db)
        assert events == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_poll_once_records_status_since_for_new_order(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        sheet = FakeSheetClient([_row()])
        await poll_once(sheet, db)
        since = await db.get_status_since("1")
        assert since is not None
        assert since[0] == "Лид"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_poll_once_updates_status_since_on_status_change(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        sheet = FakeSheetClient([_row()])
        await poll_once(sheet, db)
        first_since = await db.get_status_since("1")
        sheet.rows = [_row(Статус="Договорились")]
        await poll_once(sheet, db)
        second_since = await db.get_status_since("1")
        assert second_since[0] == "Договорились"
        assert second_since != first_since
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_poll_once_does_not_touch_status_since_when_unrelated_field_changes(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        sheet = FakeSheetClient([_row()])
        await poll_once(sheet, db)
        first_since = await db.get_status_since("1")
        sheet.rows = [_row(**{"Сумма контракта": "50000"})]
        await poll_once(sheet, db)
        second_since = await db.get_status_since("1")
        assert second_since == first_since
    finally:
        await db.close()


class FakeShootsSheet:
    def __init__(self, rows):
        self.rows = rows

    def fetch_rows(self):
        return self.rows


def _shoot_row(**overrides):
    row = {"ID съёмки": "1-1", "ID заказа": 1, "Дата": "12.10.2026", "Время": "10:00:00",
           "Локация": "Парк", "Фотограф": ""}
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_poll_shoots_reports_created_changed_and_deleted(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        sheet = FakeShootsSheet([_shoot_row(), _shoot_row(**{"ID съёмки": "1-2"})])
        first = await poll_shoots(sheet, db)
        assert [(e.kind, e.shoot.shoot_id) for e in first] == [("created", "1-1"), ("created", "1-2")]
        assert first[0].shoot.date == "2026-10-12"
        assert first[0].shoot.time == "10:00"
        assert first[0].shoot.order_id == "1"

        assert await poll_shoots(sheet, db) == []

        sheet.rows = [_shoot_row(**{"Время": "11:00"})]
        events = await poll_shoots(sheet, db)
        assert [(e.kind, e.shoot.shoot_id) for e in events] == [("changed", "1-1"), ("deleted", "1-2")]
        assert events[0].previous.time == "10:00"
        assert [s.shoot_id for s in await db.list_cached_shoots()] == ["1-1"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_poll_forgets_orders_deleted_from_sheet(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        class Sheet:
            rows = [_row(ID="1"), _row(ID="2")]

            def fetch_rows(self):
                return self.rows

        sheet = Sheet()
        await poll_once(sheet, db)
        sheet.rows = [_row(ID="2")]
        assert await poll_once(sheet, db) == []
        assert [order.order_id for order in await db.list_cached_orders()] == ["2"]
    finally:
        await db.close()
