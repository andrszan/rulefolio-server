from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.files import service
from app.files.policy import MAX_IMAGE_BYTES, MAX_MATERIAL_BYTES

FIXTURES = Path(__file__).parent / "fixtures"


def test_real_image_bytes_determine_type_and_digest() -> None:
    with (FIXTURES / "board-game-box.jpg").open("rb") as source:
        image = service._read_image(source, "../作品图片.jpg", "text/plain")

    try:
        assert image.display_name == "作品图片.jpg"
        assert image.declared_content_type == "text/plain"
        assert image.detected_content_type == "image/jpeg"
        assert image.size_bytes > 0
        assert len(image.digest) == 32
    finally:
        image.close()


@pytest.mark.parametrize(
    ("format_name", "detected_content_type"),
    (("PNG", "image/png"), ("WEBP", "image/webp")),
)
def test_allowed_image_formats_are_detected(
    format_name: str, detected_content_type: str
) -> None:
    source = BytesIO()
    Image.new("RGB", (2, 2)).save(source, format=format_name)
    source.seek(0)
    image = service._read_image(source, "image.jpg", "text/plain")

    try:
        assert image.detected_content_type == detected_content_type
    finally:
        image.close()


def test_invalid_or_too_large_bytes_never_become_an_image() -> None:
    with pytest.raises(service.ImageTypeNotAllowed):
        service._read_image(BytesIO(b"not an image"), "image.jpg", "image/jpeg")

    with pytest.raises(service.ImageLimitExceeded):
        service._read_image(
            BytesIO(b"x" * (MAX_IMAGE_BYTES + 1)), "image.jpg", "image/jpeg"
        )


def test_material_reader_validates_real_pdf_and_rejects_invalid_bytes() -> None:
    source = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(source)
    source.seek(0)

    material = service._read_material(source, "../规则书.pdf", "text/plain")
    try:
        assert material.display_name == "规则书.pdf"
        assert material.detected_content_type == "application/pdf"
        assert len(material.digest) == 32
    finally:
        material.close()

    with pytest.raises(service.MaterialTypeNotAllowed):
        service._read_material(BytesIO(b"%PDF-broken"), "规则书.pdf", "application/pdf")
    with pytest.raises(service.MaterialLimitExceeded):
        service._read_material(
            BytesIO(b"x" * (MAX_MATERIAL_BYTES + 1)),
            "规则书.pdf",
            "application/pdf",
        )


def test_recovery_never_deletes_a_file_marked_ready_by_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = SimpleNamespace(id=uuid4())
    session = Mock()
    session.scalar.return_value = file
    actual = Mock()
    lock = Mock()
    delete = Mock()
    monkeypatch.setattr(service, "set_actor", lambda *_: None)
    monkeypatch.setattr(service, "set_file_recovery_work_scope", lambda *_: None)
    monkeypatch.setattr(service.works_service, "lock_work_for_files", lock)
    monkeypatch.setattr(service, "_stored_upload", lambda _: actual)
    monkeypatch.setattr(service, "_matches", lambda *_: True)
    monkeypatch.setattr(service, "_mark_ready", lambda *_: False)
    monkeypatch.setattr(service.storage, "delete_object", delete)

    service._reconcile_pending(session, uuid4(), uuid4(), uuid4(), file.id)

    actual.close.assert_called_once()
    delete.assert_not_called()
