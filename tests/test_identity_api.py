from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.identity import service
from app.identity.models import Account, SessionRecord
from app.main import app


def session_result() -> service.SessionResult:
    return service.SessionResult(
        token="session-token",
        account_id=uuid4(),
        account_status="active",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_login_returns_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    result = session_result()
    monkeypatch.setattr(service, "login", lambda *_: result)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sessions",
            json={"email": "member@example.com", "password": "long-password"},
        )

    assert response.status_code == 200
    assert response.json()["code"] == 200
    assert response.json()["data"] == {
        "sessionToken": "session-token",
        "account": {"id": str(result.account_id), "status": "active"},
        "expiresAt": result.expires_at.isoformat().replace("+00:00", "Z"),
    }


def test_login_failure_does_not_distinguish_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object) -> service.SessionResult:
        raise service.AuthenticationFailed

    monkeypatch.setattr(service, "login", fail)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sessions",
            json={"email": "missing@example.com", "password": "incorrect"},
        )

    assert response.status_code == 401
    assert response.json()["data"] == {"reason": "authentication_failed"}


def test_recovery_request_has_fixed_accepted_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[str] = []

    def request_recovery(_, email: str) -> None:
        received.append(email)

    monkeypatch.setattr(service, "request_recovery", request_recovery)
    with TestClient(app) as client:
        responses = [
            client.post("/api/v1/account-recovery-requests", json={"email": email})
            for email in (
                "active@example.com",
                "disabled@example.com",
                "missing@example.com",
            )
        ]

    assert received == [
        "active@example.com",
        "disabled@example.com",
        "missing@example.com",
    ]
    assert all(response.status_code == 202 for response in responses)
    assert [response.json() for response in responses] == [responses[0].json()] * 3
    assert responses[0].json()["data"] == {"reason": "recovery_request_accepted"}


def test_current_session_requires_bearer_token() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions/current")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["data"] == {"reason": "session_unavailable"}


def test_recovery_exchange_hides_invalid_link_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_: object) -> service.SessionResult:
        raise service.LinkUnavailable

    monkeypatch.setattr(service, "recover_password", unavailable)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/account-recovery-exchanges",
            json={"token": "not-a-valid-link", "newPassword": "long-enough-password"},
        )

    assert response.status_code == 400
    assert response.json()["data"] == {"reason": "recovery_link_unavailable"}
    assert "not-a-valid-link" not in response.text


def test_current_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    result = session_result()
    account = Account(id=result.account_id, email="member@example.com", status="active")
    record = SessionRecord(
        account_id=result.account_id,
        token_hash=b"0" * 32,
        expires_at=result.expires_at,
    )
    monkeypatch.setattr(
        service,
        "authenticate",
        lambda *_: service.AuthenticatedSession(record=record, account=account),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/sessions/current",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 200
    assert response.json()["data"]["account"] == {
        "id": str(result.account_id),
        "status": "active",
    }
