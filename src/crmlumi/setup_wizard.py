"""Пишет config.yaml, не трогая настройки, не относящиеся к текущему шагу."""

from __future__ import annotations

from pathlib import Path

import yaml


def write_config(
    path: str | Path,
    *,
    sheet_id: str,
    worksheet_name: str,
    service_account_file: str,
) -> None:
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    google = data.setdefault("google", {})
    google["sheet_id"] = sheet_id
    google["worksheet_name"] = worksheet_name
    google["service_account_file"] = service_account_file
    data.setdefault("poll", {"interval_s": 30.0})
    data.setdefault("db_path", "data/crmlumi.sqlite")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def write_env(path: str | Path, values: dict[str, str]) -> None:
    """Добавляет или обновляет ключи, сохраняя остальные строки файла."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out: list[str] = []

    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    out.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def write_microsoft_config(
    path: str | Path, *, client_id: str, lists: dict[str, dict[str, str]]
) -> None:
    """Сохраняет client_id и ID списков. Не затирает уже вставленные вручную
    ссылки-приглашения при повторном запуске."""
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    microsoft = data.setdefault("microsoft", {})
    microsoft["client_id"] = client_id
    existing_lists = microsoft.setdefault("lists", {})
    for role_name, info in lists.items():
        entry = existing_lists.setdefault(role_name, {})
        entry["list_id"] = info["list_id"]
        entry.setdefault("invite_link", info.get("invite_link", ""))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def write_deadlines(path: str | Path, deadlines: dict[str, int]) -> None:
    """Сроки этапов — владелец меняет их кнопками в боте."""
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["deadlines"] = dict(deadlines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def write_invite_link(path: str | Path, role_name: str, link: str) -> None:
    """Сохраняет ссылку-приглашение одного списка — владелец присылает её боту."""
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    lists = data.setdefault("microsoft", {}).setdefault("lists", {})
    entry = lists.setdefault(role_name, {})
    entry.setdefault("list_id", "")
    entry["invite_link"] = link

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
