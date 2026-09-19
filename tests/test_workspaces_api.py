from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.identity import service as identity_service
from app.identity.models import Account, SessionRecord
from app.main import app
from app.workspaces import service


def _account() -> Account:
    return Account(id=uuid4(), email="owner@example.com", status="active")


def _authenticate(account: Account) -> identity_service.AuthenticatedSession:
    return identity_service.AuthenticatedSession(
        record=SessionRecord(
            account_id=account.id,
            token_hash=b"0" * 32,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        account=account,
    )


def _authorize(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    monkeypatch.setattr(
        identity_service, "authenticate", lambda *_: _authenticate(account)
    )


def test_create_workspace_returns_owner_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    workspace_id = uuid4()
    received: list[tuple[str, str | None]] = []
    _authorize(monkeypatch, account)

    def create(
        _, actor_id, name: str, description: str | None
    ) -> service.WorkspaceData:
        assert actor_id == account.id
        received.append((name, description))
        return service.WorkspaceData(workspace_id, name, description, True)

    monkeypatch.setattr(service, "create_workspace", create)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/workspaces",
            headers={"Authorization": "Bearer session-token"},
            json={"name": "  我的空间  ", "description": "  说明  "},
        )

    assert response.status_code == 201
    assert received == [("我的空间", "说明")]
    assert response.json()["data"] == {
        "id": str(workspace_id),
        "name": "我的空间",
        "description": "说明",
        "isOwner": True,
        "revision": 1,
        "accessState": "active",
        "readUntil": None,
    }


def test_workspace_exit_passes_password_revision_and_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    workspace_id = uuid4()
    requested: list[tuple[str, int, datetime, str]] = []
    _authorize(monkeypatch, account)

    def request_exit(
        _,
        actor_id,
        received_workspace_id,
        password,
        expected_revision,
        read_until,
        operation_key,
    ) -> service.WorkspaceData:
        assert actor_id == account.id
        assert received_workspace_id == workspace_id
        requested.append((password, expected_revision, read_until, operation_key))
        return service.WorkspaceData(
            workspace_id,
            "我的空间",
            None,
            True,
            revision=2,
            access_state="exiting",
            read_until=read_until,
        )

    monkeypatch.setattr(service, "request_workspace_exit", request_exit)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/exit",
            headers={
                "Authorization": "Bearer session-token",
                "Idempotency-Key": "workspace-exit-1",
            },
            json={
                "password": "current-password",
                "expectedRevision": 1,
                "readUntil": "2030-01-01T00:00:00+08:00",
            },
        )

    assert response.status_code == 200
    assert requested == [
        (
            "current-password",
            1,
            datetime(2030, 1, 1, tzinfo=UTC) - timedelta(hours=8),
            "workspace-exit-1",
        )
    ]
    assert response.json()["data"] == {
        "id": str(workspace_id),
        "name": "我的空间",
        "description": None,
        "isOwner": True,
        "revision": 2,
        "accessState": "exiting",
        "readUntil": "2030-01-01T00:00:00+08:00",
    }


def test_member_directory_hides_management_scope_from_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    _authorize(monkeypatch, account)

    def forbidden(*_: object) -> tuple[list[service.WorkspaceMemberData], int]:
        raise service.WorkspaceManagementForbidden

    monkeypatch.setattr(service, "list_members", forbidden)
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{uuid4()}/members",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 403
    assert response.json()["data"] == {"reason": "workspace_management_forbidden"}


def test_current_session_exchange_keeps_token_out_of_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    token = "received-invitation-token"
    _authorize(monkeypatch, account)

    def mismatch(*_: object) -> service.InvitationExchangeResult:
        raise service.WorkspaceInvitationAccountMismatch

    monkeypatch.setattr(service, "exchange_current_session_invitation", mismatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/workspace-invitation-exchanges/current-session",
            headers={
                "Authorization": "Bearer session-token",
                "Idempotency-Key": "invite-operation-1",
            },
            json={"token": token},
        )

    assert response.status_code == 403
    assert response.json()["data"] == {
        "reason": "workspace_invitation_account_mismatch"
    }
    assert token not in response.text


def test_activation_retry_requires_login_without_replaying_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid4()
    monkeypatch.setattr(
        service,
        "exchange_account_activation_invitation",
        lambda *_: service.InvitationExchangeResult(
            service.WorkspaceData(workspace_id, "协作空间", None, False),
            login_required=True,
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/workspace-invitation-exchanges/account-activation",
            headers={"Idempotency-Key": "invite-operation-2"},
            json={"token": "received-invitation-token", "newPassword": "long-password"},
        )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "workspace": {
            "id": str(workspace_id),
            "name": "协作空间",
            "description": None,
            "isOwner": False,
            "revision": 1,
            "accessState": "active",
            "readUntil": None,
        },
    }


def test_workspace_database_failure_returns_retriable_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    _authorize(monkeypatch, account)

    def retryable(*_: object) -> service.WorkspaceData:
        raise service.WorkspaceOperationRetryable

    monkeypatch.setattr(service, "create_workspace", retryable)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/workspaces",
            headers={"Authorization": "Bearer session-token"},
            json={"name": "协作空间"},
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["data"] == {"reason": "workspace_operation_retryable"}
