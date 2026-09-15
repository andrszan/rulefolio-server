from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.identity import service as identity_service
from app.identity.models import Account, SessionRecord
from app.main import app
from app.playtests import service


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


def test_participant_unavailable_uses_the_stable_404_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="guest@example.com", status="active")
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "read_participant_session",
        lambda *_: (_ for _ in ()).throw(service.PlaytestUnavailable),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/playtest-sessions/{uuid4()}",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 404
    assert response.json()["data"] == {"reason": "playtest_unavailable"}


def test_create_plan_hides_unavailable_participant_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="organizer@example.com", status="active")
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "create_plan",
        lambda *_: (_ for _ in ()).throw(service.PlaytestParticipantUnavailable),
    )
    workspace_id, work_id = uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-plans",
            headers={"Authorization": "Bearer session-token"},
            json={
                "observationGoal": "验证输入",
                "recordingMethod": "记录讨论",
                "sessions": [
                    {
                        "scheduledAt": "2026-09-16T10:00:00Z",
                        "location": "试玩桌",
                        "capacity": 2,
                        "materialFileIds": [str(uuid4())],
                        "participantEmails": ["unknown@example.com"],
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["data"] == {"reason": "playtest_participant_unavailable"}


def test_openapi_uses_playtest_frontend_field_names() -> None:
    with TestClient(app) as client:
        schema = client.get("/api/v1/openapi.json").json()

    create = schema["components"]["schemas"]["CreatePlanRequest"]
    participant = schema["components"]["schemas"]["ParticipantSessionResponseData"]
    assert "observationGoal" in create["properties"]
    assert "observationGoal" in create["required"]
    assert "confirmationStatus" in participant["properties"]


def test_playtest_mail_links_only_to_the_protected_session() -> None:
    session_id = uuid4()

    subject, body = service._mail_content("雾林棋局", "playtest_invitation", session_id)

    assert subject == "你受邀参加试玩场次"
    assert f"/playtest-sessions/{session_id}" in body
    assert "规则" not in body
