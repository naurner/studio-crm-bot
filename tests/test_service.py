import plistlib
from pathlib import Path

from crmlumi.service import (
    LAUNCHD_LABEL,
    SUPERVISED_ENV,
    build_launchd_plist,
    err_log_path,
    plist_path,
)


def test_build_launchd_plist_runs_crmlumi_from_project_dir():
    data = plistlib.loads(
        build_launchd_plist(
            python_executable="/Users/me/crm-lumi/.venv/bin/python",
            project_dir=Path("/Users/me/crm-lumi"),
        )
    )
    assert data["Label"] == LAUNCHD_LABEL
    assert data["ProgramArguments"] == [
        "/Users/me/crm-lumi/.venv/bin/python",
        "-m",
        "crmlumi",
        "run",
    ]
    assert data["WorkingDirectory"] == str(Path("/Users/me/crm-lumi"))
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True


def test_build_launchd_plist_marks_process_as_supervised_and_sets_path():
    data = plistlib.loads(
        build_launchd_plist(python_executable="/x/python", project_dir=Path("/x"))
    )
    env = data["EnvironmentVariables"]
    assert env[SUPERVISED_ENV] == "1"
    assert "/opt/homebrew/bin" in env["PATH"]


def test_build_launchd_plist_writes_logs_into_project_logs_dir():
    data = plistlib.loads(
        build_launchd_plist(python_executable="/x/python", project_dir=Path("/x"))
    )
    assert data["StandardErrorPath"] == str(err_log_path(Path("/x")))
    assert data["StandardOutPath"].startswith(str(Path("/x") / "logs"))


def test_plist_path_is_in_user_launch_agents():
    assert plist_path(Path("/Users/me")) == Path(
        "/Users/me/Library/LaunchAgents/com.crmlumi.run.plist"
    )
