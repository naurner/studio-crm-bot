"""Меню хоста в боте: статус процесса, обновление с GitHub, хвост лога.

Всё, ради чего раньше приходилось идти в терминал на Mac.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .service import RUNTIME_ENV, SUPERVISED_ENV

Runner = Callable[[list[str], Path], subprocess.CompletedProcess]


@dataclass
class RuntimeState:
    """Живое состояние процесса, общее для опроса таблицы и хендлеров бота."""

    project_dir: Path
    supervised: bool
    # «docker», «launchd» или «manual» — от этого зависит, как обновляться.
    kind: str = "manual"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_poll_at: datetime | None = None
    last_poll_error: str | None = None
    last_todo_error: str | None = None
    restart_requested: asyncio.Event = field(default_factory=asyncio.Event)
    # Бот что-то записал в таблицу — опросить её сразу, не дожидаясь интервала,
    # чтобы уведомления и задачи следующего этапа ушли без задержки.
    poll_now: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    changed: bool
    commit: str | None
    output: str
    # Изменился pyproject.toml: в Docker нужен пересбор образа.
    deps_changed: bool = False


RUNTIME_LABELS = {
    "docker": "Автозапуск: Docker — контейнер поднимается сам",
    "launchd": "Автозапуск: включён (launchd)",
    "manual": "Автозапуск: нет, бот запущен вручную из терминала",
}


def is_supervised(environ: Mapping[str, str] = os.environ) -> bool:
    """Поднимет ли процесс кто-то снаружи — только тогда выход = перезапуск."""
    return environ.get(SUPERVISED_ENV) == "1"


def runtime_kind(environ: Mapping[str, str] = os.environ) -> str:
    """Чем запущен бот: docker, launchd или вручную из терминала."""
    if environ.get(RUNTIME_ENV) == "docker":
        return "docker"
    return "launchd" if is_supervised(environ) else "manual"


def run_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=600, check=False
    )


def current_commit(project_dir: Path, run: Runner = run_command) -> str | None:
    try:
        result = run(["git", "rev-parse", "--short", "HEAD"], project_dir)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _output_of(result: subprocess.CompletedProcess) -> str:
    return "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())


def _deps_changed(project_dir: Path, before: str | None, after: str | None, run: Runner) -> bool:
    if not before or not after:
        return False
    try:
        diff = run(["git", "diff", "--name-only", f"{before}..{after}"], project_dir)
    except (OSError, subprocess.SubprocessError):
        return False
    return "pyproject.toml" in diff.stdout.split()


def update_from_git(
    project_dir: Path,
    python_executable: str,
    run: Runner = run_command,
    *,
    install: bool = True,
) -> UpdateResult:
    """Забирает код с GitHub и (если нужно) переустанавливает пакет.

    В Docker код берётся из смонтированной папки, а зависимости живут в
    образе: ставить их в работающий контейнер бессмысленно (пропадут при
    перезапуске), поэтому install=False, а про изменившийся pyproject.toml
    говорим отдельно — там нужен пересбор.
    """
    before = current_commit(project_dir, run)
    try:
        pull = run(["git", "pull", "--ff-only"], project_dir)
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateResult(ok=False, changed=False, commit=before, output=str(exc))
    if pull.returncode != 0:
        return UpdateResult(ok=False, changed=False, commit=before, output=_output_of(pull))

    after = current_commit(project_dir, run)
    if after == before:
        return UpdateResult(ok=True, changed=False, commit=after, output=_output_of(pull))
    deps_changed = _deps_changed(project_dir, before, after, run)

    if not install:
        return UpdateResult(
            ok=True, changed=True, commit=after, output=_output_of(pull), deps_changed=deps_changed
        )
    try:
        installed = run([python_executable, "-m", "pip", "install", "."], project_dir)
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateResult(ok=False, changed=True, commit=after, output=str(exc))
    if installed.returncode != 0:
        return UpdateResult(ok=False, changed=True, commit=after, output=_output_of(installed))
    return UpdateResult(
        ok=True, changed=True, commit=after, output=_output_of(pull), deps_changed=deps_changed
    )


def tail_text(path: Path, *, max_lines: int = 40, max_chars: int = 3500) -> str | None:
    """Хвост файла, влезающий в одно сообщение Telegram (лимит 4096 символов)."""
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(lines[-max_lines:])
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def format_status(
    runtime: RuntimeState,
    *,
    now: datetime,
    commit: str | None,
    users_count: int,
    todo_configured: bool,
    invite_links_count: int,
    lists_count: int,
) -> str:
    lines = [
        "🖥 Статус CRM Lumi",
        f"Работает: {format_duration((now - runtime.started_at).total_seconds())}",
        f"Версия: {commit or 'неизвестна'}",
        RUNTIME_LABELS.get(runtime.kind, RUNTIME_LABELS["manual"]),
    ]
    if runtime.last_poll_error:
        lines.append(f"⚠️ Таблица: ошибка — {runtime.last_poll_error}")
    elif runtime.last_poll_at is not None:
        ago = format_duration((now - runtime.last_poll_at).total_seconds())
        lines.append(f"Таблица: ок, последний опрос {ago} назад")
    else:
        lines.append("Таблица: ещё не опрашивалась")
    lines.append(f"Участников в боте: {users_count}")
    if todo_configured:
        lines.append(f"To Do: подключён, ссылок-приглашений {invite_links_count} из {lists_count}")
        if runtime.last_todo_error:
            lines.append(f"⚠️ To Do: не получается создать задачи — {runtime.last_todo_error}")
    else:
        lines.append("To Do: не подключён")
    return "\n".join(lines)
