import yaml
from typer.testing import CliRunner

from crmlumi.cli import app

runner = CliRunner()


def test_run_exits_with_error_when_not_configured(tmp_path):
    missing_config = tmp_path / "config.yaml"
    result = runner.invoke(app, ["run", "--config-path", str(missing_config)])
    assert result.exit_code == 1
    assert "Не настроено" in result.stdout


def test_run_exits_with_error_when_telegram_not_configured(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"google": {"sheet_id": "abc", "service_account_file": str(creds)}}),
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    result = runner.invoke(
        app,
        ["run", "--config-path", str(config_path), "--env-path", str(env_path)],
    )
    assert result.exit_code == 1
    assert "Не настроен Telegram" in result.stdout
