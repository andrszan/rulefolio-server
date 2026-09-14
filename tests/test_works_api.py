from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.identity import service as identity_service
from app.identity.models import Account, SessionRecord
from app.main import app
from app.works import service


def _account() -> Account:
    return Account(id=uuid4(), email="maintainer@example.com", status="active")


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


def _work(workspace_id: UUID, role: str = "maintainer") -> service.WorkData:
    return service.WorkData(
        id=uuid4(),
        workspace_id=workspace_id,
        name="测试作品",
        description="作品简介",
        creative_stage="原型",
        target_experience="共同探索",
        min_players=2,
        max_players=4,
        estimated_duration_minutes=60,
        revision=1,
        own_role=role,
        can_manage_access=role == "maintainer",
    )


def test_create_work_returns_private_work_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    workspace_id = uuid4()
    created = _work(workspace_id)
    _authorize(monkeypatch, account)

    def create(
        _, actor_id: UUID, received_workspace_id: UUID, **values: object
    ) -> service.WorkData:
        assert actor_id == account.id
        assert received_workspace_id == workspace_id
        assert values == {
            "name": "测试作品",
            "description": "作品简介",
            "creative_stage": "原型",
            "target_experience": "共同探索",
            "min_players": 2,
            "max_players": 4,
            "estimated_duration_minutes": 60,
        }
        return created

    monkeypatch.setattr(service, "create_work", create)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/works",
            headers={"Authorization": "Bearer session-token"},
            json={
                "name": "  测试作品  ",
                "description": "  作品简介  ",
                "creativeStage": "  原型  ",
                "targetExperience": "  共同探索  ",
                "minPlayers": 2,
                "maxPlayers": 4,
                "estimatedDurationMinutes": 60,
            },
        )

    assert response.status_code == 201
    assert response.json()["data"] == {
        "id": str(created.id),
        "workspaceId": str(workspace_id),
        "name": "测试作品",
        "description": "作品简介",
        "creativeStage": "原型",
        "targetExperience": "共同探索",
        "minPlayers": 2,
        "maxPlayers": 4,
        "estimatedDurationMinutes": 60,
        "revision": 1,
        "ownRole": "maintainer",
        "canManageAccess": True,
    }


def test_private_deep_link_and_access_directory_keep_error_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    workspace_id = uuid4()
    work_id = uuid4()
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service, "read_work", lambda *_: (_ for _ in ()).throw(service.WorkUnavailable)
    )
    monkeypatch.setattr(
        service,
        "list_access_members",
        lambda *_: (_ for _ in ()).throw(service.WorkManagementForbidden),
    )

    with TestClient(app) as client:
        deep_link = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}",
            headers={"Authorization": "Bearer session-token"},
        )
        directory = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/access",
            headers={"Authorization": "Bearer session-token"},
        )

    assert deep_link.status_code == 404
    assert deep_link.json()["data"] == {"reason": "work_unavailable"}
    assert directory.status_code == 403
    assert directory.json()["data"] == {"reason": "work_management_forbidden"}


def test_work_revision_conflict_uses_stable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "update_work",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(service.WorkRevisionConflict),
    )

    with TestClient(app) as client:
        response = client.patch(
            f"/api/v1/workspaces/{uuid4()}/works/{uuid4()}",
            headers={"Authorization": "Bearer session-token"},
            json={
                "name": "测试作品",
                "description": "作品简介",
                "creativeStage": "原型",
                "targetExperience": "共同探索",
                "minPlayers": 2,
                "maxPlayers": 4,
                "estimatedDurationMinutes": 60,
                "expectedRevision": 1,
            },
        )

    assert response.status_code == 409
    assert response.json()["data"] == {"reason": "work_revision_conflict"}
