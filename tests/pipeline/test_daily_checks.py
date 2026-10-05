from datetime import datetime, timezone

import pytest

from crmlumi.db import Database
from crmlumi.models import Order, Shoot
from crmlumi.pipeline.daily_checks import (
    find_overdue_tasks,
    find_shoots_without_photographer_near_date,
    find_stalled_orders,
)
from crmlumi.statuses import STATUS_DELIVERED, STATUS_SEEKING_PHOTOGRAPHER

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


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


def make_shoot(**overrides) -> Shoot:
    base = dict(shoot_id="1-1", order_id="1", date="2026-09-15", time="10:00", location="Парк", photographer="")
    base.update(overrides)
    return Shoot(**base)


@pytest.mark.asyncio
async def test_find_shoots_without_photographer_near_date(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status=STATUS_SEEKING_PHOTOGRAPHER))
        await db.save_shoot(make_shoot(shoot_id="1-1", date="2026-09-15"))
        await db.save_shoot(make_shoot(shoot_id="1-2", date="2026-12-01"))
        await db.save_shoot(make_shoot(shoot_id="1-3", date="2026-09-14", photographer="Петя"))
        await db.save_shoot(make_shoot(shoot_id="1-4", date="скоро"))
        result = await find_shoots_without_photographer_near_date(db, now=NOW)
        assert [(order.order_id, shoot.shoot_id) for order, shoot in result] == [("1", "1-1")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_find_shoots_without_photographer_ignores_orders_past_search(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status="Обработка фото"))
        await db.save_shoot(make_shoot())
        assert await find_shoots_without_photographer_near_date(db, now=NOW) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_find_stalled_orders_flags_long_running_status(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order())
        await db.set_status_since("1", "Лид", "2026-09-01T00:00:00+00:00")
        result = await find_stalled_orders(db, now=NOW)
        assert len(result) == 1
        order, days = result[0]
        assert order.order_id == "1"
        assert days == 12
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_find_stalled_orders_ignores_recent_status(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order())
        await db.set_status_since("1", "Лид", "2026-09-12T00:00:00+00:00")
        assert await find_stalled_orders(db, now=NOW) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_find_stalled_orders_ignores_delivered(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status=STATUS_DELIVERED))
        await db.set_status_since("1", STATUS_DELIVERED, "2026-09-01T00:00:00+00:00")
        assert await find_stalled_orders(db, now=NOW) == []
    finally:
        await db.close()


class FakeGraph:
    def __init__(self, tasks_by_list):
        self._tasks_by_list = tasks_by_list

    def list_tasks(self, list_id):
        return self._tasks_by_list.get(list_id, [])


def test_find_overdue_tasks_flags_past_due_incomplete():
    graph = FakeGraph(
        {
            "list-1": [
                {
                    "title": "Перезвонить: 11А",
                    "status": "notStarted",
                    "dueDateTime": {"dateTime": "2026-09-10T00:00:00.0000000"},
                }
            ]
        }
    )
    overdue = find_overdue_tasks(graph, {"Приём заявок": "list-1"}, now=NOW)
    assert overdue == [{"role": "Приём заявок", "title": "Перезвонить: 11А"}]


def test_find_overdue_tasks_ignores_completed():
    graph = FakeGraph(
        {
            "list-1": [
                {
                    "title": "Перезвонить: 11А",
                    "status": "completed",
                    "dueDateTime": {"dateTime": "2026-09-10T00:00:00.0000000"},
                }
            ]
        }
    )
    assert find_overdue_tasks(graph, {"Приём заявок": "list-1"}, now=NOW) == []


def test_find_overdue_tasks_ignores_future_due_date():
    graph = FakeGraph(
        {
            "list-1": [
                {
                    "title": "x",
                    "status": "notStarted",
                    "dueDateTime": {"dateTime": "2026-12-01T00:00:00.0000000"},
                }
            ]
        }
    )
    assert find_overdue_tasks(graph, {"Приём заявок": "list-1"}, now=NOW) == []


def test_task_due_today_is_not_overdue_yet():
    graph = FakeGraph(
        {
            "list-1": [
                {
                    "title": "x",
                    "status": "notStarted",
                    "dueDateTime": {"dateTime": "2026-09-13T00:00:00.0000000"},
                }
            ]
        }
    )
    assert find_overdue_tasks(graph, {"Приём заявок": "list-1"}, now=NOW) == []


def test_find_overdue_tasks_ignores_tasks_without_due_date():
    graph = FakeGraph({"list-1": [{"title": "x", "status": "notStarted"}]})
    assert find_overdue_tasks(graph, {"Приём заявок": "list-1"}, now=NOW) == []


def test_find_overdue_tasks_handles_seven_digit_fractional_seconds():
    graph = FakeGraph(
        {
            "list-1": [
                {
                    "title": "x",
                    "status": "notStarted",
                    "dueDateTime": {"dateTime": "2026-09-10T00:00:00.1234567"},
                }
            ]
        }
    )
    overdue = find_overdue_tasks(graph, {"Приём заявок": "list-1"}, now=NOW)
    assert len(overdue) == 1
