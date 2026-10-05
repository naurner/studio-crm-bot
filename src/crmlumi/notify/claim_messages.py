"""Рассылка «Нужен фотограф» по каждой съёмке и её итог после «Взять»."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..db import Database
from ..models import Order, Shoot
from ..roles import ROLE_PHOTOGRAPHER
from ..timefmt import format_when

log = logging.getLogger(__name__)


def format_shoot_offer_message(order: Order, shoot: Shoot) -> str:
    lines = [
        "📸 Нужен фотограф",
        f"Заказ №{order.order_id}: {order.school}",
        f"📅 {format_when(shoot.date, shoot.time)}",
        f"📍 {shoot.location or 'локация не указана'}",
    ]
    if order.people_count:
        lines.append(f"👥 {order.people_count} чел.")
    return "\n".join(lines)


def shoot_offer_keyboard(order_id: str, shoot_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✋ Взять", callback_data=f"claim:{shoot_id}"),
                InlineKeyboardButton(text="Не могу", callback_data=f"skip:{shoot_id}"),
            ],
            [InlineKeyboardButton(text="📋 Карточка заказа", callback_data=f"ord:show:{order_id}")],
        ]
    )


async def broadcast_shoot_offer(bot: Bot, db: Database, order: Order, shoot: Shoot) -> int:
    """Шлёт предложение всем фотографам. Возвращает, скольким ушло."""
    await db.clear_claim_broadcasts(shoot.shoot_id)
    assignees = await db.get_role_assignees(ROLE_PHOTOGRAPHER)
    text = format_shoot_offer_message(order, shoot)
    keyboard = shoot_offer_keyboard(order.order_id, shoot.shoot_id)
    sent = 0
    for chat_id in assignees:
        try:
            message = await bot.send_message(chat_id, text, reply_markup=keyboard)
            await db.save_claim_broadcast(shoot.shoot_id, chat_id, message.message_id)
            sent += 1
        except Exception:
            log.exception(
                "Не удалось разослать съёмку %s в чат %s", shoot.shoot_id, chat_id
            )
    return sent


async def finalize_shoot_broadcast(
    bot: Bot, db: Database, order: Order, shoot: Shoot, winner_chat_id: int
) -> None:
    winner_name = await db.get_user_display_name(winner_chat_id)
    when = format_when(shoot.date, shoot.time)
    for chat_id, message_id in await db.get_claim_broadcasts(shoot.shoot_id):
        text = (
            f"✅ Съёмка твоя: {order.school}, {when}, {shoot.location}"
            if chat_id == winner_chat_id
            else f"Съёмку {order.school}, {when} уже взял(а): {winner_name}"
        )
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except Exception:
            log.exception(
                "Не удалось обновить рассылку съёмки %s в чате %s", shoot.shoot_id, chat_id
            )
    await db.clear_claim_broadcasts(shoot.shoot_id)
