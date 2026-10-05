import yaml

from crmlumi.config import load_settings


def test_load_settings_defaults_when_file_missing(tmp_path):
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.google.sheet_id == ""
    assert settings.google.worksheet_name == "Заказы"
    assert settings.poll.interval_s == 30.0
    assert settings.is_configured is False


def test_load_settings_reads_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    creds = tmp_path / "creds.json"
    creds.write_text("{}", encoding="utf-8")
    path.write_text(
        yaml.safe_dump(
            {
                "google": {
                    "sheet_id": "abc123",
                    "worksheet_name": "Заказы",
                    "service_account_file": str(creds),
                }
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.google.sheet_id == "abc123"
    assert settings.is_configured is True


def test_is_configured_false_without_credentials_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"google": {"sheet_id": "abc123"}}), encoding="utf-8")
    settings = load_settings(path)
    assert settings.is_configured is False


def test_load_settings_reads_telegram_secrets_from_env(tmp_path):
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_OWNER_CHAT_ID=999\n", encoding="utf-8"
    )
    settings = load_settings(config_path, env_path)
    assert settings.telegram_bot_token == "123:abc"
    assert settings.telegram_owner_chat_id == "999"
    assert settings.is_telegram_configured is True


def test_is_telegram_configured_false_when_env_missing(tmp_path):
    settings = load_settings(tmp_path / "config.yaml", tmp_path / ".env")
    assert settings.is_telegram_configured is False


def test_load_settings_defaults_microsoft_section(tmp_path):
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.microsoft.client_id == ""
    assert settings.microsoft.lists == {}
    assert settings.is_todo_configured is False


def test_load_settings_reads_microsoft_lists(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "microsoft": {
                    "client_id": "client-1",
                    "lists": {
                        "Приём заявок": {"list_id": "list-1", "invite_link": "https://x"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(config_path)
    assert settings.microsoft.client_id == "client-1"
    assert settings.microsoft.lists["Приём заявок"].list_id == "list-1"


def test_is_todo_configured_requires_client_id_and_refresh_token(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"microsoft": {"client_id": "client-1"}}), encoding="utf-8"
    )
    env_path = tmp_path / ".env"
    env_path.write_text("MS_OWNER_REFRESH_TOKEN=ref-0\n", encoding="utf-8")
    settings = load_settings(config_path, env_path)
    assert settings.is_todo_configured is True


def test_is_todo_configured_false_without_refresh_token(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"microsoft": {"client_id": "client-1"}}), encoding="utf-8"
    )
    settings = load_settings(config_path, tmp_path / ".env")
    assert settings.is_todo_configured is False


def test_load_settings_defaults_daily_check_section(tmp_path):
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.daily_check.hour_utc == 8
    assert settings.daily_check.near_photographer_days == 3
    assert settings.daily_check.stalled_days == 5


def test_load_settings_defaults_reminders_and_deadlines(tmp_path):
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.reminders.evening_hour == 20
    assert settings.reminders.pre_shoot_hours == 2
    assert settings.deadlines["Лид"] == 1


def test_partial_deadlines_are_merged_with_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"deadlines": {"Лид": 2}}), encoding="utf-8")
    settings = load_settings(config_path)
    assert settings.deadlines["Лид"] == 2
    assert settings.deadlines["Печать"] == 7


def test_load_settings_reads_daily_check_overrides(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"daily_check": {"hour_utc": 10, "stalled_days": 7}}),
        encoding="utf-8",
    )
    settings = load_settings(config_path)
    assert settings.daily_check.hour_utc == 10
    assert settings.daily_check.stalled_days == 7
