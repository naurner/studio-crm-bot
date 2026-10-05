"""Что человеку показывать в меню — по владельцу и ролям из базы."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from ..db import Database
from ..roles import ROLE_NEW_LEADS
from .menus import main_menu_keyboard, main_reply_keyboard


async def can_add_order(db: Database, chat_id: int, owner_chat_id: int) -> bool:
    if chat_id == owner_chat_id:
        return True
    return chat_id in await db.get_role_assignees(ROLE_NEW_LEADS)


async def reply_keyboard_for(db: Database, chat_id: int, owner_chat_id: int) -> ReplyKeyboardMarkup:
    return main_reply_keyboard(
        is_owner=chat_id == owner_chat_id,
        can_add_order=await can_add_order(db, chat_id, owner_chat_id),
    )


async def inline_menu_for(db: Database, chat_id: int, owner_chat_id: int) -> InlineKeyboardMarkup:
    return main_menu_keyboard(
        is_owner=chat_id == owner_chat_id,
        can_add_order=await can_add_order(db, chat_id, owner_chat_id),
    )
