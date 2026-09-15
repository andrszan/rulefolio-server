from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.files import service
from app.identity import service as identity_service
from app.identity.models import Account, SessionRecord
from app.main import app


def _account() -> Account:
    return Account(id=uuid4(), email="member@example.com", status="active")


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


def _image() -> service.ImageData:
    return service.ImageData(
        id=uuid4(),
        display_name="作品图片.jpg",
        detected_content_type="image/jpeg",
        size_bytes=1024,
        created_at=datetime(2026, 9, 15, tzinfo=UTC),
    )


def test_upload_returns_private_image_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    account = _account()
    workspace_id = uuid4()
    work_id = uuid4()
    image = _image()
    _authorize(monkeypatch, account)

    def upload(
        _,
        actor_id,
        received_workspace_id,
        received_work_id,
        source,
        filename,
        content_type,
    ) -> service.ImageData:
        assert actor_id == account.id
        assert received_workspace_id == workspace_id
        assert received_work_id == work_id
        assert source.read() == b"image"
        assert filename == "作品图片.jpg"
        assert content_type == "image/jpeg"
        return image

    monkeypatch.setattr(service, "upload_image", upload)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/images",
            headers={"Authorization": "Bearer session-token"},
            files={"image": ("作品图片.jpg", b"image", "image/jpeg")},
        )

    assert response.status_code == 201
    assert response.json()["data"] == {
        "id": str(image.id),
        "displayName": "作品图片.jpg",
        "detectedContentType": "image/jpeg",
        "sizeBytes": 1024,
        "createdAt": "2026-09-15T00:00:00+00:00",
    }


def test_list_exposes_server_limits_and_preview_hides_unavailable_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    workspace_id = uuid4()
    work_id = uuid4()
    _authorize(monkeypatch, account)
    monkeypatch.setattr(service, "list_images", lambda *_: ([_image()], 1))
    monkeypatch.setattr(
        service,
        "open_image",
        lambda *_: (_ for _ in ()).throw(service.ImageUnavailable),
    )

    with TestClient(app) as client:
        listed = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/images",
            headers={"Authorization": "Bearer session-token"},
        )
        preview = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/images/{uuid4()}/preview",
            headers={"Authorization": "Bearer session-token"},
        )

    assert listed.status_code == 200
    assert listed.json()["data"]["limits"] == {
        "allowedContentTypes": ["image/jpeg", "image/png", "image/webp"],
        "maxBytes": 5 * 1024 * 1024,
        "maxPixels": 16_000_000,
        "maxCount": 12,
        "maxTotalBytes": 30 * 1024 * 1024,
    }
    assert preview.status_code == 404
    assert preview.json()["data"] == {"reason": "image_unavailable"}
