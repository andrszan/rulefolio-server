from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.evidence import service as evidence_service
from app.identity import service as identity_service
from app.identity.models import Account, SessionRecord
from app.issues import service as issues_service
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
        lambda *_, **__: (_ for _ in ()).throw(service.PlaytestParticipantUnavailable),
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


def test_result_invalid_uses_stable_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    account = Account(id=uuid4(), email="organizer@example.com", status="active")
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "read_result",
        lambda *_: (_ for _ in ()).throw(service.PlaytestResultInvalid),
    )
    workspace_id, work_id, session_id = uuid4(), uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/result",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 422
    assert response.json()["data"] == {"reason": "playtest_result_invalid"}


def test_result_management_forbidden_uses_stable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="collaborator@example.com", status="active")
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "read_result",
        lambda *_: (_ for _ in ()).throw(service.PlaytestManagementForbidden),
    )
    workspace_id, work_id, session_id = uuid4(), uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/result",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 403
    assert response.json()["data"] == {"reason": "playtest_management_forbidden"}


def test_openapi_includes_result_contract() -> None:
    with TestClient(app) as client:
        schema = client.get("/api/v1/openapi.json").json()

    request = schema["components"]["schemas"]["ResultRequest"]
    actual_material = schema["components"]["schemas"]["ActualMaterialRequest"]
    result = schema["components"]["schemas"]["ResultResponseData"]
    assert "actualMaterial" in request["properties"]
    assert "actualParticipants" in request["properties"]
    assert actual_material["properties"]["ruleContent"]["maxLength"] == 20_000
    assert (
        actual_material["properties"]["changeReason"]["anyOf"][0]["maxLength"] == 4_000
    )
    assert "materialCandidates" in result["properties"]
    assert request["properties"]["actualPlayMode"]["anyOf"][0]["maxLength"] == 160
    assert "actualPlayMode" in result["properties"]


def test_result_request_returns_field_error_for_invalid_actual_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="organizer@example.com", status="active")
    _authorize(monkeypatch, account)
    workspace_id, work_id, session_id = uuid4(), uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.put(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/result",
            headers={"Authorization": "Bearer session-token"},
            json={
                "expectedRevision": 1,
                "actualMaterial": {
                    "ruleName": "规则",
                    "ruleContent": "x" * 20_001,
                    "materialFileIds": [],
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["data"]["reason"] == "validation_failed"
    assert response.json()["data"]["errors"][0]["location"][-1] == "ruleContent"


def test_playtest_mail_links_only_to_the_protected_session() -> None:
    session_id = uuid4()

    subject, body = service._mail_content("雾林棋局", "playtest_invitation", session_id)

    assert subject == "你受邀参加试玩场次"
    assert f"/playtest-sessions/{session_id}" in body
    assert "规则" not in body


def test_feedback_management_forbidden_uses_stable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="collaborator@example.com", status="active")
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "read_feedback",
        lambda *_: (_ for _ in ()).throw(service.PlaytestManagementForbidden),
    )
    workspace_id, work_id, session_id = uuid4(), uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/feedback",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 403
    assert response.json()["data"] == {"reason": "feedback_management_forbidden"}


def test_feedback_submission_response_includes_current_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="organizer@example.com", status="active")
    _authorize(monkeypatch, account)
    submission = evidence_service.FeedbackSubmissionData(
        id=uuid4(),
        source="oral_discussion",
        temporary_alias=None,
        status="submitted",
        revision=1,
        recorded_by_account_id=account.id,
        recorded_by_email=account.email,
        updated_at=datetime.now(UTC),
        answers=(),
    )
    monkeypatch.setattr(service, "create_feedback_submission", lambda *_: submission)
    workspace_id, work_id, session_id = uuid4(), uuid4(), uuid4()

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/feedback/submissions",
            headers={
                "Authorization": "Bearer session-token",
                "Idempotency-Key": "feedback-create",
            },
            json={
                "source": "oral_discussion",
                "temporaryAlias": None,
                "answers": [{"itemId": str(uuid4()), "textValue": "记录内容"}],
            },
        )

    assert response.status_code == 201
    assert response.json()["data"]["recordedByAccountId"] == str(account.id)


def test_linked_direct_feedback_uses_conflict_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="guest@example.com", status="active")
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        service,
        "save_participant_feedback",
        lambda *_: (_ for _ in ()).throw(evidence_service.FeedbackSubmissionLinked),
    )

    with TestClient(app) as client:
        response = client.put(
            f"/api/v1/playtest-sessions/{uuid4()}/feedback/mine",
            headers={"Authorization": "Bearer session-token"},
            json={
                "status": "draft",
                "expectedRevision": 1,
                "answers": [{"itemId": str(uuid4()), "textValue": "当前答案"}],
            },
        )

    assert response.status_code == 409
    assert response.json()["data"] == {"reason": "feedback_submission_linked"}


def test_openapi_includes_feedback_contract() -> None:
    with TestClient(app) as client:
        schema = client.get("/api/v1/openapi.json").json()

    item = schema["components"]["schemas"]["FeedbackItemResponseData"]
    direct = schema["components"]["schemas"]["DirectFeedbackRequest"]
    participant = schema["components"]["schemas"]["ParticipantSessionResponseData"]
    submission = schema["components"]["schemas"]["FeedbackSubmissionResponseData"]
    assert "isLocked" in item["properties"]
    assert "expectedRevision" in direct["properties"]
    assert "feedbackItems" in participant["properties"]
    assert "recordedByAccountId" in submission["properties"]


def test_overview_uses_camel_case_minimal_projections_and_stable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=uuid4(), email="collaborator@example.com", status="active")
    _authorize(monkeypatch, account)
    workspace_id, work_id, session_id, issue_id = uuid4(), uuid4(), uuid4(), uuid4()
    scheduled_at = datetime(2026, 9, 16, 10, tzinfo=UTC)
    filters = service.OverviewFilters(
        scheduled_from=scheduled_at,
        actual_headcount_min=0,
        actual_headcount_max=2,
        actual_play_mode="实体桌游",
    )
    monkeypatch.setattr(
        service,
        "read_overview",
        lambda *_: service.OverviewData(
            filters=filters,
            included_session_count=1,
            missing_actual_headcount_count=0,
            missing_actual_duration_count=0,
            missing_completion_status_count=0,
            missing_actual_play_mode_count=0,
            temporary_variant_count=1,
            headcount_coverage=(service.HeadcountCoverageData(0, 1, 0, 0, 1),),
            play_modes=("实体桌游", "规则补充复测"),
        ),
    )
    monkeypatch.setattr(
        service,
        "list_overview_sessions",
        lambda *_: (
            [
                service.OverviewSessionData(
                    id=session_id,
                    scheduled_at=scheduled_at,
                    actual_headcount=0,
                    actual_duration_minutes=45,
                    completion_status="completed",
                    actual_play_mode="实体桌游",
                    has_temporary_variant=True,
                )
            ],
            1,
        ),
    )
    monkeypatch.setattr(
        service,
        "list_overview_issues",
        lambda *_: (
            [
                issues_service.OverviewIssueData(
                    id=issue_id,
                    description="开局提示需要调整。",
                    decision="modify",
                    status="open",
                    verification_status="pending",
                    current_conclusion_type=None,
                    current_conclusion_session_id=None,
                )
            ],
            1,
        ),
    )

    with TestClient(app) as client:
        summary = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-overview",
            params={
                "scheduledFrom": scheduled_at.isoformat(),
                "actualHeadcountMin": "0",
                "actualHeadcountMax": "2",
                "actualPlayMode": "实体桌游",
            },
            headers={"Authorization": "Bearer session-token"},
        )
        sessions = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-overview/sessions",
            headers={"Authorization": "Bearer session-token"},
        )
        issues = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-overview/issues",
            params={"kind": "pending-retest"},
            headers={"Authorization": "Bearer session-token"},
        )
        invalid = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-overview",
            params={"actualHeadcountMin": "2", "actualHeadcountMax": "0"},
            headers={"Authorization": "Bearer session-token"},
        )

    assert summary.status_code == sessions.status_code == issues.status_code == 200
    assert summary.json()["data"] == {
        "filters": {
            "scheduledFrom": scheduled_at.isoformat(),
            "scheduledBefore": None,
            "actualHeadcountMin": 0,
            "actualHeadcountMax": 2,
            "actualPlayMode": "实体桌游",
        },
        "includedSessionCount": 1,
        "missingActualHeadcountCount": 0,
        "missingActualDurationCount": 0,
        "missingCompletionStatusCount": 0,
        "missingActualPlayModeCount": 0,
        "temporaryVariantCount": 1,
        "headcountCoverage": [
            {
                "actualHeadcount": 0,
                "completedCount": 1,
                "interruptedCount": 0,
                "unrecordedCompletionCount": 0,
                "temporaryVariantCount": 1,
            }
        ],
        "playModes": ["实体桌游", "规则补充复测"],
    }
    assert sessions.json()["data"]["items"] == [
        {
            "id": str(session_id),
            "scheduledAt": scheduled_at.isoformat(),
            "actualHeadcount": 0,
            "actualDurationMinutes": 45,
            "completionStatus": "completed",
            "actualPlayMode": "实体桌游",
            "hasTemporaryVariant": True,
        }
    ]
    assert issues.json()["data"]["items"] == [
        {
            "id": str(issue_id),
            "description": "开局提示需要调整。",
            "decision": "modify",
            "status": "open",
            "verificationStatus": "pending",
            "currentConclusionType": None,
            "currentConclusionSessionId": None,
        }
    ]
    assert invalid.status_code == 422
    assert invalid.json()["data"] == {"reason": "playtest_overview_invalid"}

    monkeypatch.setattr(
        service,
        "read_overview",
        lambda *_: (_ for _ in ()).throw(service.PlaytestOverviewUnavailable),
    )
    with TestClient(app) as client:
        unavailable = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-overview",
            headers={"Authorization": "Bearer session-token"},
        )
        invalid_kind = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-overview/issues",
            params={"kind": "unknown"},
            headers={"Authorization": "Bearer session-token"},
        )

    assert unavailable.status_code == 404
    assert unavailable.json()["data"] == {"reason": "playtest_overview_unavailable"}
    assert invalid_kind.status_code == 422
    assert invalid_kind.json()["data"] == {"reason": "playtest_overview_invalid"}
