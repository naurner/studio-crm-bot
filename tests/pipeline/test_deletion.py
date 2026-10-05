import json

import pytest

from crmlumi.db import Database
from crmlumi.models import Order, Shoot
from crmlumi.pipeline.deletion import delete_order


def make_order(**overrides) -> Order:
    base = dict(
        order_id="1", school="11А", contact_name="Иванова", contact_phone="+7999", people_count="24",
        shoot_date="", locations="", photographer="", status="Съёмка назначена", contract_amount="",
        payment_status="", notes="",
    )
    base.update(overrides)
    return Order(**base)


class FakeSheet:
    def __init__(self, rows=None, missing=()):
        self.rows = list(rows or [])
        self.deleted: list[str] = []
        self.missing = set(missing)

    def fetch_rows(self):
        return self.rows

    def delete_row(self, row_id):
        if row_id in self.missing:
            raise ValueError(row_id)
        self.deleted.append(row_id)


class FakeGraph:
    def __init__(self):
        self.deleted: list[tuple[str, str]] = []

    def delete_task(self, list_id, task_id):
        self.deleted.append((list_id, task_id))


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.connect()
    yield database
    await database.close()


async def test_delete_order_removes_rows_tasks_and_bot_data(db):
    await db.register_user(100, username=None, display_name="Петя")
    await db.save_order(make_order())
    await db.save_order(make_order(order_id="2"))
    await db.save_shoot(Shoot("1-1", "1", "2026-10-12", "10:00", "Парк", "Петя"))
    await db.try_claim_shoot("1-1", 100)
    await db.save_shoot_task("1-1", "list-photo", "task-shoot", "sig")
    await db.save_created_task("1", "Приём заявок", "list-leads", "task-lead")
    await db.set_last_reminder(100, "1", "Лид", "2026-09-14T10:00:00+05:00")
    orders_sheet = FakeSheet()
    shoots_sheet = FakeSheet([{"ID съёмки": "1-1", "ID заказа": "1"}, {"ID съёмки": "2-1", "ID заказа": "2"}])
    graph = FakeGraph()

    result = await delete_order(db, orders_sheet, shoots_sheet, "1", graph=graph)

    assert shoots_sheet.deleted == ["1-1"]
    assert orders_sheet.deleted == ["1"]
    assert sorted(graph.deleted) == [("list-leads", "task-lead"), ("list-photo", "task-shoot")]
    assert [(chat_id, shoot.shoot_id) for chat_id, shoot in result.notify] == [(100, "1-1")]
    assert await db.get_cached_order("1") is None
    assert await db.get_cached_order("2") is not None
    assert await db.get_cached_shoot("1-1") is None
    assert await db.get_shoot_task("1-1") is None
    assert await db.get_created_task("1", "Приём заявок") is None
    assert await db.get_last_reminder(100, "1", "Лид") is None


async def test_delete_order_tolerates_row_already_removed_by_hand(db):
    await db.save_order(make_order())
    orders_sheet = FakeSheet(missing={"1"})
    result = await delete_order(db, orders_sheet, FakeSheet(), "1")
    assert result.shoots_deleted == 0
    assert await db.get_cached_order("1") is None


async def test_old_cached_snapshots_without_link_fields_still_load(db):
    snapshot = {
        "order_id": "5", "school": "9Б", "contact_name": "", "contact_phone": "", "people_count": "",
        "shoot_date": "", "locations": "", "photographer": "", "status": "Лид",
        "contract_amount": "", "payment_status": "", "notes": "",
    }
    await db._connection.execute(
        "INSERT INTO order_cache (order_id, snapshot, updated_at) VALUES (?, ?, ?)",
        ("5", json.dumps(snapshot, ensure_ascii=False), "2026-09-14"),
    )
    await db._connection.commit()
    order = await db.get_cached_order("5")
    assert order.raw_photos_link == ""
