import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crmlumi.host import (
    RuntimeState,
    current_commit,
    format_duration,
    format_status,
    is_supervised,
    runtime_kind,
    tail_text,
    update_from_git,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class FakeRunner:
    """Отвечает на команды по порядку вызовов; запоминает, что запускали."""

    def __init__(
        self,
        commits: list[str],
        pull_code: int = 0,
        install_code: int = 0,
        changed_files: str = "src/crmlumi/cli.py",
    ):
        self.commits = list(commits)
        self.pull_code = pull_code
        self.install_code = install_code
        self.changed_files = changed_files
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd):
        self.calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, stdout=self.commits.pop(0) + "\n", stderr="")
        if args[:2] == ["git", "pull"]:
            return subprocess.CompletedProcess(args, self.pull_code, stdout="pull output", stderr="")
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, 0, stdout=self.changed_files, stderr="")
        return subprocess.CompletedProcess(args, self.install_code, stdout="", stderr="pip error")


def make_runtime(**overrides) -> RuntimeState:
    base = dict(
        project_dir=Path("/x"),
        supervised=True,
        kind="launchd",
        started_at=NOW - timedelta(hours=3),
    )
    base.update(overrides)
    return RuntimeState(**base)


def test_is_supervised_only_when_env_flag_set():
    assert is_supervised({"CRMLUMI_SUPERVISED": "1"}) is True
    assert is_supervised({}) is False


def test_current_commit_returns_short_hash():
    assert current_commit(Path("/x"), FakeRunner(["abc1234"])) == "abc1234"


def test_update_from_git_skips_install_when_nothing_new():
    runner = FakeRunner(["abc1234", "abc1234"])
    result = update_from_git(Path("/x"), "/x/python", runner)
    assert result.ok is True
    assert result.changed is False
    assert not any("pip" in call for call in runner.calls)


def test_update_from_git_installs_when_new_commits_arrive():
    runner = FakeRunner(["abc1234", "def5678"])
    result = update_from_git(Path("/x"), "/x/python", runner)
    assert result.ok is True
    assert result.changed is True
    assert result.commit == "def5678"
    assert ["/x/python", "-m", "pip", "install", "."] in runner.calls


def test_update_in_docker_skips_install_and_flags_changed_dependencies():
    # В контейнере зависимости живут в образе: ставить их на лету бессмысленно.
    runner = FakeRunner(["abc1234", "def5678"], changed_files="pyproject.toml\nsrc/crmlumi/cli.py")
    result = update_from_git(Path("/x"), "/x/python", runner, install=False)
    assert result.ok is True and result.changed is True
    assert result.deps_changed is True
    assert not any("pip" in call for call in runner.calls)


def test_update_in_docker_without_dependency_changes():
    runner = FakeRunner(["abc1234", "def5678"], changed_files="README.md")
    result = update_from_git(Path("/x"), "/x/python", runner, install=False)
    assert result.deps_changed is False


def test_runtime_kind_tells_docker_launchd_and_manual():
    assert runtime_kind({"CRMLUMI_RUNTIME": "docker", "CRMLUMI_SUPERVISED": "1"}) == "docker"
    assert runtime_kind({"CRMLUMI_SUPERVISED": "1"}) == "launchd"
    assert runtime_kind({}) == "manual"


def test_status_names_the_runtime():
    def autostart_line(**overrides):
        text = format_status(
            make_runtime(**overrides),
            now=NOW,
            commit="abc1234",
            users_count=1,
            todo_configured=False,
            invite_links_count=0,
            lists_count=0,
        )
        return next(line for line in text.splitlines() if line.startswith("Автозапуск"))

    assert "Docker" in autostart_line(kind="docker")
    assert "launchd" in autostart_line(kind="launchd")
    assert "вручную" in autostart_line(kind="manual", supervised=False)


def test_update_from_git_reports_failed_pull():
    runner = FakeRunner(["abc1234"], pull_code=1)
    result = update_from_git(Path("/x"), "/x/python", runner)
    assert result.ok is False
    assert "pull output" in result.output


def test_update_from_git_reports_failed_install():
    runner = FakeRunner(["abc1234", "def5678"], install_code=1)
    result = update_from_git(Path("/x"), "/x/python", runner)
    assert result.ok is False
    assert "pip error" in result.output


def test_tail_text_returns_none_for_missing_file(tmp_path):
    assert tail_text(tmp_path / "nope.log") is None


def test_tail_text_keeps_only_last_lines(tmp_path):
    path = tmp_path / "err.log"
    path.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
    text = tail_text(path, max_lines=3)
    assert text == "line 97\nline 98\nline 99"


def test_tail_text_fits_telegram_message_limit(tmp_path):
    path = tmp_path / "err.log"
    path.write_text("x" * 10000, encoding="utf-8")
    assert len(tail_text(path)) <= 3500


def test_format_duration_picks_largest_units():
    assert format_duration(59) == "0 мин"
    assert format_duration(3 * 3600 + 5 * 60) == "3 ч 5 мин"
    assert format_duration(2 * 86400 + 4 * 3600) == "2 д 4 ч"


def test_format_status_shows_uptime_version_and_autostart():
    text = format_status(
        make_runtime(last_poll_at=NOW - timedelta(minutes=1)),
        now=NOW,
        commit="abc1234",
        users_count=3,
        todo_configured=True,
        invite_links_count=6,
        lists_count=6,
    )
    assert "3 ч 0 мин" in text
    assert "abc1234" in text
    assert "Автозапуск: включён" in text
    assert "6 из 6" in text


def test_format_status_highlights_todo_error():
    text = format_status(
        make_runtime(last_todo_error="401 Unauthorized"),
        now=NOW,
        commit="abc1234",
        users_count=1,
        todo_configured=True,
        invite_links_count=6,
        lists_count=6,
    )
    assert "401 Unauthorized" in text


def test_format_status_highlights_poll_error():
    text = format_status(
        make_runtime(supervised=False, kind="manual", last_poll_error="нет сети"),
        now=NOW,
        commit=None,
        users_count=1,
        todo_configured=False,
        invite_links_count=0,
        lists_count=0,
    )
    assert "нет сети" in text
    assert "вручную" in text
    assert "To Do: не подключён" in text
