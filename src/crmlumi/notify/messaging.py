"""Мелкие обёртки над отправкой сообщений, общие для всех хендлеров."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup


async def edit_message(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        # Повторное нажатие той же кнопки — текст не изменился, это не ошибка.
        if "message is not modified" not in str(exc):
            raise
