"""HTTP-обёртка над Microsoft Graph API для задач To Do.

Не знает про OAuth: принимает функцию, возвращающую действующий
access-токен. Обновление токена — в todo/oauth.py (TokenManager).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import httpx

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _schedule_fields(due_date: str | None, remind_at: datetime | None, *, clear: bool) -> dict:
    """Срок и напоминание в формате Graph.

    Срок в To Do — это день: передаём полночь без сдвига поясов, иначе в
    восточных поясах задача «уехала» бы на предыдущий день. Напоминание — точный
    момент, его переводим в UTC.
    """
    fields: dict = {}
    if due_date:
        value = due_date if "T" in due_date else f"{due_date}T00:00:00"
        fields["dueDateTime"] = {"dateTime": value, "timeZone": "UTC"}
    elif clear:
        fields["dueDateTime"] = None
    if remind_at is not None:
        moment = remind_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        fields["reminderDateTime"] = {"dateTime": moment, "timeZone": "UTC"}
        fields["isReminderOn"] = True
    elif clear:
        fields["isReminderOn"] = False
    return fields


class GraphTodoClient:
    def __init__(
        self, token_provider: Callable[[], str], http_client: httpx.Client | None = None
    ) -> None:
        self._token_provider = token_provider
        self._http = http_client or httpx.Client(timeout=15.0)

    def get_me(self) -> dict:
        response = self._http.get(f"{GRAPH_BASE}/me", headers=self._headers())
        response.raise_for_status()
        return response.json()

    def create_task(
        self,
        list_id: str,
        title: str,
        *,
        due_date: str | None = None,
        remind_at: datetime | None = None,
        note: str | None = None,
    ) -> str:
        """due_date — день (`2026-10-12`), remind_at — точный момент уведомления To Do,
        note — заметка задачи (например, ссылки на диск)."""
        body: dict = {"title": title, **_schedule_fields(due_date, remind_at, clear=False)}
        if note:
            body["body"] = {"content": note, "contentType": "text"}
        response = self._http.post(
            f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks",
            headers=self._headers(),
            json=body,
        )
        response.raise_for_status()
        return response.json()["id"]

    def update_task(
        self,
        list_id: str,
        task_id: str,
        *,
        title: str,
        due_date: str | None,
        remind_at: datetime | None,
    ) -> None:
        body = {"title": title, **_schedule_fields(due_date, remind_at, clear=True)}
        response = self._http.patch(
            f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks/{task_id}",
            headers=self._headers(),
            json=body,
        )
        response.raise_for_status()

    def delete_task(self, list_id: str, task_id: str) -> None:
        response = self._http.delete(
            f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks/{task_id}", headers=self._headers()
        )
        if response.status_code == 404:
            return
        response.raise_for_status()

    def complete_task(self, list_id: str, task_id: str) -> None:
        """Отмечает задачу выполненной. Задачу уже удалили в To Do — не ошибка."""
        response = self._http.patch(
            f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks/{task_id}",
            headers=self._headers(),
            json={"status": "completed"},
        )
        if response.status_code == 404:
            return
        response.raise_for_status()

    def create_list(self, display_name: str) -> str:
        response = self._http.post(
            f"{GRAPH_BASE}/me/todo/lists",
            headers=self._headers(),
            json={"displayName": display_name},
        )
        response.raise_for_status()
        return response.json()["id"]

    def find_list_by_name(self, display_name: str) -> str | None:
        response = self._http.get(f"{GRAPH_BASE}/me/todo/lists", headers=self._headers())
        response.raise_for_status()
        for item in response.json().get("value", []):
            if item.get("displayName") == display_name:
                return item["id"]
        return None

    def list_tasks(self, list_id: str) -> list[dict]:
        response = self._http.get(
            f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks", headers=self._headers()
        )
        response.raise_for_status()
        return response.json().get("value", [])

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider()}"}
