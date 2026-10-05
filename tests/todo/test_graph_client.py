import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from crmlumi.todo.graph_client import GraphTodoClient


def make_client(handler) -> GraphTodoClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return GraphTodoClient(lambda: "test-token", http_client=http_client)


def test_get_me_returns_json():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.url.path == "/v1.0/me"
        return httpx.Response(200, json={"id": "u1", "mail": "a@b.com"})

    client = make_client(handler)
    assert client.get_me() == {"id": "u1", "mail": "a@b.com"}


def test_create_task_returns_id():
    def handler(request):
        assert request.url.path == "/v1.0/me/todo/lists/list-1/tasks"
        return httpx.Response(201, json={"id": "task-1"})

    client = make_client(handler)
    assert client.create_task("list-1", "Перезвонить") == "task-1"


def test_create_task_sends_due_date_when_given():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "task-1"})

    client = make_client(handler)
    client.create_task("list-1", "Съёмка", due_date="2026-10-01T00:00:00")
    assert captured["body"]["dueDateTime"]["dateTime"] == "2026-10-01T00:00:00"


def test_create_task_omits_due_date_when_not_given():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "task-1"})

    client = make_client(handler)
    client.create_task("list-1", "Перезвонить")
    assert "dueDateTime" not in captured["body"]


def test_create_list_returns_id():
    def handler(request):
        assert request.url.path == "/v1.0/me/todo/lists"
        return httpx.Response(201, json={"id": "list-9", "displayName": "Новые заявки"})

    client = make_client(handler)
    assert client.create_list("Новые заявки") == "list-9"


def test_find_list_by_name_returns_matching_id():
    def handler(request):
        return httpx.Response(
            200, json={"value": [{"id": "list-1", "displayName": "Новые заявки"}]}
        )

    client = make_client(handler)
    assert client.find_list_by_name("Новые заявки") == "list-1"


def test_find_list_by_name_returns_none_when_missing():
    def handler(request):
        return httpx.Response(200, json={"value": []})

    client = make_client(handler)
    assert client.find_list_by_name("Нет такого") is None


def test_create_task_raises_on_http_error():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.create_task("list-1", "x")


def test_create_task_accepts_plain_day_and_reminder_moment():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "task-1"})

    tz = timezone(timedelta(hours=5))
    client = make_client(handler)
    client.create_task(
        "list-1", "Съёмка", due_date="2026-10-12", remind_at=datetime(2026, 10, 12, 8, 0, tzinfo=tz)
    )
    body = captured["body"]
    assert body["dueDateTime"] == {"dateTime": "2026-10-12T00:00:00", "timeZone": "UTC"}
    assert body["reminderDateTime"] == {"dateTime": "2026-10-12T03:00:00", "timeZone": "UTC"}
    assert body["isReminderOn"] is True


def test_update_task_clears_schedule_when_missing():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = make_client(handler)
    client.update_task("list-1", "t1", title="Съёмка", due_date=None, remind_at=None)
    assert captured["method"] == "PATCH"
    assert captured["body"] == {"title": "Съёмка", "dueDateTime": None, "isReminderOn": False}


def test_delete_task_ignores_already_deleted():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(404)

    make_client(handler).delete_task("list-1", "gone")
    assert calls == ["DELETE"]


def test_complete_task_patches_status():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "t1", "status": "completed"})

    client = make_client(handler)
    client.complete_task("list-1", "t1")
    assert captured == {
        "method": "PATCH",
        "path": "/v1.0/me/todo/lists/list-1/tasks/t1",
        "body": {"status": "completed"},
    }


def test_complete_task_ignores_deleted_task():
    client = make_client(lambda request: httpx.Response(404, json={"error": "not found"}))
    client.complete_task("list-1", "gone")


def test_complete_task_raises_on_other_errors():
    client = make_client(lambda request: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(httpx.HTTPStatusError):
        client.complete_task("list-1", "t1")


def test_list_tasks_returns_task_list():
    def handler(request):
        assert request.url.path == "/v1.0/me/todo/lists/list-1/tasks"
        return httpx.Response(200, json={"value": [{"id": "t1", "title": "x"}]})

    client = make_client(handler)
    assert client.list_tasks("list-1") == [{"id": "t1", "title": "x"}]


def test_list_tasks_returns_empty_list_when_no_tasks():
    def handler(request):
        return httpx.Response(200, json={"value": []})

    client = make_client(handler)
    assert client.list_tasks("list-1") == []


def test_create_task_puts_note_into_body():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "task-1"})

    make_client(handler).create_task("list-1", "Обработать фото", note="📁 Исходники: https://disk/raw")
    assert captured["body"]["body"] == {"content": "📁 Исходники: https://disk/raw", "contentType": "text"}
