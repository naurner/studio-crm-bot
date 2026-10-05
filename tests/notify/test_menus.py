import re

from crmlumi.notify.menus import (
    BTN_HOST,
    BTN_ORDERS,
    BTN_NEW_ORDER,
    BTN_ROLES,
    BTN_TODO,
    bot_commands,
    cancel_reply_keyboard,
    device_login_keyboard,
    device_login_message,
    help_text,
    host_menu_keyboard,
    invite_links_editor_keyboard,
    main_menu_keyboard,
    main_reply_keyboard,
)
from crmlumi.roles import role_names


def labels(keyboard) -> list[str]:
    return [button.text for row in keyboard.keyboard for button in row]


def callbacks(keyboard) -> list[str]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_reply_keyboard_for_member_has_only_common_buttons():
    keyboard = main_reply_keyboard(is_owner=False, can_add_order=False)
    assert BTN_ORDERS in labels(keyboard)
    assert BTN_TODO in labels(keyboard)
    assert BTN_NEW_ORDER not in labels(keyboard)
    assert BTN_ROLES not in labels(keyboard)
    assert BTN_HOST not in labels(keyboard)
    assert keyboard.is_persistent is True
    assert keyboard.resize_keyboard is True


def test_reply_keyboard_for_owner_has_roles_host_and_new_order():
    keyboard = main_reply_keyboard(is_owner=True, can_add_order=True)
    assert {BTN_NEW_ORDER, BTN_ROLES, BTN_HOST} <= set(labels(keyboard))


def test_cancel_reply_keyboard_has_single_button():
    assert len(labels(cancel_reply_keyboard())) == 1


def test_inline_main_menu_matches_access():
    member = callbacks(main_menu_keyboard(is_owner=False, can_add_order=False))
    owner = callbacks(main_menu_keyboard(is_owner=True, can_add_order=True))
    assert "ord:home" in member and "menu:todo" in member and "menu:help" in member
    assert "menu:roles" not in member and "menu:host" not in member
    assert {"menu:roles", "menu:host", "menu:new_order"} <= set(owner)


def test_bot_commands_are_latin_so_telegram_accepts_them():
    # Telegram принимает в меню команд только a-z, 0-9 и _, до 32 символов.
    for is_owner in (False, True):
        for can_add in (False, True):
            for command in bot_commands(is_owner=is_owner, can_add_order=can_add):
                assert re.fullmatch(r"[a-z0-9_]{1,32}", command.command)
                assert command.description


def test_bot_commands_depend_on_access():
    member = [c.command for c in bot_commands(is_owner=False, can_add_order=False)]
    owner = [c.command for c in bot_commands(is_owner=True, can_add_order=True)]
    assert "menu" in member and "cancel" in member
    assert "roles" not in member and "host" not in member and "new_order" not in member
    assert {"roles", "host", "new_order"} <= set(owner)


def test_help_text_mentions_owner_buttons_only_for_owner():
    assert BTN_HOST in help_text(is_owner=True, can_add_order=True)
    assert BTN_HOST not in help_text(is_owner=False, can_add_order=False)


def test_host_menu_hides_restart_when_not_supervised():
    assert "host:restart" not in callbacks(host_menu_keyboard(supervised=False))
    assert "host:restart" in callbacks(host_menu_keyboard(supervised=True))
    assert callbacks(host_menu_keyboard(supervised=True))[-1] == "menu:main"


def test_invite_links_editor_marks_filled_lists():
    first_role = role_names()[0]
    keyboard = invite_links_editor_keyboard({first_role: "https://x"})
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert texts[0].startswith("✅")
    assert texts[1].startswith("➕")
    assert callbacks(keyboard)[-1] == "menu:host"


def test_device_login_message_puts_code_in_copyable_tag():
    assert "<code>AB&lt;12</code>" in device_login_message("AB<12")


def test_device_login_keyboard_opens_verification_page():
    keyboard = device_login_keyboard("https://microsoft.com/devicelogin")
    assert keyboard.inline_keyboard[0][0].url == "https://microsoft.com/devicelogin"
