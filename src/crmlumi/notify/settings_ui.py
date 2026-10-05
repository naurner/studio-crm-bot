"""Настройки: частота напоминаний (каждый сам себе) и сроки этапов (владелец)."""

from __future__ import annotations

from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from ..config import ReminderSettings
from ..db import Database
from ..pipeline.reminders import REMINDER_CHOICES, interval_label
from ..setup_wizard import write_deadlines
from ..statuses import FUNNEL_ORDER
from .messaging import edit_message
from .order_card import button

# Этапы, у которых бывает срок (без поиска фотографа, съёмки и «Выдано»).
DEADLINE_STAGES: tuple[str, ...] = tuple(
    status
    for status in FUNNEL_ORDER
    if status not in ("Дата и локация назначены", "Ищем фотографа", "Съёмка назначена", "Выдано")
)


def choice_label(hours: int) -> str:
    if hours == 0:
        return "🔕 Выкл"
    if hours % 24 == 0:
        days = hours // 24
        return "1 день" if days == 1 else f"{days} дня"
    return f"{hours} ч"


def settings_text(hours: int, reminders: ReminderSettings) -> str:
    return (
        "⚙️ Настройки\n\n"
        f"🔔 Напоминания о твоих делах: {interval_label(hours)}.\n"
        f"Не шлём ночью с {reminders.quiet_start}:00 до {reminders.quiet_end}:00.\n\n"
        "Фотографам расписание приходит всегда: вечером на завтра, "
        f"по воскресеньям на неделю и за {reminders.pre_shoot_hours:g} ч до съёмки.\n\n"
        "Как часто напоминать?"
    )


def settings_keyboard(hours: int, *, is_owner: bool) -> InlineKeyboardMarkup:
    buttons = [
        button(f"{'✅ ' if choice == hours else ''}{choice_label(choice)}", f"set:rem:{choice}")
        for choice in REMINDER_CHOICES
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    if is_owner:
        rows.append([button("⏱ Сроки этапов", "dl:menu")])
    rows.append([button("← Меню", "menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Короткие имена для кнопок: на телефоне в полкнопки влезает ~15 символов.
SHORT_STAGE_NAMES: dict[str, str] = {
    "Обработка фото": "Обработка",
    "Согласование с клиентом": "Согласование",
}


def _days_text(days: int) -> str:
    return f"{days} дн." if days else "без срока"


def deadlines_text(deadlines: dict[str, int]) -> str:
    """Значения — в тексте сообщения, где они видны целиком на любом экране."""
    lines = ["⏱ Сроки этапов — сколько дней даётся с начала этапа:", ""]
    lines += [f"• {status} — {_days_text(deadlines.get(status, 0))}" for status in DEADLINE_STAGES]
    lines += [
        "",
        "По ним ставится срок задачи в To Do и пишется «срок истёк» в карточке и напоминаниях. "
        "Новый срок действует для новых задач.",
    ]
    return "\n".join(lines)


def deadlines_keyboard(deadlines: dict[str, int]) -> InlineKeyboardMarkup:
    rows = []
    for index, status in enumerate(DEADLINE_STAGES):
        name = SHORT_STAGE_NAMES.get(status, status)
        rows.append([button(f"➖ {name}", f"dl:dec:{index}"), button(f"➕ {name}", f"dl:inc:{index}")])
    rows.append([button("← Хост", "menu:host")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def current_hours(db: Database, chat_id: int, reminders: ReminderSettings) -> int:
    hours = await db.get_reminder_hours(chat_id)
    return reminders.default_interval_hours if hours is None else hours


async def settings_view(
    db: Database, chat_id: int, owner_chat_id: int, reminders: ReminderSettings
) -> tuple[str, InlineKeyboardMarkup]:
    hours = await current_hours(db, chat_id, reminders)
    return settings_text(hours, reminders), settings_keyboard(hours, is_owner=chat_id == owner_chat_id)


def build_settings_router() -> Router:
    router = Router()

    @router.callback_query(F.data == "set:menu")
    async def on_menu(
        callback: CallbackQuery, db: Database, owner_chat_id: int, reminder_settings: ReminderSettings
    ) -> None:
        text, keyboard = await settings_view(db, callback.from_user.id, owner_chat_id, reminder_settings)
        await edit_message(callback, text, keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("set:rem:"))
    async def on_interval(
        callback: CallbackQuery, db: Database, owner_chat_id: int, reminder_settings: ReminderSettings
    ) -> None:
        hours = int(callback.data.rsplit(":", 1)[1])
        if hours not in REMINDER_CHOICES:
            await callback.answer()
            return
        await db.set_reminder_hours(callback.from_user.id, hours)
        text, keyboard = await settings_view(db, callback.from_user.id, owner_chat_id, reminder_settings)
        await edit_message(callback, text, keyboard)
        await callback.answer(f"Напоминания: {interval_label(hours)}")

    @router.callback_query(F.data == "dl:menu")
    async def on_deadlines(callback: CallbackQuery, owner_chat_id: int, deadlines: dict[str, int]) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Это только для владельца.", show_alert=True)
            return
        await edit_message(callback, deadlines_text(deadlines), deadlines_keyboard(deadlines))
        await callback.answer()

    @router.callback_query(F.data.startswith("dl:inc:") | F.data.startswith("dl:dec:"))
    async def on_deadline_change(
        callback: CallbackQuery, owner_chat_id: int, deadlines: dict[str, int], config_path: Path
    ) -> None:
        if callback.from_user.id != owner_chat_id:
            await callback.answer("Это только для владельца.", show_alert=True)
            return
        _, action, index = callback.data.split(":")
        status = DEADLINE_STAGES[int(index)]
        days = deadlines.get(status, 0) + (1 if action == "inc" else -1)
        deadlines[status] = max(0, min(days, 60))
        write_deadlines(config_path, deadlines)
        await edit_message(callback, deadlines_text(deadlines), deadlines_keyboard(deadlines))
        await callback.answer(f"{status}: {_days_text(deadlines[status])}")

    return router
