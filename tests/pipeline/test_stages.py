from datetime import date, timedelta, timezone

import pytest

from crmlumi.db import Database
from crmlumi.models import Order, Shoot
from crmlumi.pipeline.stages import (
    ADVANCE_LABELS,
    EDITABLE_FIELDS,
    apply_order_update,
    deadline_text,
    get_order_access,
    link_consumer_statuses,
    looks_like_link,
    missing_inputs,
    next_status,
    orders_waiting_for,
    previous_status,
    shoots_problem,
    stage_due_date,
    stage_links,
    stage_role_for_status,
)
from crmlumi.roles import ROLE_DATES_LOCATIONS, ROLE_NEW_LEADS, ROLE_PHOTOGRAPHER
from crmlumi.statuses import FUNNEL_ORDER

OWNER = 999
TZ = timezone(timedelta(hours=5))


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
    base = dict(shoot_id="1-1", order_id="1", date="2026-10-12", time="10:00", location="Парк", photographer="")
    base.update(overrides)
    return Shoot(**base)


class FakeSheet:
    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    def update_order(self, row_id, updates):
        self.updates.append((row_id, updates))


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.connect()
    yield database
    await database.close()


def test_next_and_previous_follow_funnel():
    assert next_status("Лид") == "Договорились"
    assert next_status("Выдано") is None
    assert previous_status("Договорились") == "Лид"
    assert previous_status("Лид") is None
    assert next_status("опечатка") is None


def test_every_stage_except_delivered_has_responsible_role():
    for status in FUNNEL_ORDER[:-1]:
        assert stage_role_for_status(status) is not None
    assert stage_role_for_status("Выдано") is None


def test_advance_buttons_cover_all_stages_but_photographer_search_and_final():
    assert set(ADVANCE_LABELS) == set(FUNNEL_ORDER) - {"Ищем фотографа", "Выдано"}


def test_missing_inputs_asks_only_empty_contract_amount():
    assert missing_inputs(make_order(), "Договорились") == [("contract_amount", False)]
    assert missing_inputs(make_order(contract_amount="45000"), "Договорились") == []
    assert missing_inputs(make_order(), "Обработка фото") == [("raw_photos_link", False)]
    assert missing_inputs(make_order(raw_photos_link="https://disk/x"), "Обработка фото") == []
    assert missing_inputs(make_order(), "Выдано") == []


def test_shoots_problem_requires_dated_shoots_for_scheduling_stages():
    assert shoots_problem("Дата и локация назначены", []) is not None
    assert shoots_problem("Дата и локация назначены", [make_shoot(date="")]) is not None
    assert shoots_problem("Дата и локация назначены", [make_shoot()]) is None
    assert shoots_problem("Ищем фотографа", []) is not None
    assert shoots_problem("Обработка фото", []) is None


def test_stage_due_date_counts_days_from_stage_start_in_local_time():
    # 20:00 UTC 13-го — это уже 14-е по +5.
    assert stage_due_date("Лид", "2026-09-13T20:00:00+00:00", {"Лид": 1}, TZ) == date(2026, 9, 15)
    assert stage_due_date("Выдано", "2026-09-13T20:00:00+00:00", {"Лид": 1}, TZ) is None
    assert stage_due_date("Лид", None, {"Лид": 1}, TZ) is None


def test_deadline_text():
    today = date(2026, 9, 14)
    assert "осталось 2 дн." in deadline_text(date(2026, 9, 16), today)
    assert deadline_text(today, today).startswith("сегодня")
    assert "просрочен на 1 дн." in deadline_text(date(2026, 9, 13), today)


async def test_owner_can_do_everything(db):
    access = await get_order_access(db, OWNER, OWNER, make_order())
    assert access.can_advance and access.can_edit_shoots
    assert access.editable_fields == EDITABLE_FIELDS
    assert not access.is_responsible


async def test_stage_role_member_is_responsible_and_can_advance(db):
    await db.toggle_role_member(ROLE_NEW_LEADS, 100)
    access = await get_order_access(db, 100, OWNER, make_order())
    assert access.is_responsible and access.can_advance
    assert access.editable_fields == EDITABLE_FIELDS


async def test_several_people_can_share_a_role(db):
    await db.toggle_role_member(ROLE_NEW_LEADS, 100)
    await db.toggle_role_member(ROLE_NEW_LEADS, 200)
    for chat_id in (100, 200):
        assert (await get_order_access(db, chat_id, OWNER, make_order())).can_advance


async def test_outsider_can_only_view(db):
    access = await get_order_access(db, 300, OWNER, make_order())
    assert not access.can_advance and not access.can_edit_shoots
    assert access.editable_fields == ()


async def test_dates_role_edits_shoots_on_any_stage(db):
    await db.toggle_role_member(ROLE_DATES_LOCATIONS, 100)
    access = await get_order_access(db, 100, OWNER, make_order(status="Вёрстка"))
    assert not access.can_advance
    assert access.can_edit_shoots
    assert access.editable_fields == ()


async def test_photographers_do_not_advance_search_stage(db):
    await db.toggle_role_member(ROLE_PHOTOGRAPHER, 100)
    order = make_order(status="Ищем фотографа")
    access = await get_order_access(db, 100, OWNER, order)
    assert access.is_responsible and not access.can_advance
    assert not access.can_edit_shoots and access.editable_fields == ()
    assert not (await get_order_access(db, OWNER, OWNER, order)).can_advance


async def test_only_photographers_of_the_order_close_shoots(db):
    await db.toggle_role_member(ROLE_PHOTOGRAPHER, 100)
    await db.toggle_role_member(ROLE_PHOTOGRAPHER, 200)
    await db.save_shoot(make_shoot(photographer="Петя"))
    await db.try_claim_shoot("1-1", 100)
    order = make_order(status="Съёмка назначена")
    winner = await get_order_access(db, 100, OWNER, order)
    other = await get_order_access(db, 200, OWNER, order)
    assert winner.is_claimant and winner.can_advance
    assert not other.can_advance and not other.is_responsible


async def test_orders_waiting_for_lists_only_my_stages(db):
    await db.toggle_role_member(ROLE_NEW_LEADS, 100)
    await db.save_order(make_order(order_id="1", status="Лид"))
    await db.save_order(make_order(order_id="2", status="Печать"))
    waiting = await orders_waiting_for(db, 100, OWNER)
    assert [order.order_id for order in waiting] == ["1"]


async def test_apply_order_update_writes_columns_and_returns_updated_order(db):
    sheet = FakeSheet()
    updated = await apply_order_update(
        db, sheet, make_order(), values={"contract_amount": "45000"}, status="Договорились"
    )
    assert sheet.updates == [("1", {"Сумма контракта": "45000", "Статус": "Договорились"})]
    assert updated.status == "Договорились"
    assert updated.contract_amount == "45000"


async def test_moving_back_before_photographer_search_releases_shoots(db):
    await db.save_shoot(make_shoot(photographer="Петя"))
    await db.try_claim_shoot("1-1", 100)
    sheet, shoots_sheet = FakeSheet(), FakeSheet()
    order = make_order(status="Съёмка назначена", photographer="Петя")
    updated = await apply_order_update(
        db, sheet, order, status="Ищем фотографа", shoots_sheet=shoots_sheet
    )
    assert sheet.updates[0][1] == {"Статус": "Ищем фотографа", "Ответственный фотограф": ""}
    assert shoots_sheet.updates == [("1-1", {"Фотограф": ""})]
    assert updated.photographer == ""
    assert await db.get_shoot_claim("1-1") is None


async def test_moving_forward_keeps_photographers(db):
    await db.save_shoot(make_shoot(photographer="Петя"))
    await db.try_claim_shoot("1-1", 100)
    sheet, shoots_sheet = FakeSheet(), FakeSheet()
    order = make_order(status="Съёмка назначена", photographer="Петя")
    await apply_order_update(db, sheet, order, status="Обработка фото", shoots_sheet=shoots_sheet)
    assert shoots_sheet.updates == []
    assert await db.get_shoot_claim("1-1") == 100


def test_links_follow_the_chain():
    order = make_order(
        status="Печать",
        raw_photos_link="https://disk/raw",
        layout_link="https://disk/layout",
    )
    assert stage_links(order) == [("📖 Макет", "https://disk/layout")]
    assert stage_links(make_order(status="Обработка фото")) == []
    assert link_consumer_statuses("raw_photos_link") == ("Обработка фото",)
    assert link_consumer_statuses("layout_link") == ("Согласование с клиентом", "Печать")
    assert looks_like_link("вот https://disk.yandex.ru/d/abc")
    assert not looks_like_link("завтра скину")


async def test_photographer_of_order_can_add_raw_link_at_any_time(db):
    await db.save_shoot(make_shoot(photographer="Петя"))
    await db.try_claim_shoot("1-1", 100)
    access = await get_order_access(db, 100, OWNER, make_order(status="Обработка фото"))
    assert access.editable_fields == ("raw_photos_link",)
    outsider = await get_order_access(db, 200, OWNER, make_order(status="Обработка фото"))
    assert outsider.editable_fields == ()
