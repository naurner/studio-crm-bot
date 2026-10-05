import yaml

from crmlumi.setup_wizard import (
    write_config,
    write_env,
    write_invite_link,
    write_microsoft_config,
)


def test_write_config_creates_valid_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(
        path,
        sheet_id="abc123",
        worksheet_name="Заказы",
        service_account_file="config/service_account.json",
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["google"]["sheet_id"] == "abc123"
    assert data["google"]["worksheet_name"] == "Заказы"
    assert data["poll"]["interval_s"] == 30.0
    assert data["db_path"] == "data/crmlumi.sqlite"


def test_write_config_preserves_existing_unrelated_settings(tmp_path):
    # Повторный запуск мастера не должен сбрасывать вручную выставленный интервал.
    path = tmp_path / "config.yaml"
    path.write_text("poll:\n  interval_s: 60\n", encoding="utf-8")
    write_config(
        path,
        sheet_id="abc123",
        worksheet_name="Заказы",
        service_account_file="config/service_account.json",
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["poll"]["interval_s"] == 60
    assert data["google"]["sheet_id"] == "abc123"


def test_write_env_writes_secrets(tmp_path):
    path = tmp_path / ".env"
    write_env(path, {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_OWNER_CHAT_ID": "42"})
    content = path.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=abc" in content
    assert "TELEGRAM_OWNER_CHAT_ID=42" in content


def test_write_env_updates_existing_key_without_duplicating(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=old\nOTHER=keep\n", encoding="utf-8")
    write_env(path, {"TELEGRAM_BOT_TOKEN": "new"})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert "TELEGRAM_BOT_TOKEN=new" in lines
    assert "OTHER=keep" in lines
    assert len([line for line in lines if line.startswith("TELEGRAM_BOT_TOKEN")]) == 1


def test_write_microsoft_config_creates_lists_section(tmp_path):
    path = tmp_path / "config.yaml"
    write_microsoft_config(
        path,
        client_id="client-1",
        lists={"Приём заявок": {"list_id": "list-1", "invite_link": ""}},
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["microsoft"]["client_id"] == "client-1"
    assert data["microsoft"]["lists"]["Приём заявок"]["list_id"] == "list-1"


def test_write_microsoft_config_preserves_invite_link_on_rerun(tmp_path):
    path = tmp_path / "config.yaml"
    write_microsoft_config(
        path,
        client_id="client-1",
        lists={"Приём заявок": {"list_id": "list-1", "invite_link": "https://x"}},
    )
    write_microsoft_config(
        path,
        client_id="client-1",
        lists={"Приём заявок": {"list_id": "list-1", "invite_link": ""}},
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["microsoft"]["lists"]["Приём заявок"]["invite_link"] == "https://x"


def test_write_invite_link_keeps_list_id_and_other_settings(tmp_path):
    path = tmp_path / "config.yaml"
    write_microsoft_config(
        path,
        client_id="client-1",
        lists={"Приём заявок": {"list_id": "list-1", "invite_link": ""}},
    )
    write_invite_link(path, "Приём заявок", "https://to-do.live.com/sharing?x=1")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = data["microsoft"]["lists"]["Приём заявок"]
    assert entry["list_id"] == "list-1"
    assert entry["invite_link"] == "https://to-do.live.com/sharing?x=1"
    assert data["microsoft"]["client_id"] == "client-1"


def test_write_invite_link_creates_missing_sections(tmp_path):
    path = tmp_path / "config.yaml"
    write_invite_link(path, "Фотограф", "https://y")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["microsoft"]["lists"]["Фотограф"]["invite_link"] == "https://y"
