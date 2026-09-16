from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.identity import service as identity_service
from app.identity.models import Account, SessionRecord
from app.issues import service
from app.main import app


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


def test_issue_unavailable_hides_issue_and_source_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="maintainer@example.com", status="active")
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "read_issue",
        lambda *_: (_ for _ in ()).throw(service.IssueUnavailable),
    )
    workspace_id, work_id, issue_id = uuid4(), uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/issues/{issue_id}",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 404
    assert response.json()["data"] == {"reason": "issue_unavailable"}


def test_issue_management_forbidden_uses_stable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="organizer@example.com", status="active")
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "list_issues",
        lambda *_: (_ for _ in ()).throw(service.IssueManagementForbidden),
    )
    workspace_id, work_id = uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/issues",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 403
    assert response.json()["data"] == {"reason": "issue_management_forbidden"}


def test_create_issue_returns_camel_case_summary_and_operation_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="maintainer@example.com", status="active")
    _authorize(monkeypatch, account)
    now = datetime.now(UTC)
    issue = service.IssueData(
        id=uuid4(),
        description="终局结算需要示例。",
        decision="modify",
        reason="来源都指出理解障碍。",
        adjustment_note="已补充终局结算示例。",
        verification_status="pending",
        current_conclusion=None,
        status="open",
        revision=1,
        created_at=now,
        updated_at=now,
        source_count=1,
    )
    monkeypatch.setattr(service, "create_issue", lambda *_1, **_2: issue)
    workspace_id, work_id, source_id = uuid4(), uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/issues",
            headers={
                "Authorization": "Bearer session-token",
                "Idempotency-Key": "issue-create",
            },
            json={
                "description": "终局结算需要示例。",
                "decision": "modify",
                "reason": "来源都指出理解障碍。",
                "sources": [{"sourceType": "observation", "sourceId": str(source_id)}],
            },
        )

    assert response.status_code == 201
    assert response.json()["data"]["sourceCount"] == 1
    assert response.json()["data"]["adjustmentNote"] == "已补充终局结算示例。"
    assert response.json()["data"]["verificationStatus"] == "pending"
    assert response.json()["data"]["createdAt"] == now.isoformat()

    monkeypatch.setattr(
        service,
        "create_issue",
        lambda *_1, **_2: (_ for _ in ()).throw(service.IssueOperationConflict),
    )
    with TestClient(app) as client:
        conflict = client.post(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/issues",
            headers={
                "Authorization": "Bearer session-token",
                "Idempotency-Key": "issue-create",
            },
            json={
                "description": "另一条问题。",
                "decision": "observe",
                "reason": "不同创建意图。",
                "sources": [{"sourceType": "observation", "sourceId": str(source_id)}],
            },
        )

    assert conflict.status_code == 409
    assert conflict.json()["data"] == {"reason": "issue_operation_conflict"}


def test_invalid_issue_values_use_issue_invalid_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="maintainer@example.com", status="active")
    _authorize(monkeypatch, account)
    workspace_id, work_id = uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/issues",
            headers={
                "Authorization": "Bearer session-token",
                "Idempotency-Key": "issue-invalid",
            },
            json={
                "description": "无效决定。",
                "decision": "unknown",
                "reason": "验证稳定错误。",
                "sources": [{"sourceType": "observation", "sourceId": str(uuid4())}],
            },
        )

    assert response.status_code == 422
    assert response.json()["data"] == {"reason": "issue_invalid"}


def test_openapi_includes_issue_camel_case_contract() -> None:
    with TestClient(app) as client:
        schema = client.get("/api/v1/openapi.json").json()

    create = schema["components"]["schemas"]["IssueCreateRequest"]
    reference = schema["components"]["schemas"]["IssueEvidenceReferenceRequest"]
    response = schema["components"]["schemas"]["IssueResponseData"]
    evidence = schema["components"]["schemas"]["IssueEvidenceResponseData"]
    evidence_session = schema["components"]["schemas"][
        "IssueEvidenceSessionResponseData"
    ]
    retest = schema["components"]["schemas"]["IssueRetestResponseData"]
    assert "sources" in create["properties"]
    assert "sourceType" in reference["properties"]
    assert "sourceCount" in response["properties"]
    assert "adjustmentNote" in response["properties"]
    assert "verificationStatus" in response["properties"]
    assert "currentConclusion" in response["properties"]
    assert "linkId" in evidence["properties"]
    assert "sourceType" in evidence["properties"]
    assert "location" in evidence_session["properties"]
    assert "currentAdjustment" in retest["properties"]
