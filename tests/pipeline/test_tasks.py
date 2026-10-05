from datetime import datetime, timedelta, timezone

import pytest

from crmlumi.db import Database
from crmlumi.models import Order, Shoot
from crmlumi.pipeline.tasks import (
    complete_finished_tasks,
    create_stage_task,
    sync_shoot_tasks,
    sync_stage_tasks,
)
from crmlumi.roles import ROLE_DATES_LOCATIONS, ROLE_NEW_LEADS, ROLE_PHOTOGRAPHER


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


class FakeGraph:
    def __init__(self):
        self.created: list[tuple[str, str]] = []
        self.schedules: list[tuple[str | None, datetime | None]] = []
        self.updated: list[tuple[str, str, str]] = []
        self.completed: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self._next_id = 1

    def create_task(self, list_id, title, *, due_date=None, remind_at=None):
        self.created.append((list_id, title))
        self.schedules.append((due_date, remind_at))
        task_id = f"task-{self._next_id}"
        self._next_id += 1
        return task_id

    def update_task(self, list_id, task_id, *, title, due_date, remind_at):
        self.updated.append((list_id, task_id, title))

    def complete_task(self, list_id, task_id):
        self.completed.append((list_id, task_id))

    def delete_task(self, list_id, task_id):
        self.deleted.append((list_id, task_id))


TZ = timezone(timedelta(hours=5))


def make_shoot(**overrides) -> Shoot:
    base = dict(shoot_id="1-1", order_id="1", date="2026-10-12", time="10:00", location="Парк", photographer="Петя")
    base.update(overrides)
    return Shoot(**base)


async def sync_shoots(db, graph):
    return await sync_shoot_tasks(db, graph, "list-photo", tz=TZ, pre_shoot_hours=2)


@pytest.mark.asyncio
async def test_stage_task_gets_due_date_from_deadlines(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order())
        await db.set_status_since("1", "Лид", "2026-09-14T06:00:00+00:00")
        graph = FakeGraph()
        await sync_stage_tasks(
            db, graph, {ROLE_NEW_LEADS: "list-leads"}, deadlines={"Лид": 1}, tz=TZ
        )
        assert graph.schedules == [("2026-09-15", None)]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_shoot_task_created_with_due_day_and_reminder(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status="Съёмка назначена"))
        await db.save_shoot(make_shoot())
        graph = FakeGraph()
        assert await sync_shoots(db, graph) == 1
        assert graph.created == [("list-photo", "Съёмка: 11А — Парк, пн 12.10 10:00 (Петя)")]
        assert graph.schedules == [("2026-10-12", datetime(2026, 10, 12, 8, 0, tzinfo=TZ))]
        assert await sync_shoots(db, graph) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_shoot_task_follows_reschedule_decline_and_completion(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status="Съёмка назначена"))
        await db.save_shoot(make_shoot())
        graph = FakeGraph()
        await sync_shoots(db, graph)

        await db.save_shoot(make_shoot(time="11:30"))
        await sync_shoots(db, graph)
        assert graph.updated == [("list-photo", "task-1", "Съёмка: 11А — Парк, пн 12.10 11:30 (Петя)")]

        await db.save_shoot(make_shoot(time="11:30", photographer=""))
        await db.save_order(make_order(status="Ищем фотографа"))
        await sync_shoots(db, graph)
        assert graph.deleted == [("list-photo", "task-1")]

        await db.save_shoot(make_shoot(photographer="Маша"))
        await sync_shoots(db, graph)
        await db.save_order(make_order(status="Обработка фото"))
        await sync_shoots(db, graph)
        assert graph.completed == [("list-photo", "task-2")]
        assert await sync_shoots(db, graph) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_shoot_task_removed_when_shoot_deleted(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status="Съёмка назначена"))
        await db.save_shoot(make_shoot())
        graph = FakeGraph()
        await sync_shoots(db, graph)
        await db.delete_cached_shoot("1-1")
        await sync_shoots(db, graph)
        assert graph.deleted == [("list-photo", "task-1")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_complete_finished_tasks_closes_task_of_passed_stage(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_created_task("1", ROLE_NEW_LEADS, "list-leads", "task-lead")
        await db.save_order(make_order(status="Договорились"))
        graph = FakeGraph()
        assert await complete_finished_tasks(db, graph) == 1
        assert graph.completed == [("list-leads", "task-lead")]
        # Второй проход ничего не закрывает повторно.
        assert await complete_finished_tasks(db, graph) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_complete_finished_tasks_keeps_task_while_same_role_owns_order(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_created_task("1", ROLE_DATES_LOCATIONS, "list-dates", "task-dates")
        await db.save_order(make_order(status="Дата и локация назначены"))
        await db.save_created_task("2", ROLE_PHOTOGRAPHER, "list-photo", "task-shoot")
        await db.save_order(make_order(order_id="2", status="Съёмка назначена"))
        graph = FakeGraph()
        assert await complete_finished_tasks(db, graph) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_complete_finished_tasks_ignores_orders_missing_from_cache(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_created_task("404", ROLE_NEW_LEADS, "list-leads", "task-x")
        graph = FakeGraph()
        assert await complete_finished_tasks(db, graph) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_stage_task_creates_and_saves(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        graph = FakeGraph()
        task_id = await create_stage_task(db, graph, "list-1", make_order(), ROLE_NEW_LEADS)
        assert task_id == "task-1"
        assert graph.created == [("list-1", "Перезвонить по заказу: 11А")]
        assert await db.get_created_task("1", ROLE_NEW_LEADS) == "task-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_stage_task_is_idempotent(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        graph = FakeGraph()
        first = await create_stage_task(db, graph, "list-1", make_order(), ROLE_NEW_LEADS)
        second = await create_stage_task(db, graph, "list-1", make_order(), ROLE_NEW_LEADS)
        assert first == second
        assert len(graph.created) == 1
    finally:
        await db.close()


class FailingGraph:
    def __init__(self):
        self.calls = 0

    def create_task(self, list_id, title, *, due_date=None):
        self.calls += 1
        raise RuntimeError("Graph недоступен")


LIST_IDS = {
    ROLE_NEW_LEADS: "list-leads",
    ROLE_DATES_LOCATIONS: "list-dates",
    ROLE_PHOTOGRAPHER: "list-photo",
}


@pytest.mark.asyncio
async def test_sync_stage_tasks_creates_callback_task_for_new_lead(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(order_id="8", school="11Б", status="Лид"))
        graph = FakeGraph()
        created = await sync_stage_tasks(db, graph, LIST_IDS)
        assert created == 1
        assert graph.created == [("list-leads", "Перезвонить по заказу: 11Б")]
        assert await db.get_created_task("8", ROLE_NEW_LEADS) == "task-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sync_stage_tasks_does_not_duplicate_on_next_poll(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status="Лид"))
        graph = FakeGraph()
        await sync_stage_tasks(db, graph, LIST_IDS)
        assert await sync_stage_tasks(db, graph, LIST_IDS) == 0
        assert len(graph.created) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sync_stage_tasks_uses_current_stage_list(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(order_id="1", status="Договорились"))
        await db.save_order(make_order(order_id="2", status="Ищем фотографа"))
        await db.save_order(make_order(order_id="3", status="Выдано"))
        graph = FakeGraph()
        await sync_stage_tasks(db, graph, LIST_IDS)
        # «Ищем фотографа» — задача появится при claim, «Выдано» — задач нет.
        assert graph.created == [("list-dates", "Назначить съёмки: 11А")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sync_stage_tasks_skips_roles_without_list(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status="Лид"))
        graph = FakeGraph()
        assert await sync_stage_tasks(db, graph, {ROLE_NEW_LEADS: ""}) == 0
        assert graph.created == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sync_stage_tasks_stops_on_first_graph_error_and_retries_later(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(order_id="1", status="Лид"))
        await db.save_order(make_order(order_id="2", status="Лид"))
        failing = FailingGraph()
        with pytest.raises(RuntimeError):
            await sync_stage_tasks(db, failing, LIST_IDS)
        assert failing.calls == 1

        graph = FakeGraph()
        assert await sync_stage_tasks(db, graph, LIST_IDS) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_stage_task_returns_none_for_photographer_role(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        graph = FakeGraph()
        result = await create_stage_task(db, graph, "list-1", make_order(), ROLE_PHOTOGRAPHER)
        assert result is None
        assert graph.created == []
    finally:
        await db.close()
