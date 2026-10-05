import pytest

from crmlumi.db import Database
from crmlumi.models import Order


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


@pytest.mark.asyncio
async def test_get_cached_order_returns_none_when_missing(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.get_cached_order("1") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_and_get_round_trip(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        order = make_order()
        await db.save_order(order)
        cached = await db.get_cached_order("1")
        assert cached == order
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_order_overwrites_previous_snapshot(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(status="Лид"))
        await db.save_order(make_order(status="Договорились"))
        cached = await db.get_cached_order("1")
        assert cached.status == "Договорились"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_state_survives_reconnect(tmp_path):
    path = tmp_path / "test.sqlite"
    db1 = Database(path)
    await db1.connect()
    await db1.save_order(make_order())
    await db1.close()

    db2 = Database(path)
    await db2.connect()
    try:
        assert await db2.get_cached_order("1") is not None
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_try_claim_order_first_caller_becomes_winner(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        winner = await db.try_claim_order("1", 100)
        assert winner == 100
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_try_claim_order_second_caller_sees_first_winner(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.try_claim_order("1", 100)
        winner = await db.try_claim_order("1", 200)
        assert winner == 100
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_claim_returns_none_when_unclaimed(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.get_claim("1") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_claim_returns_current_holder(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.try_claim_order("1", 100)
        assert await db.get_claim("1") == 100
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_clear_claim_allows_reclaim(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.try_claim_order("1", 100)
        await db.clear_claim("1")
        assert await db.get_claim("1") is None
        winner = await db.try_claim_order("1", 200)
        assert winner == 200
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_claims_for_user(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.try_claim_order("1", 100)
        await db.try_claim_order("2", 100)
        await db.try_claim_order("3", 200)
        assert set(await db.list_claims_for_user(100)) == {"1", "2"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_user_display_name_prefers_display_name(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.register_user(100, username="alex", display_name="Алексей")
        assert await db.get_user_display_name(100) == "Алексей"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_user_display_name_falls_back_to_username(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.register_user(100, username="alex", display_name=None)
        assert await db.get_user_display_name(100) == "@alex"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_user_display_name_falls_back_to_chat_id_for_unknown_user(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.get_user_display_name(999) == "999"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_and_get_claim_broadcasts(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_claim_broadcast("1", 100, 555)
        await db.save_claim_broadcast("1", 200, 556)
        broadcasts = await db.get_claim_broadcasts("1")
        assert set(broadcasts) == {(100, 555), (200, 556)}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_clear_claim_broadcasts(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_claim_broadcast("1", 100, 555)
        await db.clear_claim_broadcasts("1")
        assert await db.get_claim_broadcasts("1") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_register_user_returns_true_for_new_user(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        is_new = await db.register_user(100, username="alex", display_name="Alex")
        assert is_new is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_register_user_returns_false_for_existing_user(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.register_user(100, username="alex", display_name="Alex")
        is_new = await db.register_user(100, username="alex", display_name="Alex Renamed")
        assert is_new is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_register_user_updates_display_name_on_repeat_start(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.register_user(100, username="alex", display_name="Alex")
        await db.register_user(100, username="alex", display_name="Alex Renamed")
        users = await db.list_users()
        assert users[0]["display_name"] == "Alex Renamed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_users_returns_registered_users(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.register_user(100, username="alex", display_name="Alex")
        await db.register_user(200, username="masha", display_name="Masha")
        users = await db.list_users()
        assert {u["chat_id"] for u in users} == {100, 200}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_role_assignees_empty_by_default(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.get_role_assignees("Приём заявок") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_role_assignee_replaces_previous(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_role_assignee("Приём заявок", 100)
        await db.set_role_assignee("Приём заявок", 200)
        assert await db.get_role_assignees("Приём заявок") == [200]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_toggle_role_member_adds_then_removes(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        added = await db.toggle_role_member("Фотограф", 100)
        assert added is True
        assert await db.get_role_assignees("Фотограф") == [100]

        removed = await db.toggle_role_member("Фотограф", 100)
        assert removed is False
        assert await db.get_role_assignees("Фотограф") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_toggle_role_member_supports_several_people(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.toggle_role_member("Фотограф", 100)
        await db.toggle_role_member("Фотограф", 200)
        assert set(await db.get_role_assignees("Фотограф")) == {100, 200}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_clear_role_removes_all_assignees(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.toggle_role_member("Фотограф", 100)
        await db.toggle_role_member("Фотограф", 200)
        await db.clear_role("Фотограф")
        assert await db.get_role_assignees("Фотограф") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_ms_account_returns_none_when_not_linked(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.get_ms_account(100) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_and_get_ms_account(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_ms_account(100, "ms-user-1", "alex@example.com")
        account = await db.get_ms_account(100)
        assert account == {"ms_user_id": "ms-user-1", "ms_email": "alex@example.com"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_ms_account_overwrites_previous(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_ms_account(100, "old-id", "old@example.com")
        await db.save_ms_account(100, "new-id", "new@example.com")
        account = await db.get_ms_account(100)
        assert account["ms_user_id"] == "new-id"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_created_task_returns_none_when_missing(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.get_created_task("1", "Приём заявок") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_and_get_created_task(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_created_task("1", "Приём заявок", "list-1", "task-1")
        assert await db.get_created_task("1", "Приём заявок") == "task-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_created_task_is_scoped_per_role(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_created_task("1", "Приём заявок", "list-1", "task-1")
        assert await db.get_created_task("1", "Обработка фото") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_cached_orders_empty_by_default(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.list_cached_orders() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_cached_orders_returns_saved_orders(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.save_order(make_order(order_id="1"))
        await db.save_order(make_order(order_id="2"))
        orders = await db.list_cached_orders()
        assert {o.order_id for o in orders} == {"1", "2"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_status_since_returns_none_when_missing(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.get_status_since("1") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_and_get_status_since(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_status_since("1", "Лид", "2026-09-01T00:00:00+00:00")
        assert await db.get_status_since("1") == ("Лид", "2026-09-01T00:00:00+00:00")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_status_since_overwrites_previous(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_status_since("1", "Лид", "2026-09-01T00:00:00+00:00")
        await db.set_status_since("1", "Договорились", "2026-09-05T00:00:00+00:00")
        assert await db.get_status_since("1") == ("Договорились", "2026-09-05T00:00:00+00:00")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_last_daily_check_date_returns_none_by_default(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        assert await db.get_last_daily_check_date() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_and_get_last_daily_check_date(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_last_daily_check_date("2026-09-13")
        assert await db.get_last_daily_check_date() == "2026-09-13"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_last_daily_check_date_overwrites(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    try:
        await db.set_last_daily_check_date("2026-09-13")
        await db.set_last_daily_check_date("2026-09-14")
        assert await db.get_last_daily_check_date() == "2026-09-14"
    finally:
        await db.close()
