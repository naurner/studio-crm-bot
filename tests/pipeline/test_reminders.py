from datetime import datetime, timedelta, timezone

import pytest

from crmlumi.config import ReminderSettings
from crmlumi.db import Database
from crmlumi.models import Order, Shoot
from crmlumi.pipeline.reminders import (
    collect_pre_shoot_reminders,
    collect_schedule_digests,
    collect_stage_reminders,
    in_active_hours,
    interval_label,
)
from crmlumi.roles import ROLE_NEW_LEADS, ROLE_PHOTOGRAPHER

TZ = timezone(timedelta(hours=5))
OWNER = 999
SETTINGS = ReminderSettings()


def at(day: int, hour: int, minute: int = 0, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


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
        status="Лид",
        contract_amount="",
        payment_status="",
        notes="",
    )
    base.update(overrides)
    return Order(**base)


def make_shoot(**overrides) -> Shoot:
    base = dict(
        shoot_id="8-1", order_id="8", date="2026-09-15", time="10:00", location="Парк",
        photographer="Петя",
    )
    base.update(overrides)
    return Shoot(**base)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.connect()
    yield database
    await database.close()


def test_interval_labels():
    assert interval_label(24) == "раз в день"
    assert interval_label(48) == "раз в 2 дня"
    assert interval_label(6) == "раз в 6 ч"
    assert interval_label(0) == "выключены"


def test_quiet_hours():
    assert in_active_hours(at(14, 9), SETTINGS)
    assert not in_active_hours(at(14, 21), SETTINGS)
    assert not in_active_hours(at(14, 3), SETTINGS)


async def test_stale_lead_is_reminded_once_per_interval(db):
    await db.toggle_role_member(ROLE_NEW_LEADS, 100)
    await db.save_order(make_order())
    await db.set_status_since("8", "Лид", at(13, 10).isoformat())

    assert await collect_stage_reminders(db, 100, OWNER, now=at(13, 20), interval_hours=24) == []
    due = await collect_stage_reminders(db, 100, OWNER, now=at(14, 11), interval_hours=24)
    assert [item.order.order_id for item in due] == ["8"]
    assert due[0].days_in_stage == 1

    await db.set_last_reminder(100, "8", "Лид", at(14, 11).isoformat())
    assert await collect_stage_reminders(db, 100, OWNER, now=at(14, 18), interval_hours=24) == []
    assert await collect_stage_reminders(db, 100, OWNER, now=at(15, 12), interval_hours=24)


async def test_reminders_off_or_not_my_stage(db):
    await db.save_order(make_order())
    await db.set_status_since("8", "Лид", at(1, 10).isoformat())
    assert await collect_stage_reminders(db, 100, OWNER, now=at(14, 11), interval_hours=24) == []
    await db.toggle_role_member(ROLE_NEW_LEADS, 100)
    assert await collect_stage_reminders(db, 100, OWNER, now=at(14, 11), interval_hours=0) == []


async def test_shoot_stage_reminds_only_after_all_shoots(db):
    await db.register_user(100, username=None, display_name="Петя")
    await db.save_order(make_order(status="Съёмка назначена"))
    await db.set_status_since("8", "Съёмка назначена", at(10, 10).isoformat())
    await db.save_shoot(make_shoot())
    await db.try_claim_shoot("8-1", 100)
    assert await collect_stage_reminders(db, 100, OWNER, now=at(15, 9), interval_hours=24) == []
    assert await collect_stage_reminders(db, 100, OWNER, now=at(15, 12), interval_hours=24)


async def test_evening_schedule_for_tomorrow(db):
    await db.save_order(make_order(status="Съёмка назначена"))
    await db.save_shoot(make_shoot(date="2026-09-15"))
    await db.save_shoot(make_shoot(shoot_id="8-2", date="2026-09-16"))
    await db.try_claim_shoot("8-1", 100)
    await db.try_claim_shoot("8-2", 100)

    assert await collect_schedule_digests(db, now=at(14, 19), settings=SETTINGS) == []
    digests = await collect_schedule_digests(db, now=at(14, 20), settings=SETTINGS)
    assert len(digests) == 1
    digest = digests[0]
    assert not digest.weekly
    assert [shoot.shoot_id for _, shoot in digest.items] == ["8-1"]

    await db.mark_notice_sent(digest.notice_key)
    assert await collect_schedule_digests(db, now=at(14, 22), settings=SETTINGS) == []


async def test_sunday_sends_week_instead_of_tomorrow(db):
    await db.save_order(make_order(status="Съёмка назначена"))
    await db.save_shoot(make_shoot(date="2026-09-14"))  # пн
    await db.save_shoot(make_shoot(shoot_id="8-2", date="2026-09-20"))  # вс
    await db.save_shoot(make_shoot(shoot_id="8-3", date="2026-09-21"))  # следующий пн
    for shoot_id in ("8-1", "8-2", "8-3"):
        await db.try_claim_shoot(shoot_id, 100)
    digests = await collect_schedule_digests(db, now=at(13, 20), settings=SETTINGS)  # вс
    assert digests[0].weekly
    assert [shoot.shoot_id for _, shoot in digests[0].items] == ["8-1", "8-2"]


async def test_pre_shoot_reminder_window(db):
    await db.save_order(make_order(status="Съёмка назначена"))
    await db.save_shoot(make_shoot(date="2026-09-15", time="10:00"))
    await db.try_claim_shoot("8-1", 100)

    assert await collect_pre_shoot_reminders(db, now=at(15, 7, 59), settings=SETTINGS) == []
    due = await collect_pre_shoot_reminders(db, now=at(15, 8, 0), settings=SETTINGS)
    assert [item.chat_id for item in due] == [100]
    assert await collect_pre_shoot_reminders(db, now=at(15, 10, 0), settings=SETTINGS) == []

    await db.mark_notice_sent(due[0].notice_key)
    assert await collect_pre_shoot_reminders(db, now=at(15, 9), settings=SETTINGS) == []
    # Перенесли на 11:00 — напомним заново.
    await db.save_shoot(make_shoot(date="2026-09-15", time="11:00"))
    assert await collect_pre_shoot_reminders(db, now=at(15, 9, 30), settings=SETTINGS)


async def test_pre_shoot_skips_unassigned_and_untimed(db):
    await db.toggle_role_member(ROLE_PHOTOGRAPHER, 100)
    await db.save_order(make_order(status="Ищем фотографа"))
    await db.save_shoot(make_shoot(photographer=""))
    await db.save_shoot(make_shoot(shoot_id="8-2", time=""))
    await db.try_claim_shoot("8-2", 100)
    assert await collect_pre_shoot_reminders(db, now=at(15, 9), settings=SETTINGS) == []
