from datetime import date, datetime, timedelta, timezone

import pytest

from crmlumi.db import Database
from crmlumi.models import Order, Shoot
from crmlumi.pipeline.shoots import (
    add_shoot,
    assign_shoot,
    claim_shoot,
    delete_shoot,
    migrate_legacy_shoots,
    next_shoot_id,
    photo_stage_status,
    photographer_chat_id,
    reconcile_orders_with_shoots,
    release_shoot,
    shoot_finished,
    shoots_to_offer,
    sort_shoots,
    summarize_shoots,
)

TZ = timezone(timedelta(hours=5))


def make_order(**overrides) -> Order:
    base = dict(
        order_id="8",
        school="11А",
        contact_name="Иванова",
        contact_phone="+7999",
        people_count="24",
        shoot_date="",
        locations="",
        photographer="",
        status="Ищем фотографа",
        contract_amount="",
        payment_status="",
        notes="",
    )
    base.update(overrides)
    return Order(**base)


def make_shoot(**overrides) -> Shoot:
    base = dict(
        shoot_id="8-1",
        order_id="8",
        date="2026-10-12",
        time="10:00",
        location="Парк",
        photographer="",
    )
    base.update(overrides)
    return Shoot(**base)


class FakeSheet:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.appended: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def fetch_rows(self):
        return self.rows

    def append_row(self, row):
        self.appended.append(row)
        self.rows.append(row)

    def update_order(self, row_id, updates):
        self.updates.append((row_id, dict(updates)))

    def delete_row(self, row_id):
        self.deleted.append(row_id)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.connect()
    yield database
    await database.close()


def test_next_shoot_id_continues_numbering_per_order():
    rows = [{"ID съёмки": "8-1"}, {"ID съёмки": "8-3"}, {"ID съёмки": "9-5"}]
    assert next_shoot_id("8", rows) == "8-4"
    assert next_shoot_id("10", rows) == "10-1"


def test_sort_shoots_by_date_then_time():
    shoots = [
        make_shoot(shoot_id="8-1", date="2026-10-13", time="09:00"),
        make_shoot(shoot_id="8-2", date="2026-10-12", time="15:00"),
        make_shoot(shoot_id="8-3", date="2026-10-12", time="10:00"),
    ]
    assert [s.shoot_id for s in sort_shoots(shoots)] == ["8-3", "8-2", "8-1"]


def test_summarize_shoots_builds_order_columns():
    summary = summarize_shoots(
        [
            make_shoot(shoot_id="8-2", date="2026-10-13", time="15:00", location="Школа", photographer="Маша"),
            make_shoot(shoot_id="8-1", photographer="Петя"),
        ]
    )
    assert summary == {
        "shoot_date": "2026-10-12",
        "locations": "пн 12.10 10:00 Парк; вт 13.10 15:00 Школа",
        "photographer": "Петя, Маша",
    }


def test_photo_stage_status_follows_shoots():
    taken = [make_shoot(photographer="Петя")]
    free = [make_shoot(photographer="Петя"), make_shoot(shoot_id="8-2")]
    assert photo_stage_status("Ищем фотографа", taken) == "Съёмка назначена"
    assert photo_stage_status("Ищем фотографа", free) is None
    assert photo_stage_status("Съёмка назначена", free) == "Ищем фотографа"
    assert photo_stage_status("Съёмка назначена", taken) is None
    assert photo_stage_status("Лид", taken) is None


def test_shoot_finished():
    now = datetime(2026, 10, 12, 11, 0, tzinfo=TZ)
    assert shoot_finished(make_shoot(time="10:00"), now)
    assert not shoot_finished(make_shoot(time="12:00"), now)
    assert not shoot_finished(make_shoot(time=""), now)
    assert shoot_finished(make_shoot(date="2026-10-11", time=""), now)


async def test_add_shoot_appends_row_with_next_id(db):
    sheet = FakeSheet([{"ID съёмки": "8-1"}])
    shoot = await add_shoot(sheet, "8", date="2026-10-13", time="15:00", location="Школа")
    assert shoot.shoot_id == "8-2"
    assert sheet.appended == [
        {"ID съёмки": "8-2", "ID заказа": "8", "Дата": "2026-10-13", "Время": "15:00",
         "Локация": "Школа", "Фотограф": ""}
    ]


async def test_claim_shoot_first_wins_and_writes_name(db):
    await db.register_user(100, username=None, display_name="Петя")
    await db.register_user(200, username=None, display_name="Маша")
    sheet = FakeSheet()
    assert await claim_shoot(db, sheet, make_shoot(), 100) == 100
    assert await claim_shoot(db, sheet, make_shoot(), 200) == 100
    assert sheet.updates == [("8-1", {"Фотограф": "Петя"})]


async def test_release_and_assign_shoot(db):
    await db.register_user(200, username=None, display_name="Маша")
    sheet = FakeSheet()
    await db.try_claim_shoot("8-1", 100)
    await db.save_claim_broadcast("8-1", 100, 5)
    released = await release_shoot(db, sheet, make_shoot(photographer="Петя"))
    assert released.photographer == ""
    assert await db.get_shoot_claim("8-1") is None
    assert await db.get_claim_broadcasts("8-1") == []

    assigned = await assign_shoot(db, sheet, make_shoot(), 200)
    assert assigned.photographer == "Маша"
    assert await db.get_shoot_claim("8-1") == 200


async def test_photographer_chat_id_falls_back_to_display_name(db):
    await db.register_user(300, username="lena", display_name=None)
    assert await photographer_chat_id(db, make_shoot(photographer="@lena")) == 300
    assert await photographer_chat_id(db, make_shoot(photographer="Незнакомец")) is None


async def test_delete_last_shoot_clears_order_summary(db):
    shoots_sheet, orders_sheet = FakeSheet(), FakeSheet()
    await db.try_claim_shoot("8-1", 100)
    order = make_order(shoot_date="2026-10-12", locations="пн 12.10 10:00 Парк")
    await delete_shoot(db, shoots_sheet, orders_sheet, make_shoot(), order=order, remaining=[])
    assert shoots_sheet.deleted == ["8-1"]
    assert await db.get_shoot_claim("8-1") is None
    assert orders_sheet.updates == [("8", {"Дата съёмки": "", "Локации": ""})]


async def test_reconcile_writes_summary_and_moves_stage(db):
    await db.save_order(make_order())
    await db.save_shoot(make_shoot(photographer="Петя"))
    await db.save_order(make_order(order_id="9", status="Лид"))  # без съёмок — не трогаем
    sheet = FakeSheet()
    assert await reconcile_orders_with_shoots(db, sheet) == 1
    assert sheet.updates == [
        (
            "8",
            {
                "Дата съёмки": "2026-10-12",
                "Локации": "пн 12.10 10:00 Парк",
                "Ответственный фотограф": "Петя",
                "Статус": "Съёмка назначена",
            },
        )
    ]


async def test_reconcile_is_quiet_when_everything_matches(db):
    await db.save_order(
        make_order(
            status="Съёмка назначена",
            shoot_date="2026-10-12",
            locations="пн 12.10 10:00 Парк",
            photographer="Петя",
        )
    )
    await db.save_shoot(make_shoot(photographer="Петя"))
    sheet = FakeSheet()
    assert await reconcile_orders_with_shoots(db, sheet) == 0


async def test_shoots_to_offer_skips_taken_past_and_already_sent(db):
    await db.save_order(make_order())
    await db.save_shoot(make_shoot(shoot_id="8-1"))
    await db.save_shoot(make_shoot(shoot_id="8-2", photographer="Петя"))
    await db.save_shoot(make_shoot(shoot_id="8-3", date="2026-09-01"))
    await db.save_shoot(make_shoot(shoot_id="8-4"))
    await db.save_claim_broadcast("8-4", 100, 7)
    offers = await shoots_to_offer(db, today=date(2026, 9, 14))
    assert [shoot.shoot_id for _, shoot in offers] == ["8-1"]


async def test_shoots_to_offer_forgets_broadcasts_when_order_left_search(db):
    await db.save_order(make_order(status="Договорились"))
    await db.save_shoot(make_shoot())
    await db.save_claim_broadcast("8-1", 100, 7)
    assert await shoots_to_offer(db, today=date(2026, 9, 14)) == []
    assert await db.get_claim_broadcasts("8-1") == []


async def test_migrate_legacy_shoots_moves_date_location_and_claim(db):
    await db.try_claim_order("8", 100)
    rows = [
        {"ID": "8", "Дата съёмки": "2026-10-12", "Локации": "Парк", "Ответственный фотограф": "Петя"},
        {"ID": "9", "Дата съёмки": "скоро", "Локации": "Школа"},
        {"ID": "10", "Школа/класс": "без съёмки"},
    ]
    sheet = FakeSheet()
    assert await migrate_legacy_shoots(db, rows, sheet) == 2
    assert sheet.appended[0]["ID съёмки"] == "8-1"
    assert sheet.appended[0]["Дата"] == "2026-10-12"
    assert sheet.appended[1]["Локация"] == "Школа (скоро)"
    assert await db.get_shoot_claim("8-1") == 100
