import pytest

from crmlumi.todo.oauth import DeviceCodeFlow, TokenManager, refresh_access_token


class FakeMsalApp:
    def __init__(self, flow_response=None, token_response=None):
        self._flow_response = flow_response or {
            "user_code": "ABC123",
            "verification_uri": "https://microsoft.com/devicelogin",
            "message": "Go to https://microsoft.com/devicelogin and enter ABC123",
        }
        self._token_response = token_response or {
            "access_token": "tok",
            "refresh_token": "ref",
        }

    def initiate_device_flow(self, scopes):
        return self._flow_response

    def acquire_token_by_device_flow(self, flow):
        return self._token_response

    def acquire_token_by_refresh_token(self, refresh_token, scopes):
        return self._token_response


def test_start_returns_device_code_info():
    flow = DeviceCodeFlow("client-id", app=FakeMsalApp())
    info = flow.start()
    assert info.user_code == "ABC123"
    assert info.verification_uri == "https://microsoft.com/devicelogin"


def test_start_raises_when_flow_has_no_user_code():
    fake = FakeMsalApp(flow_response={"error_description": "bad client id"})
    flow = DeviceCodeFlow("client-id", app=fake)
    with pytest.raises(RuntimeError):
        flow.start()


def test_wait_for_result_returns_tokens():
    flow = DeviceCodeFlow("client-id", app=FakeMsalApp())
    flow.start()
    result = flow.wait_for_result()
    assert result["access_token"] == "tok"


def test_wait_for_result_raises_without_start():
    flow = DeviceCodeFlow("client-id", app=FakeMsalApp())
    with pytest.raises(RuntimeError):
        flow.wait_for_result()


def test_wait_for_result_raises_when_denied():
    fake = FakeMsalApp(token_response={"error_description": "user declined"})
    flow = DeviceCodeFlow("client-id", app=fake)
    flow.start()
    with pytest.raises(RuntimeError):
        flow.wait_for_result()


def test_refresh_access_token_returns_new_tokens(monkeypatch):
    fake = FakeMsalApp(token_response={"access_token": "new-tok", "refresh_token": "new-ref"})
    monkeypatch.setattr(
        "crmlumi.todo.oauth.msal.PublicClientApplication",
        lambda client_id, authority: fake,
    )
    result = refresh_access_token("client-id", "old-ref")
    assert result["access_token"] == "new-tok"


def test_refresh_access_token_raises_on_failure(monkeypatch):
    fake = FakeMsalApp(token_response={"error_description": "invalid_grant"})
    monkeypatch.setattr(
        "crmlumi.todo.oauth.msal.PublicClientApplication",
        lambda client_id, authority: fake,
    )
    with pytest.raises(RuntimeError):
        refresh_access_token("client-id", "old-ref")


def test_token_manager_refreshes_on_first_use(monkeypatch):
    calls = []

    def fake_refresh(client_id, refresh_token):
        calls.append((client_id, refresh_token))
        return {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600}

    monkeypatch.setattr("crmlumi.todo.oauth.refresh_access_token", fake_refresh)
    manager = TokenManager(client_id="client-1", refresh_token="ref-0")
    assert manager.get_access_token() == "tok-1"
    assert calls == [("client-1", "ref-0")]


def test_token_manager_reuses_token_before_expiry(monkeypatch):
    calls = []

    def fake_refresh(client_id, refresh_token):
        calls.append(refresh_token)
        return {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600}

    monkeypatch.setattr("crmlumi.todo.oauth.refresh_access_token", fake_refresh)
    manager = TokenManager(client_id="client-1", refresh_token="ref-0")
    manager.get_access_token()
    manager.get_access_token()
    assert len(calls) == 1


def test_token_manager_notifies_on_new_refresh_token(monkeypatch):
    def fake_refresh(client_id, refresh_token):
        return {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600}

    monkeypatch.setattr("crmlumi.todo.oauth.refresh_access_token", fake_refresh)
    notified = []
    manager = TokenManager(
        client_id="client-1", refresh_token="ref-0", on_refresh_token_changed=notified.append
    )
    manager.get_access_token()
    assert notified == ["ref-1"]


def test_token_manager_refreshes_again_after_expiry(monkeypatch):
    calls = []

    def fake_refresh(client_id, refresh_token):
        calls.append(refresh_token)
        return {
            "access_token": f"tok-{len(calls)}",
            "refresh_token": refresh_token,
            "expires_in": -100,
        }

    monkeypatch.setattr("crmlumi.todo.oauth.refresh_access_token", fake_refresh)
    manager = TokenManager(client_id="client-1", refresh_token="ref-0")
    manager.get_access_token()
    manager.get_access_token()
    assert len(calls) == 2
