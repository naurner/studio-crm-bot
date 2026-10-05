"""Device Code Flow и обновление access-токена для Microsoft Graph.

Единственное место в проекте, работающее со входом через msal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import msal

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Tasks.ReadWrite", "User.Read"]


@dataclass(frozen=True)
class DeviceCodeInfo:
    user_code: str
    verification_uri: str
    message: str


class DeviceCodeFlow:
    """Один логин: start() печатает код, wait_for_result() блокирует до входа."""

    def __init__(
        self, client_id: str, *, app: "msal.PublicClientApplication | None" = None
    ) -> None:
        self._app = app or msal.PublicClientApplication(client_id, authority=AUTHORITY)
        self._flow: dict | None = None

    def start(self) -> DeviceCodeInfo:
        self._flow = self._app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in self._flow:
            raise RuntimeError(
                "Не удалось начать вход: "
                f"{self._flow.get('error_description', 'неизвестная ошибка')}"
            )
        return DeviceCodeInfo(
            user_code=self._flow["user_code"],
            verification_uri=self._flow["verification_uri"],
            message=self._flow["message"],
        )

    def wait_for_result(self) -> dict:
        if self._flow is None:
            raise RuntimeError("start() ещё не вызван")
        result = self._app.acquire_token_by_device_flow(self._flow)
        if "access_token" not in result:
            raise RuntimeError(
                f"Вход не удался: {result.get('error_description', 'неизвестная ошибка')}"
            )
        return result


def refresh_access_token(client_id: str, refresh_token: str) -> dict:
    """Обменивает refresh-токен на новый access-токен (и, возможно, новый refresh)."""
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY)
    result = app.acquire_token_by_refresh_token(refresh_token, scopes=SCOPES)
    if "access_token" not in result:
        raise RuntimeError(
            f"Не удалось обновить токен: {result.get('error_description', 'неизвестная ошибка')}"
        )
    return result


@dataclass
class TokenManager:
    """Держит access-токен и обновляет его сам, когда истекает.

    Refresh-токены Microsoft иногда меняются при обновлении (ротация) —
    новый сохраняется через on_refresh_token_changed, иначе через время
    старый перестанет работать и обновление начнёт падать.
    """

    client_id: str
    refresh_token: str
    on_refresh_token_changed: "Callable[[str], None] | None" = None
    _access_token: str = field(default="", init=False)
    _expires_at: float = field(default=0.0, init=False)

    def get_access_token(self) -> str:
        if time.monotonic() >= self._expires_at:
            self._refresh()
        return self._access_token

    def _refresh(self) -> None:
        result = refresh_access_token(self.client_id, self.refresh_token)
        self._access_token = result["access_token"]
        # 60 секунд запаса, чтобы не словить 401 из-за рассинхрона часов.
        self._expires_at = time.monotonic() + result.get("expires_in", 3600) - 60
        new_refresh = result.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            if self.on_refresh_token_changed is not None:
                self.on_refresh_token_changed(new_refresh)
