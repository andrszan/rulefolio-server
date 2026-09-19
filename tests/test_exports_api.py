from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.exports import service as exports_service
from app.identity import service as identity_service
from app.identity.models import Account, SessionRecord
from app.main import app


def _account() -> Account:
    return Account(id=uuid4(), email="exporter@example.com", status="active")


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


def test_download_work_export_returns_private_zip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    workspace_id = uuid4()
    work_id = uuid4()
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        exports_service,
        "build_work_export",
        lambda *_: exports_service.WorkExport(
            BytesIO(b"archive"), "星海远征-作品资料.zip"
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/export",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 200
    assert response.content == b"archive"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].startswith(
        "attachment; filename*=UTF-8''"
    )


def test_download_work_export_hides_unavailable_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        exports_service,
        "build_work_export",
        lambda *_: (_ for _ in ()).throw(exports_service.WorkExportUnavailable()),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{uuid4()}/works/{uuid4()}/export",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 404
    assert response.json()["data"] == {"reason": "work_export_unavailable"}


def test_download_work_export_marks_partial_build_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    _authorize(monkeypatch, account)
    monkeypatch.setattr(
        exports_service,
        "build_work_export",
        lambda *_: (_ for _ in ()).throw(exports_service.WorkExportRetryable()),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{uuid4()}/works/{uuid4()}/export",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["data"] == {"reason": "work_export_retryable"}
