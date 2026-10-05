"""Конфигурация: config.yaml для настроек, .env для секретов.

Разделение намеренное: config.yaml можно показывать и коммитить,
а всё, что нельзя, живёт в .env и не попадает в репозиторий.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path(os.getenv("CRMLUMI_CONFIG") or "config/config.yaml")
DEFAULT_ENV_PATH = Path(os.getenv("CRMLUMI_ENV") or ".env")


class GoogleSettings(BaseModel):
    sheet_id: str = ""
    worksheet_name: str = "Заказы"
    service_account_file: str = "config/service_account.json"


class PollSettings(BaseModel):
    interval_s: float = 30.0


class TodoListSettings(BaseModel):
    list_id: str = ""
    invite_link: str = ""


class MicrosoftSettings(BaseModel):
    client_id: str = ""
    lists: dict[str, TodoListSettings] = Field(default_factory=dict)


class DailyCheckSettings(BaseModel):
    hour_utc: int = 8
    near_photographer_days: int = 3
    stalled_days: int = 5


class ReminderSettings(BaseModel):
    """Время — локальное время Mac."""

    evening_hour: int = 20  # расписание фотографам на завтра
    weekly_weekday: int = 6  # в этот день (0 — пн, 6 — вс) вместо «на завтра» — на неделю
    pre_shoot_hours: float = 2  # напоминание перед съёмкой
    quiet_start: int = 21  # напоминания о делах не шлём с 21:00…
    quiet_end: int = 9  # …до 9:00
    default_interval_hours: int = 24


# Сколько дней даётся на этап (срок задачи в To Do и в карточке заказа).
DEFAULT_DEADLINES: dict[str, int] = {
    "Лид": 1,
    "Договорились": 3,
    "Обработка фото": 7,
    "Вёрстка": 5,
    "Согласование с клиентом": 3,
    "Печать": 7,
}


class Settings(BaseModel):
    google: GoogleSettings = Field(default_factory=GoogleSettings)
    poll: PollSettings = Field(default_factory=PollSettings)
    db_path: str = "data/crmlumi.sqlite"
    telegram_bot_token: str = ""
    telegram_owner_chat_id: str = ""
    microsoft: MicrosoftSettings = Field(default_factory=MicrosoftSettings)
    ms_owner_refresh_token: str = ""
    daily_check: DailyCheckSettings = Field(default_factory=DailyCheckSettings)
    reminders: ReminderSettings = Field(default_factory=ReminderSettings)
    deadlines: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_DEADLINES))

    @property
    def is_configured(self) -> bool:
        return bool(self.google.sheet_id) and Path(self.google.service_account_file).exists()

    @property
    def is_telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token) and bool(self.telegram_owner_chat_id)

    @property
    def is_todo_configured(self) -> bool:
        return bool(self.microsoft.client_id) and bool(self.ms_owner_refresh_token)


def load_env_file(path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Читает .env без внешней зависимости. Уже заданные переменные не трогает."""
    path = Path(path)
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(
    path: str | Path = DEFAULT_CONFIG_PATH,
    env_path: str | Path = DEFAULT_ENV_PATH,
) -> Settings:
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    env = load_env_file(env_path)
    data.setdefault(
        "telegram_bot_token",
        os.getenv("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", ""),
    )
    data.setdefault(
        "telegram_owner_chat_id",
        os.getenv("TELEGRAM_OWNER_CHAT_ID") or env.get("TELEGRAM_OWNER_CHAT_ID", ""),
    )
    data.setdefault(
        "ms_owner_refresh_token",
        os.getenv("MS_OWNER_REFRESH_TOKEN") or env.get("MS_OWNER_REFRESH_TOKEN", ""),
    )
    # Частично заданные сроки дополняются значениями по умолчанию.
    data["deadlines"] = {**DEFAULT_DEADLINES, **(data.get("deadlines") or {})}
    return Settings(**data)
