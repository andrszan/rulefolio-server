from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.identity import service as identity_service
from app.identity.models import Account, SessionRecord
from app.main import app
from app.notifications import service


def _authorize(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    monkeypatch.setattr(
        identity_service,
        "authenticate",
        lambda *_: identity_service.AuthenticatedSession(
            record=SessionRecord(
                account_id=account.id,
                token_hash=b"0" * 32,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
            account=account,
        ),
    )


def _todo(account_id: object) -> service.NotificationTodoData:
    now = datetime.now(UTC)
    return service.NotificationTodoData(
        id=uuid4(),
        kind="issue_opened",
        summary="有新的问题需要处理",
        context_label="问题：通知 API 测试",
        workspace_id=uuid4(),
        work_id=uuid4(),
        target_kind="issue",
        target_id=uuid4(),
        status="open",
        created_at=now,
        resolved_at=None,
        mail_status="failed",
        last_attempt_at=now,
        can_retry_mail=True,
    )


def test_notification_list_uses_safe_camel_case_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="notifications@example.com", status="active")
    todo = _todo(account.id)
    _authorize(monkeypatch, account)

    def list_todos(*args: object) -> tuple[list[service.NotificationTodoData], int]:
        assert args[1:] == (account.id, "completed", 2, 10)
        return [todo], 1

    monkeypatch.setattr(service, "list_todos", list_todos)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/notifications/todos",
            params={"status": "completed", "page": 2, "size": 10},
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    assert item == {
        "id": str(todo.id),
        "kind": "issue_opened",
        "summary": "有新的问题需要处理",
        "contextLabel": "问题：通知 API 测试",
        "workspaceId": str(todo.workspace_id),
        "workId": str(todo.work_id),
        "targetKind": "issue",
        "targetId": str(todo.target_id),
        "status": "open",
        "createdAt": todo.created_at.isoformat().replace("+00:00", "Z"),
        "resolvedAt": None,
        "mailStatus": "failed",
        "lastAttemptAt": todo.last_attempt_at.isoformat().replace("+00:00", "Z"),
        "canRetryMail": True,
    }


def test_notification_retry_requires_key_and_hides_unavailable_todo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="notifications@example.com", status="active")
    _authorize(monkeypatch, account)
    todo_id = uuid4()
    with TestClient(app) as client:
        missing_key = client.post(
            f"/api/v1/notifications/todos/{todo_id}/mail-retry",
            headers={"Authorization": "Bearer session-token"},
        )
    assert missing_key.status_code == 422
    assert missing_key.json()["data"] == {"reason": "validation_failed"}

    monkeypatch.setattr(
        service,
        "retry_failed_mail",
        lambda *_: (_ for _ in ()).throw(service.NotificationTodoUnavailable),
    )
    with TestClient(app) as client:
        unavailable = client.post(
            f"/api/v1/notifications/todos/{todo_id}/mail-retry",
            headers={
                "Authorization": "Bearer session-token",
                "Idempotency-Key": "notification-retry-1",
            },
        )
    assert unavailable.status_code == 404
    assert unavailable.json()["data"] == {"reason": "notification_todo_unavailable"}
