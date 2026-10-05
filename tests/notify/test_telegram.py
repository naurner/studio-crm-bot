from types import SimpleNamespace

import pytest

from crmlumi.db import Database
from crmlumi.models import Order, Shoot
from crmlumi.notify.claim_messages import (
    broadcast_shoot_offer,
    finalize_shoot_broadcast,
    format_shoot_offer_message,
    shoot_offer_keyboard,
)
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault, ReplyKeyboardMarkup

from crmlumi.notify.menus import BTN_NEW_ORDER
from crmlumi.notify.telegram import (
    apply_role_changes,
    broadcast_menu,
    can_add_order,
    format_daily_digest,
    format_new_lead_message,
    format_stage_message,
    notify_new_lead,
    notify_owner_new_member,
    notify_stage_role,
    role_status_text,
    roles_menu_keyboard,
    send_daily_digest,
    sync_bot_commands,
    todo_screen,
    user_picker_keyboard,
)
from crmlumi.roles import ROLE_NEW_LEADS, ROLE_PHOTO_PROCESSING, ROLE_PHOTOGRAPHER, role_names


def make_order(**overrides) -> Order:
    base = dict(
        order_id="1",
        school="11А, школа 5",
        contact_name="Иванова Мария",
        contact_phone="+79991234567",
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


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.markups: list = []
        self.edited: list[tuple[int, int, str]] = []
        self.commands: list[tuple[object, list[str]]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        message_id = len(self.sent) + 1
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(self, text, chat_id, message_id):
        self.edited.append((chat_id, message_id, text))

    async def set_my_commands(self, commands, scope=None):
        self.commands.append((scope, [command.command for command in commands]))


def test_format_new_lead_message_includes_school_and_contact():
    text = format_new_lead_message(make_order())
    assert "11А, школа 5" in text
    assert "Иванова Мария" in text
    assert "+79991234567" in text


def test_roles_menu_keyboard_has_one_button_per_role():
    keyboard = roles_menu_keyboard()
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == len(role_names()) + 1  # + кнопка "← Меню"
    assert buttons[0].callback_data == "role:0"


def test_roles_menu_keyboard_includes_back_to_menu_button():
    keyboard = roles_menu_keyboard()
    last_row = keyboard.inline_keyboard[-1]
    assert last_row[0].callback_data == "menu:main"


@pytest.mark.asyncio
async def test_can_add_order_true_for_owner(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await can_add_order(db, 999, owner_chat_id=999) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_can_add_order_true_for_new_leads_role_member(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_role_assignee(ROLE_NEW_LEADS, 100)
        assert await can_add_order(db, 100, owner_chat_id=999) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_can_add_order_false_for_unrelated_user(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await can_add_order(db, 100, owner_chat_id=999) is False
    finally:
        await db.close()


def test_role_status_text_when_nobody_assigned():
    text = role_status_text("Приём заявок", [])
    assert "никто не назначен" in text


def test_role_status_text_lists_assignee_names():
    text = role_status_text(
        "Приём заявок", [{"chat_id": 1, "username": None, "display_name": "Алексей"}]
    )
    assert "Алексей" in text


def test_user_picker_keyboard_marks_current_assignee():
    users = [
        {"chat_id": 1, "username": None, "display_name": "Алексей"},
        {"chat_id": 2, "username": "masha", "display_name": None},
    ]
    keyboard = user_picker_keyboard(0, users, current_chat_ids={1})
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert texts[0].startswith("✅")
    assert not texts[1].startswith("✅")


def test_user_picker_keyboard_falls_back_to_username_without_display_name():
    users = [{"chat_id": 2, "username": "masha", "display_name": None}]
    keyboard = user_picker_keyboard(0, users, current_chat_ids=set())
    text = keyboard.inline_keyboard[0][0].text
    assert "masha" in text


def test_user_picker_keyboard_includes_clear_option():
    keyboard = user_picker_keyboard(0, [], current_chat_ids=set())
    clear_row = keyboard.inline_keyboard[-2]
    assert clear_row[0].callback_data == "clear:0"


def test_user_picker_keyboard_includes_back_to_roles_button():
    keyboard = user_picker_keyboard(0, [], current_chat_ids=set())
    last_row = keyboard.inline_keyboard[-1]
    assert last_row[0].callback_data == "menu:roles"


@pytest.mark.asyncio
async def test_notify_new_lead_sends_to_all_assignees(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_role_assignee(ROLE_NEW_LEADS, 100)
        bot = FakeBot()
        await notify_new_lead(bot, db, make_order())
        assert len(bot.sent) == 1
        chat_id, text = bot.sent[0]
        assert chat_id == 100
        assert "11А, школа 5" in text
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_notify_new_lead_does_nothing_when_role_unassigned(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        bot = FakeBot()
        await notify_new_lead(bot, db, make_order())
        assert bot.sent == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_notify_new_lead_sends_to_several_assignees(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.toggle_role_member(ROLE_NEW_LEADS, 100)
        await db.toggle_role_member(ROLE_NEW_LEADS, 200)
        bot = FakeBot()
        await notify_new_lead(bot, db, make_order())
        assert {chat_id for chat_id, _ in bot.sent} == {100, 200}
    finally:
        await db.close()


def test_shoot_offer_message_has_date_time_and_location():
    shoot = Shoot("1-2", "1", "2026-10-01", "10:30", "Парк Горького", "")
    text = format_shoot_offer_message(make_order(), shoot)
    assert "11А, школа 5" in text
    assert "чт, 01.10.2026 · 10:30" in text
    assert "Парк Горького" in text


def test_shoot_offer_keyboard_claims_the_shoot_and_links_to_order():
    keyboard = shoot_offer_keyboard("1", "1-2")
    assert [b.callback_data for b in keyboard.inline_keyboard[0]] == ["claim:1-2", "skip:1-2"]
    assert keyboard.inline_keyboard[1][0].callback_data == "ord:show:1"


@pytest.mark.asyncio
async def test_broadcast_shoot_offer_and_finalize(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.register_user(100, username=None, display_name="Алексей")
        await db.toggle_role_member(ROLE_PHOTOGRAPHER, 100)
        await db.toggle_role_member(ROLE_PHOTOGRAPHER, 200)
        shoot = Shoot("1-1", "1", "2026-10-01", "10:30", "Парк", "")
        bot = FakeBot()
        assert await broadcast_shoot_offer(bot, db, make_order(), shoot) == 2
        assert len(await db.get_claim_broadcasts("1-1")) == 2
        await finalize_shoot_broadcast(bot, db, make_order(), shoot, winner_chat_id=100)
        edited = {chat_id: text for chat_id, _, text in bot.edited}
        assert "твоя" in edited[100]
        assert "Алексей" in edited[200]
        assert await db.get_claim_broadcasts("1-1") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_broadcast_shoot_offer_without_photographers_sends_nothing(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        bot = FakeBot()
        shoot = Shoot("1-1", "1", "2026-10-01", "10:30", "Парк", "")
        assert await broadcast_shoot_offer(bot, db, make_order(), shoot) == 0
        assert await db.get_claim_broadcasts("1-1") == []
    finally:
        await db.close()

def test_format_stage_message_names_order_and_stage():
    text = format_stage_message(make_order(order_id="8", status="Печать"), "Печать и выдача")
    assert "№8" in text
    assert "Печать" in text


@pytest.mark.asyncio
async def test_notify_stage_role_sends_to_assignee(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_role_assignee(ROLE_PHOTO_PROCESSING, 100)
        bot = FakeBot()
        await notify_stage_role(bot, db, make_order(), ROLE_PHOTO_PROCESSING)
        assert len(bot.sent) == 1
        assert bot.sent[0][0] == 100
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_notify_stage_role_does_nothing_when_unassigned(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        bot = FakeBot()
        await notify_stage_role(bot, db, make_order(), ROLE_PHOTO_PROCESSING)
        assert bot.sent == []
    finally:
        await db.close()


def test_format_daily_digest_returns_none_when_nothing_to_report():
    assert format_daily_digest([], [], []) is None


def test_format_daily_digest_lists_shoots_without_photographer():
    shoot = Shoot("1-1", "1", "2026-09-15", "10:00", "Парк", "")
    text = format_daily_digest([], [(make_order(), shoot)], [])
    assert "11А, школа 5" in text
    assert "вт, 15.09.2026 · 10:00" in text


def test_format_daily_digest_lists_stalled_orders():
    order = make_order(status="Лид")
    text = format_daily_digest([], [], [(order, 12)])
    assert "12 дн." in text


def test_format_daily_digest_lists_overdue_tasks():
    text = format_daily_digest([{"role": "Приём заявок", "title": "Перезвонить"}], [], [])
    assert "Перезвонить" in text
    assert "Приём заявок" in text


@pytest.mark.asyncio
async def test_send_daily_digest_sends_to_owner():
    bot = FakeBot()
    await send_daily_digest(bot, 999, "текст сводки")
    assert bot.sent == [(999, "текст сводки")]


def test_todo_screen_shows_url_button_per_link():
    text, keyboard = todo_screen(
        {"Приём заявок": "https://x", "Фотограф": "https://y"}, "client", is_owner=False
    )
    urls = [b.url for row in keyboard.inline_keyboard for b in row if b.url]
    assert urls == ["https://x", "https://y"]
    assert any(b.callback_data == "todo:link" for row in keyboard.inline_keyboard for b in row)


def test_todo_screen_without_links_offers_owner_to_add_them():
    text, keyboard = todo_screen({}, "", is_owner=True)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "host:invites" in callbacks


def test_todo_screen_without_links_for_member_just_explains():
    text, keyboard = todo_screen({}, "", is_owner=False)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "host:invites" not in callbacks
    assert "ещё не добавил" in text


@pytest.mark.asyncio
async def test_sync_bot_commands_sets_default_and_personal_menus(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.register_user(100, username=None, display_name="Маша")
        await db.set_role_assignee(ROLE_NEW_LEADS, 100)
        await db.register_user(200, username=None, display_name="Петя")
        bot = FakeBot()
        await sync_bot_commands(bot, db, owner_chat_id=999)

        by_scope = {
            (scope.chat_id if isinstance(scope, BotCommandScopeChat) else "default"): names
            for scope, names in bot.commands
        }
        assert isinstance(bot.commands[0][0], BotCommandScopeDefault)
        assert "new_order" in by_scope[100]
        assert "new_order" not in by_scope[200]
        assert "host" in by_scope[999]
        assert "host" not in by_scope[200]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_role_changes_notifies_added_and_removed_people(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_role_assignee(ROLE_NEW_LEADS, 200)
        bot = FakeBot()
        await apply_role_changes(
            bot, db, 999, ROLE_NEW_LEADS, before={100, 999}, after={200, 999}
        )
        texts = dict(bot.sent)
        assert "назначена роль" in texts[200]
        assert "сняли с роли" in texts[100]
        assert 999 not in texts
        # Новый человек в «Приём заявок» сразу получает кнопку «Новый заказ».
        added_keyboard = bot.markups[[chat_id for chat_id, _ in bot.sent].index(200)]
        assert isinstance(added_keyboard, ReplyKeyboardMarkup)
        labels = [b.text for row in added_keyboard.keyboard for b in row]
        assert BTN_NEW_ORDER in labels
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_broadcast_menu_sends_keyboard_to_everyone_and_links_to_members(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.register_user(999, username=None, display_name="Владелец")
        await db.register_user(100, username=None, display_name="Маша")
        bot = FakeBot()
        sent = await broadcast_menu(bot, db, 999, {"Фотограф": "https://y"}, "client")
        assert sent == 2
        assert [chat_id for chat_id, _ in bot.sent].count(999) == 1
        assert [chat_id for chat_id, _ in bot.sent].count(100) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_notify_owner_new_member_offers_role_assignment():
    bot = FakeBot()
    await notify_owner_new_member(bot, 999, "Маша")
    assert bot.sent == [(999, "В бота пришёл новый участник: Маша.")]
    assert bot.markups[0].inline_keyboard[0][0].callback_data == "menu:roles"


def test_stage_message_carries_link_for_the_stage():
    order = make_order(status="Обработка фото", raw_photos_link="https://disk/raw")
    assert "📁 Исходники: https://disk/raw" in format_stage_message(order, "Обработка фото")
    waiting = format_stage_message(make_order(status="Вёрстка"), "Вёрстка и согласование")
    assert "ещё не прислали" in waiting
