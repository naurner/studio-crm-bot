"""Сквозные проверки хендлеров: настоящий Dispatcher aiogram, фейковый Telegram.

Ловят то, что не видно юнит-тестам: неверные имена зависимостей в
хендлерах, порядок регистрации (кнопка посреди диалога), фильтры.
"""

from datetime import datetime
from itertools import count
from pathlib import Path

import pytest
import yaml
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SetMyCommands
from aiogram.types import CallbackQuery, Chat, Message, ReplyKeyboardMarkup, Update, User

from crmlumi.config import DEFAULT_DEADLINES, ReminderSettings
from crmlumi.db import Database
from crmlumi.host import RuntimeState, UpdateResult
from crmlumi.models import Order, Shoot
from crmlumi.notify.menus import (
    BTN_CANCEL,
    BTN_HOST,
    BTN_NEW_ORDER,
    BTN_ORDERS,
    BTN_SETTINGS,
    BTN_SKIP,
    BTN_TODO,
)
from crmlumi.notify.telegram import build_router
from crmlumi.pipeline.stages import EDITABLE_FIELDS
from crmlumi.roles import ROLE_DATES_LOCATIONS, ROLE_NEW_LEADS, ROLE_PHOTOGRAPHER, role_names

OWNER = 999
MEMBER = 100

_ids = count(1)


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests: list = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_ids),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""

    async def close(self):
        pass

    def sent(self, chat_id=None) -> list[SendMessage]:
        return [
            r
            for r in self.requests
            if isinstance(r, SendMessage) and (chat_id is None or r.chat_id == chat_id)
        ]


class FakeSheet:
    def __init__(self, rows=None):
        self.rows = list(rows) if rows is not None else [{"ID": "7"}]
        self.appended: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def fetch_rows(self):
        return self.rows

    def append_row(self, row):
        self.appended.append(row)
        self.rows.append(row)

    def update_order(self, order_id, updates):
        self.updates.append((order_id, updates))

    def delete_row(self, row_id):
        self.deleted.append(row_id)


class Harness:
    def __init__(self, db: Database, tmp_path: Path, *, supervised: bool = True, kind: str = "launchd"):
        self.session = RecordingSession()
        self.bot = Bot(token="42:TEST", session=self.session)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(build_router())
        self.sheet = FakeSheet()
        self.shoots_sheet = FakeSheet(rows=[])
        self.deadlines = dict(DEFAULT_DEADLINES)
        self.invite_links: dict[str, str] = {}
        self.config_path = tmp_path / "config.yaml"
        self.runtime = RuntimeState(project_dir=tmp_path, supervised=supervised, kind=kind)
        self.db = db

    async def _feed(self, **update_fields):
        update = Update(update_id=next(_ids), **update_fields)
        await self.dp.feed_update(
            self.bot,
            update,
            db=self.db,
            owner_chat_id=OWNER,
            sheet=self.sheet,
            ms_client_id="client",
            graph=None,
            invite_links=self.invite_links,
            runtime=self.runtime,
            config_path=self.config_path,
            shoots_sheet=self.shoots_sheet,
            deadlines=self.deadlines,
            reminder_settings=ReminderSettings(),
        )

    async def text(self, chat_id: int, text: str):
        await self._feed(
            message=Message(
                message_id=next(_ids),
                date=datetime.now(),
                chat=Chat(id=chat_id, type="private"),
                from_user=User(id=chat_id, is_bot=False, first_name=f"User{chat_id}"),
                text=text,
            )
        )

    async def press(self, chat_id: int, data: str):
        await self._feed(
            callback_query=CallbackQuery(
                id=str(next(_ids)),
                from_user=User(id=chat_id, is_bot=False, first_name=f"User{chat_id}"),
                chat_instance="test",
                data=data,
                message=Message(
                    message_id=next(_ids),
                    date=datetime.now(),
                    chat=Chat(id=chat_id, type="private"),
                    text="старое сообщение",
                ),
            )
        )

    def last_text(self, chat_id: int) -> str:
        return self.session.sent(chat_id)[-1].text

    def last_edit(self) -> EditMessageText:
        return [r for r in self.session.requests if isinstance(r, EditMessageText)][-1]

    def alerts(self) -> list[str]:
        return [
            r.text
            for r in self.session.requests
            if isinstance(r, AnswerCallbackQuery) and r.show_alert
        ]


def callbacks_of(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


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


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.connect()
    yield database
    await database.close()


async def test_start_registers_member_gives_keyboard_and_tells_owner(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(MEMBER, "/start")

    assert [u["chat_id"] for u in await db.list_users()] == [MEMBER]
    welcome = h.session.sent(MEMBER)[0]
    assert isinstance(welcome.reply_markup, ReplyKeyboardMarkup)
    assert any(isinstance(r, SetMyCommands) for r in h.session.requests)
    assert "новый участник" in h.last_text(OWNER)


async def test_start_sends_todo_links_to_new_member(db, tmp_path):
    h = Harness(db, tmp_path)
    h.invite_links["Фотограф"] = "https://to-do/y"
    await h.text(MEMBER, "/start")
    urls = [
        b.url
        for m in h.session.sent(MEMBER)
        if m.reply_markup is not None and hasattr(m.reply_markup, "inline_keyboard")
        for row in m.reply_markup.inline_keyboard
        for b in row
        if b.url
    ]
    assert urls == ["https://to-do/y"]


async def test_owner_creates_order_by_tapping_and_answering(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(OWNER, BTN_NEW_ORDER)
    for answer in ["11А, школа 5", "Иванова Мария", "+79991234567", "24"]:
        await h.text(OWNER, answer)

    assert len(h.sheet.appended) == 1
    row = h.sheet.appended[0]
    assert row["ID"] == "8"
    assert row["Школа/класс"] == "11А, школа 5"
    assert "Заказ №8 создан" in h.last_text(OWNER)
    assert isinstance(h.session.sent(OWNER)[-1].reply_markup, ReplyKeyboardMarkup)


async def test_cancel_button_stops_new_order_dialog(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(OWNER, BTN_NEW_ORDER)
    await h.text(OWNER, "11А")
    await h.text(OWNER, BTN_CANCEL)
    await h.text(OWNER, "Иванова Мария")
    await h.text(OWNER, "+7999")
    await h.text(OWNER, "24")
    assert h.sheet.appended == []
    assert h.session.sent(OWNER)[2].text == "Отменено."


async def test_menu_button_in_the_middle_of_dialog_is_not_taken_as_answer(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(OWNER, BTN_NEW_ORDER)
    await h.text(OWNER, BTN_TODO)
    for answer in ["Иванова", "+7999", "24"]:
        await h.text(OWNER, answer)
    assert h.sheet.appended == []


async def test_member_cannot_add_order_or_open_host_menu(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(MEMBER, "/new_order")
    assert "Заявки может добавлять" in h.last_text(MEMBER)
    await h.text(MEMBER, BTN_HOST)
    assert "только для владельца" in h.last_text(MEMBER)


async def test_cyrillic_command_aliases_still_work(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(OWNER, "/меню")
    assert h.last_text(OWNER) == "Главное меню:"


async def test_owner_saves_invite_link_through_bot(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.press(OWNER, "invite:0")
    await h.text(OWNER, "не ссылка")
    assert "не похоже на ссылку" in h.last_text(OWNER)
    await h.text(OWNER, "https://to-do.live.com/tasks/sharing?InvitationToken=abc")

    role = role_names()[0]
    assert h.invite_links[role] == "https://to-do.live.com/tasks/sharing?InvitationToken=abc"
    saved = yaml.safe_load(h.config_path.read_text(encoding="utf-8"))
    assert saved["microsoft"]["lists"][role]["invite_link"].startswith("https://to-do.live.com")


async def test_restart_button_asks_supervisor_to_restart(db, tmp_path):
    h = Harness(db, tmp_path, supervised=True)
    await h.press(OWNER, "host:restart")
    assert h.runtime.restart_requested.is_set()
    assert any(isinstance(r, EditMessageText) for r in h.session.requests)


async def test_restart_button_refuses_when_started_from_terminal(db, tmp_path):
    h = Harness(db, tmp_path, supervised=False, kind="manual")
    await h.press(OWNER, "host:restart")
    assert not h.runtime.restart_requested.is_set()
    alerts = [r for r in h.session.requests if isinstance(r, AnswerCallbackQuery)]
    assert alerts and alerts[-1].show_alert


async def test_host_status_button_shows_status(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.press(OWNER, "host:status")
    edits = [r for r in h.session.requests if isinstance(r, EditMessageText)]
    assert "Статус CRM Lumi" in edits[-1].text


async def test_assigning_role_notifies_person_with_new_buttons(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(MEMBER, "/start")
    new_leads_index = role_names().index("Приём заявок")
    await h.press(OWNER, f"assign:{new_leads_index}:{MEMBER}")

    note = h.session.sent(MEMBER)[-1]
    assert "назначена роль" in note.text
    assert BTN_NEW_ORDER in [b.text for row in note.reply_markup.keyboard for b in row]


async def test_any_role_can_hold_several_people(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(MEMBER, "/start")
    await h.text(200, "/start")
    index = role_names().index(ROLE_NEW_LEADS)
    await h.press(OWNER, f"assign:{index}:{MEMBER}")
    await h.press(OWNER, f"assign:{index}:200")
    assert set(await db.get_role_assignees(ROLE_NEW_LEADS)) == {MEMBER, 200}
    # Повторное нажатие убирает только этого человека.
    await h.press(OWNER, f"assign:{index}:{MEMBER}")
    assert await db.get_role_assignees(ROLE_NEW_LEADS) == [200]


async def test_orders_button_shows_stages_and_my_queue(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.toggle_role_member(ROLE_NEW_LEADS, MEMBER)
    await db.save_order(make_order(order_id="1", status="Лид"))
    await db.save_order(make_order(order_id="2", status="Печать"))
    await h.text(MEMBER, BTN_ORDERS)
    home = h.session.sent(MEMBER)[-1]
    data = callbacks_of(home.reply_markup)
    assert "ord:mine:0" in data
    assert "ord:st:0:0" in data and "ord:st:8:0" in data
    await h.press(MEMBER, "ord:mine:0")
    assert callbacks_of(h.last_edit().reply_markup)[0] == "ord:open:1"


async def test_responsible_person_moves_lead_forward_skipping_optional_amount(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.toggle_role_member(ROLE_NEW_LEADS, MEMBER)
    await db.save_order(make_order())
    await h.press(MEMBER, "ord:open:1")
    assert "ord:adv:1:0" in callbacks_of(h.last_edit().reply_markup)

    await h.press(MEMBER, "ord:adv:1:0")
    assert "Сумма контракта" in h.last_text(MEMBER)
    await h.text(MEMBER, BTN_SKIP)

    assert h.sheet.updates == [("1", {"Статус": "Договорились"})]
    assert h.runtime.poll_now.is_set()
    assert "Этап: Договорились" in h.last_text(MEMBER)


async def test_outsider_cannot_move_order(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order())
    await h.press(MEMBER, "ord:open:1")
    assert not any(d.startswith("ord:adv") for d in callbacks_of(h.last_edit().reply_markup))
    await h.press(MEMBER, "ord:adv:1:0")
    assert h.sheet.updates == []
    assert h.alerts()


async def test_stale_advance_button_does_nothing(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Договорились"))
    await h.press(OWNER, "ord:adv:1:0")
    assert h.sheet.updates == []
    assert "уже на этапе" in h.alerts()[-1]


async def test_no_questions_when_stage_needs_nothing(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Печать", layout_link="https://disk/layout"))
    await h.press(OWNER, "ord:adv:1:8")
    assert h.sheet.updates == [("1", {"Статус": "Выдано"})]
    assert "Этап: Выдано" in h.last_edit().text


async def test_owner_edits_field_and_sets_payment(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(contract_amount="40000"))
    await h.press(OWNER, f"ord:field:1:{EDITABLE_FIELDS.index('contract_amount')}")
    assert "Сейчас: 40000" in h.last_text(OWNER)
    await h.text(OWNER, "45000")
    await h.press(OWNER, "ord:payset:1:1")
    assert h.sheet.updates == [
        ("1", {"Сумма контракта": "45000"}),
        ("1", {"Оплата": "Предоплата"}),
    ]


async def test_main_button_during_order_input_is_not_saved_as_value(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order())
    await h.press(OWNER, f"ord:field:1:{EDITABLE_FIELDS.index('notes')}")
    await h.text(OWNER, BTN_ORDERS)
    await h.text(OWNER, "просто сообщение")
    assert h.sheet.updates == []


async def test_notification_button_opens_card_as_new_message(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order())
    await h.press(OWNER, "ord:show:1")
    assert "📋 Заказ №1" in h.last_text(OWNER)


def make_shoot(**overrides) -> Shoot:
    base = dict(shoot_id="1-1", order_id="1", date="2026-10-12", time="10:00", location="Парк", photographer="")
    base.update(overrides)
    return Shoot(**base)


async def test_scheduling_starts_wizard_with_calendar_time_and_location(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.toggle_role_member(ROLE_DATES_LOCATIONS, MEMBER)
    await db.save_order(make_order(status="Договорились"))
    await h.press(MEMBER, "ord:adv:1:1")
    calendar = h.session.sent(MEMBER)[-1]
    assert any(d.startswith("dp:day:") for d in callbacks_of(calendar.reply_markup))

    await h.press(MEMBER, "dp:day:20271012")
    assert "dp:h:10" in callbacks_of(h.last_edit().reply_markup)
    await h.press(MEMBER, "dp:h:10")
    await h.press(MEMBER, "dp:m:10:30")
    assert "Где съёмка" in h.last_text(MEMBER)
    await h.text(MEMBER, "Парк Горького")

    assert h.shoots_sheet.appended == [
        {"ID съёмки": "1-1", "ID заказа": "1", "Дата": "2027-10-12", "Время": "10:30",
         "Локация": "Парк Горького", "Фотограф": ""}
    ]
    shoots_screen = h.session.sent(MEMBER)[-1]
    assert "sh:done:1:1" in callbacks_of(shoots_screen.reply_markup)
    assert "sh:add:1" in callbacks_of(shoots_screen.reply_markup)

    await db.save_shoot(make_shoot(date="2027-10-12", time="10:30", location="Парк Горького"))
    await h.press(MEMBER, "sh:done:1:1")
    assert h.sheet.updates == [("1", {"Статус": "Дата и локация назначены"})]


async def test_typed_date_and_time_skip_the_pickers(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Договорились"))
    await h.press(OWNER, "sh:add:1")
    await h.text(OWNER, "31.02")
    assert "Не понял дату" in h.last_text(OWNER)
    await h.text(OWNER, "12.10.2027 в 15:30")
    await h.text(OWNER, "Школа №5")
    row = h.shoots_sheet.appended[0]
    assert (row["Дата"], row["Время"], row["Локация"]) == ("2027-10-12", "15:30", "Школа №5")


async def test_date_without_time_asks_for_time(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Договорились"))
    await h.press(OWNER, "sh:add:1")
    await h.text(OWNER, "12.10.2027")
    assert "Во сколько" in h.last_text(OWNER)
    await h.text(OWNER, "9:15")
    await h.text(OWNER, "Парк")
    assert h.shoots_sheet.appended[0]["Время"] == "09:15"


async def test_search_stage_needs_shoots(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Дата и локация назначены"))
    await h.press(OWNER, "ord:adv:1:2")
    assert h.sheet.updates == []
    assert "съёмку" in h.alerts()[-1]


async def test_photographer_takes_shoot_from_offer_and_declines(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(MEMBER, "/start")
    await db.toggle_role_member(ROLE_PHOTOGRAPHER, MEMBER)
    await db.save_order(make_order(status="Ищем фотографа"))
    await db.save_shoot(make_shoot())

    await h.press(MEMBER, "claim:1-1")
    assert h.shoots_sheet.updates == [("1-1", {"Фотограф": f"User{MEMBER}"})]
    assert h.runtime.poll_now.is_set()

    await h.text(200, "/start")
    await db.toggle_role_member(ROLE_PHOTOGRAPHER, 200)
    await h.press(200, "claim:1-1")
    assert "Уже взял" in h.alerts()[-1]

    await db.save_shoot(make_shoot(photographer=f"User{MEMBER}"))
    await h.press(MEMBER, "sh:decline:1-1")
    assert h.shoots_sheet.updates[-1] == ("1-1", {"Фотограф": ""})
    assert await db.get_shoot_claim("1-1") is None


async def test_photographers_of_order_close_shoots(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Съёмка назначена"))
    await db.save_shoot(make_shoot(photographer="Петя"))
    await db.try_claim_shoot("1-1", MEMBER)
    await h.press(MEMBER, "ord:adv:1:4")
    assert "Ссылка на диск с исходниками" in h.last_text(MEMBER)
    await h.text(MEMBER, "завтра скину")
    assert "не похоже на ссылку" in h.last_text(MEMBER)
    await h.text(MEMBER, "https://disk.yandex.ru/d/raw")
    assert h.sheet.updates == [
        ("1", {"Ссылка на исходники": "https://disk.yandex.ru/d/raw", "Статус": "Обработка фото"})
    ]


async def test_owner_moves_order_back_and_releases_shoots(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Съёмка назначена", photographer="Петя"))
    await db.save_shoot(make_shoot(photographer="Петя"))
    await db.try_claim_shoot("1-1", MEMBER)
    await h.press(OWNER, "ord:back:1:4")
    assert h.sheet.updates == [("1", {"Статус": "Ищем фотографа", "Ответственный фотограф": ""})]
    assert h.shoots_sheet.updates == [("1-1", {"Фотограф": ""})]
    assert await db.get_shoot_claim("1-1") is None


async def test_reschedule_edit_location_delete_and_assign(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(MEMBER, "/start")
    await db.toggle_role_member(ROLE_PHOTOGRAPHER, MEMBER)
    await db.save_order(make_order(status="Ищем фотографа"))
    await db.save_shoot(make_shoot())

    await h.press(OWNER, "sh:when:1-1")
    await h.text(OWNER, "13.10.2027 11:00")
    assert h.shoots_sheet.updates[-1] == ("1-1", {"Дата": "2027-10-13", "Время": "11:00"})

    await h.press(OWNER, "sh:where:1-1")
    await h.text(OWNER, "Набережная")
    assert h.shoots_sheet.updates[-1] == ("1-1", {"Локация": "Набережная"})

    await h.press(OWNER, f"sh:assignto:1-1:{MEMBER}")
    assert h.shoots_sheet.updates[-1] == ("1-1", {"Фотограф": f"User{MEMBER}"})
    assert "поставили на съёмку" in h.last_text(MEMBER)

    await h.press(OWNER, "sh:del:1-1")
    await h.press(OWNER, "sh:delok:1-1")
    assert h.shoots_sheet.deleted == ["1-1"]


async def test_outsider_cannot_touch_shoots(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Договорились"))
    await db.save_shoot(make_shoot())
    await h.press(MEMBER, "sh:add:1")
    await h.press(MEMBER, "sh:delok:1-1")
    await h.press(MEMBER, "sh:assignto:1-1:100")
    assert h.shoots_sheet.deleted == [] and h.shoots_sheet.updates == []
    assert len(h.alerts()) == 3


async def test_stale_picker_button_is_explained(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.press(OWNER, "dp:day:20271012")
    assert "устарело" in h.alerts()[-1]


async def test_settings_change_reminder_interval(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(MEMBER, BTN_SETTINGS)
    assert "раз в день" in h.last_text(MEMBER)
    await h.press(MEMBER, "set:rem:6")
    assert await db.get_reminder_hours(MEMBER) == 6
    assert "раз в 6 ч" in h.last_edit().text


async def test_owner_changes_stage_deadline(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.press(OWNER, "dl:inc:0")
    assert h.deadlines["Лид"] == 2
    assert yaml.safe_load(h.config_path.read_text(encoding="utf-8"))["deadlines"]["Лид"] == 2
    await h.press(MEMBER, "dl:inc:0")
    assert h.deadlines["Лид"] == 2


async def test_card_shows_shoots_and_stage_deadline(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Договорились"))
    await db.set_status_since("1", "Договорились", datetime.now().astimezone().isoformat())
    await db.save_shoot(make_shoot(date="2027-10-12", time="10:00", location="Парк"))
    await h.press(OWNER, "ord:open:1")
    card = h.last_edit()
    assert "Съёмки (1):" in card.text
    assert "12.10.2027 · 10:00 — Парк — ❗ нет фотографа" in card.text
    assert "Срок этапа: до" in card.text
    assert "sh:list:1" in callbacks_of(card.reply_markup)


async def test_links_can_be_skipped_on_transition(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Обработка фото"))
    await h.press(OWNER, "ord:adv:1:5")
    await h.text(OWNER, BTN_SKIP)
    assert h.sheet.updates == [("1", {"Статус": "Вёрстка"})]


async def test_link_added_later_goes_straight_to_current_stage(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(200, "/start")
    await db.toggle_role_member("Обработка фото", 200)
    await db.save_order(make_order(status="Обработка фото"))
    await db.save_shoot(make_shoot(photographer="Петя"))
    await db.try_claim_shoot("1-1", MEMBER)

    await h.press(MEMBER, f"ord:field:1:{EDITABLE_FIELDS.index('raw_photos_link')}")
    await h.text(MEMBER, "https://disk/raw2")
    assert h.sheet.updates == [("1", {"Ссылка на исходники": "https://disk/raw2"})]
    note = h.last_text(200)
    assert "📁 Исходники: https://disk/raw2" in note
    assert "получили: 1" in h.session.sent(MEMBER)[-2].text


async def test_owner_deletes_order_with_shoots(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.text(MEMBER, "/start")
    await db.save_order(make_order(status="Съёмка назначена"))
    await db.set_status_since("1", "Съёмка назначена", "2026-09-14T10:00:00+00:00")
    await db.save_shoot(make_shoot(photographer=f"User{MEMBER}"))
    await db.try_claim_shoot("1-1", MEMBER)
    h.shoots_sheet.rows = [
        {"ID съёмки": "1-1", "ID заказа": "1"},
        {"ID съёмки": "1-2", "ID заказа": "1"},
        {"ID съёмки": "2-1", "ID заказа": "2"},
    ]

    await h.press(MEMBER, "ord:delok:1")
    assert h.sheet.deleted == [] and "только владелец" in h.alerts()[-1]

    await h.press(OWNER, "ord:del:1")
    assert "Отменить это нельзя" in h.last_edit().text
    await h.press(OWNER, "ord:delok:1")

    assert h.shoots_sheet.deleted == ["1-1", "1-2"]
    assert h.sheet.deleted == ["1"]
    assert await db.get_cached_order("1") is None
    assert await db.list_shoots_for_order("1") == []
    assert await db.get_shoot_claim("1-1") is None
    assert await db.get_status_since("1") is None
    assert "Заказ отменён" in h.last_text(MEMBER)
    assert "удалён" in h.last_edit().text


async def test_card_shows_links_and_owner_delete_button(db, tmp_path):
    h = Harness(db, tmp_path)
    await db.save_order(make_order(status="Вёрстка", raw_photos_link="https://disk/raw"))
    await h.press(OWNER, "ord:open:1")
    assert "📁 Исходники: https://disk/raw" in h.last_edit().text
    assert "ord:del:1" in callbacks_of(h.last_edit().reply_markup)
    await h.press(MEMBER, "ord:open:1")
    assert "ord:del:1" not in callbacks_of(h.last_edit().reply_markup)


def fake_update(*, deps_changed: bool):
    def update(project_dir, python_executable, *args, install=True, **kwargs):
        update.install = install
        return UpdateResult(
            ok=True, changed=True, commit="def5678", output="", deps_changed=deps_changed
        )

    update.install = None
    return update


async def test_docker_update_pulls_without_pip_and_restarts(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path, kind="docker")
    update = fake_update(deps_changed=False)
    monkeypatch.setattr("crmlumi.notify.telegram.update_from_git", update)

    await h.press(OWNER, "host:update")

    assert update.install is False  # зависимости в образе, ставить их на лету нечего
    assert "Перезапускаюсь" in h.last_edit().text
    assert h.runtime.restart_requested.is_set()


async def test_docker_update_asks_for_rebuild_when_dependencies_change(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path, kind="docker")
    monkeypatch.setattr("crmlumi.notify.telegram.update_from_git", fake_update(deps_changed=True))

    await h.press(OWNER, "host:update")

    assert "docker compose up -d --build" in h.last_edit().text
    assert not h.runtime.restart_requested.is_set()


async def test_launchd_update_still_installs_the_package(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path, kind="launchd")
    update = fake_update(deps_changed=True)
    monkeypatch.setattr("crmlumi.notify.telegram.update_from_git", update)

    await h.press(OWNER, "host:update")

    assert update.install is True
    assert h.runtime.restart_requested.is_set()
