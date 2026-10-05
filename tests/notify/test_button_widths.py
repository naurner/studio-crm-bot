"""На телефоне в ряд из двух кнопок влезает ~15 символов, из трёх — ~9.

Длиннее — Telegram обрезает подпись многоточием, и кнопку не понять.
"""

from datetime import date

import pytest

from crmlumi.config import DEFAULT_DEADLINES
from crmlumi.models import Shoot
from crmlumi.notify.datetime_picker import calendar_keyboard, hours_keyboard
from crmlumi.notify.menus import main_reply_keyboard
from crmlumi.notify.orders_ui import edit_fields_keyboard
from crmlumi.notify.settings_ui import deadlines_keyboard, deadlines_text, settings_keyboard
from crmlumi.notify.shoots_ui import shoot_card_keyboard
from crmlumi.pipeline.stages import EDITABLE_FIELDS

LIMITS = {2: 16, 3: 10}


def assert_fits(rows):
    for row in rows:
        limit = LIMITS.get(len(row))
        if limit is None:
            continue
        for item in row:
            assert len(item.text) <= limit, f"«{item.text}» не влезет в ряд из {len(row)}"


def test_deadlines_menu_fits_and_shows_values_in_text():
    assert_fits(deadlines_keyboard(DEFAULT_DEADLINES).inline_keyboard)
    text = deadlines_text({**DEFAULT_DEADLINES, "Печать": 0})
    assert "• Согласование с клиентом — 3 дн." in text
    assert "• Печать — без срока" in text


@pytest.mark.parametrize("is_owner", [False, True])
def test_main_keyboard_fits(is_owner):
    assert_fits(main_reply_keyboard(is_owner=is_owner, can_add_order=True).keyboard)


def test_other_menus_fit():
    assert_fits(edit_fields_keyboard("8", EDITABLE_FIELDS).inline_keyboard)
    assert_fits(settings_keyboard(24, is_owner=True).inline_keyboard)
    assert_fits(calendar_keyboard(2026, 9, today=date(2026, 9, 14)).inline_keyboard)
    assert_fits(hours_keyboard().inline_keyboard)
    shoot = Shoot("8-1", "8", "2026-10-12", "10:00", "Парк", "")
    assert_fits(
        shoot_card_keyboard(
            shoot, can_edit=True, can_claim=True, is_claimant=False, is_owner=True, searching=True
        ).inline_keyboard
    )
