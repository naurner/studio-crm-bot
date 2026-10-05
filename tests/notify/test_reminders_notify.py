from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from crmlumi.config import ReminderSettings
from crmlumi.db import Database
from crmlumi.models import Order, Shoot, ShootEvent
from crmlumi.notify.datetime_picker import calendar_keyboard, hours_keyboard, minutes_keyboard, parse_day_callback
from crmlumi.notify.reminders import notify_shoot_changes, run_reminders
from crmlumi.notify.settings_ui import DEADLINE_STAGES, deadlines_keyboard, settings_keyboard
from crmlumi.roles import ROLE_NEW_LEADS

TZ = timezone(timedelta(hours=5))


def make_order(**overrides) -> Order:
    base = dict(
        order_id="8", school="11А", contact_name="Иванова", contact_phone="+7999", people_count="24",
        shoot_date="", locations="", photographer="", status="Лид", contract_amount="",
        payment_status="", notes="",
    )
    base.update(overrides)
    return Order(**base)


def make_shoot(**overrides) -> Shoot:
    base = dict(shoot_id="8-1", order_id="8", date="2026-09-15", time="10:00", location="Парк", photographer="Петя")
    base.update(overrides)
    return Shoot(**base)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.connect()
    yield database
    await database.close()


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_calendar_disables_past_days_and_offers_quick_picks():
    keyboard = calendar_keyboard(2026, 9, today=date(2026, 9, 14))
    data = callbacks(keyboard)
    assert "dp:day:20260913" not in data
    assert "dp:day:20260914" in data and "dp:day:20260930" in data
    assert "dp:day:20260915" in data  # «Завтра»
    assert "dp:cal:202610" in data
    assert "dp:cal:202608" not in data  # в прошлый месяц не листаем


def test_time_keyboards():
    assert "dp:h:07" in callbacks(hours_keyboard()) and "dp:notime" in callbacks(hours_keyboard())
    assert callbacks(minutes_keyboard("10"))[:4] == ["dp:m:10:00", "dp:m:10:15", "dp:m:10:30", "dp:m:10:45"]
    assert parse_day_callback("dp:day:20261012") == date(2026, 10, 12)
    assert parse_day_callback("dp:day:2026") is None


def test_settings_keyboards():
    member = callbacks(settings_keyboard(24, is_owner=False))
    assert "set:rem:0" in member and "dl:menu" not in member
    assert "dl:menu" in callbacks(settings_keyboard(24, is_owner=True))
    assert len(deadlines_keyboard({"Лид": 1}).inline_keyboard) == len(DEADLINE_STAGES) + 1


async def test_run_reminders_sends_stage_schedule_and_pre_shoot_once(db):
    settings = ReminderSettings()
    await db.register_user(100, username=None, display_name="Маша")
    await db.toggle_role_member(ROLE_NEW_LEADS, 100)
    await db.save_order(make_order())
    await db.set_status_since("8", "Лид", datetime(2026, 9, 12, 10, tzinfo=TZ).isoformat())
    await db.save_order(make_order(order_id="9", school="9Б", status="Съёмка назначена"))
    await db.save_shoot(make_shoot(shoot_id="9-1", order_id="9", date="2026-09-15", time="10:00"))
    await db.try_claim_shoot("9-1", 200)

    bot = FakeBot()
    evening = datetime(2026, 9, 14, 21, 30, tzinfo=TZ)
    await run_reminders(bot, db, 999, now=evening, settings=settings, deadlines={"Лид": 1})
    # 21:30 — тихие часы для дел, но расписание на завтра уходит.
    assert [chat_id for chat_id, _ in bot.sent] == [200]
    assert "на завтра" in bot.sent[0][1] and "10:00 — Парк" in bot.sent[0][1]

    morning = datetime(2026, 9, 15, 8, 30, tzinfo=TZ)
    await run_reminders(bot, db, 999, now=morning, settings=settings, deadlines={"Лид": 1})
    assert bot.sent[-1][0] == 200 and "Через 2 ч съёмка" in bot.sent[-1][1]

    day = datetime(2026, 9, 15, 9, 5, tzinfo=TZ)
    await run_reminders(bot, db, 999, now=day, settings=settings, deadlines={"Лид": 1})
    assert bot.sent[-1][0] == 100
    assert "позвонить клиенту" in bot.sent[-1][1] and "срок этапа истёк" in bot.sent[-1][1]

    count = len(bot.sent)
    await run_reminders(bot, db, 999, now=day + timedelta(minutes=5), settings=settings, deadlines={"Лид": 1})
    assert len(bot.sent) == count


async def test_reminders_respect_personal_interval_off(db):
    await db.register_user(100, username=None, display_name="Маша")
    await db.toggle_role_member(ROLE_NEW_LEADS, 100)
    await db.set_reminder_hours(100, 0)
    await db.save_order(make_order())
    await db.set_status_since("8", "Лид", datetime(2026, 9, 1, 10, tzinfo=TZ).isoformat())
    bot = FakeBot()
    await run_reminders(bot, db, 999, now=datetime(2026, 9, 15, 12, tzinfo=TZ), settings=ReminderSettings(), deadlines={})
    assert bot.sent == []


async def test_photographer_hears_about_reschedule_cancel_and_handover(db):
    await db.register_user(100, username=None, display_name="Петя")
    await db.register_user(200, username=None, display_name="Маша")
    await db.save_order(make_order(status="Съёмка назначена"))
    await db.try_claim_shoot("8-1", 100)
    bot = FakeBot()

    moved = ShootEvent("changed", make_shoot(time="11:00"), make_shoot())
    await notify_shoot_changes(bot, db, [moved])
    assert bot.sent[-1][0] == 100 and "было: вт, 15.09.2026 · 10:00" in bot.sent[-1][1]

    taken_by_self = ShootEvent("changed", make_shoot(), make_shoot(photographer=""))
    count = len(bot.sent)
    await notify_shoot_changes(bot, db, [taken_by_self])
    assert len(bot.sent) == count

    await db.set_shoot_claim("8-1", 200)
    handed = ShootEvent("changed", make_shoot(photographer="Маша"), make_shoot(photographer="Петя"))
    await notify_shoot_changes(bot, db, [handed])
    assert bot.sent[-1] == (100, "↪️ Съёмку 11А, вт, 15.09.2026 · 10:00 передали: Маша")

    cancelled = ShootEvent("deleted", make_shoot(photographer="Маша"), make_shoot(photographer="Маша"))
    await notify_shoot_changes(bot, db, [cancelled])
    assert bot.sent[-1][0] == 200 and "отменена" in bot.sent[-1][1]
