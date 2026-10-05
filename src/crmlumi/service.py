"""Автозапуск на Mac через launchd — одной командой вместо ручного plist."""

from __future__ import annotations

import plistlib
from pathlib import Path

LAUNCHD_LABEL = "com.crmlumi.run"
SUPERVISED_ENV = "CRMLUMI_SUPERVISED"
# Чем поднят процесс: «docker» ставит docker-compose.yml, launchd — plist ниже.
RUNTIME_ENV = "CRMLUMI_RUNTIME"
LOG_DIR_NAME = "logs"
OUT_LOG_NAME = "crmlumi.out.log"
ERR_LOG_NAME = "crmlumi.err.log"
# У launchd почти пустой PATH — без Homebrew-путей не найдётся git для обновления из бота.
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def err_log_path(project_dir: Path) -> Path:
    return project_dir / LOG_DIR_NAME / ERR_LOG_NAME


def build_launchd_plist(*, python_executable: str, project_dir: Path) -> bytes:
    log_dir = project_dir / LOG_DIR_NAME
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [python_executable, "-m", "crmlumi", "run"],
            "WorkingDirectory": str(project_dir),
            "RunAtLoad": True,
            "KeepAlive": True,
            "EnvironmentVariables": {
                "PATH": LAUNCHD_PATH,
                "PYTHONUNBUFFERED": "1",
                SUPERVISED_ENV: "1",
            },
            "StandardOutPath": str(log_dir / OUT_LOG_NAME),
            "StandardErrorPath": str(log_dir / ERR_LOG_NAME),
        }
    )
