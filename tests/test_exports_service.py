import json
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest

from app.exports import service
from app.files import materials as file_materials
from app.files import service as files_service
from app.works import service as works_service


class SessionStub:
    def execute(self, *_: object) -> None:
        return None

    def rollback(self) -> None:
        return None


def _work(workspace_id, work_id, role: str = "collaborator") -> works_service.WorkData:
    return works_service.WorkData(
        id=work_id,
        workspace_id=workspace_id,
        name="星海远征",
        description="合作探索游戏",
        creative_stage="原型",
        target_experience="共同探索",
        min_players=2,
        max_players=4,
        estimated_duration_minutes=60,
        revision=3,
        own_role=role,
        can_manage_access=role == "maintainer",
    )


def _rule_materials(
    material: file_materials.MaterialData,
) -> works_service.RuleMaterialsData:
    return works_service.RuleMaterialsData(
        rule_name="核心规则",
        rule_description=None,
        rule_content="准备后开始游戏。",
        materials=(material,),
        revision=3,
    )


def test_build_work_export_writes_complete_collaborator_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid4()
    workspace_id = uuid4()
    work_id = uuid4()
    material = file_materials.MaterialData(
        id=uuid4(),
        display_name="../当前材料\x00.pdf",
        detected_content_type="application/pdf",
        size_bytes=7,
        sha256=sha256(b"archive").hexdigest(),
        created_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    payload = b"archive"
    monkeypatch.setattr(service, "set_actor", lambda *_: None)
    monkeypatch.setattr(
        service.works_service,
        "read_work",
        lambda *_: _work(workspace_id, work_id),
    )
    monkeypatch.setattr(
        service.works_service,
        "read_rule_materials",
        lambda *_: _rule_materials(material),
    )
    monkeypatch.setattr(
        service.files_service,
        "open_material",
        lambda *_: files_service.ImageStream(material, BytesIO(payload)),
    )

    archive = service.build_work_export(SessionStub(), actor_id, workspace_id, work_id)
    try:
        with ZipFile(archive.body) as zip_file:
            names = zip_file.namelist()
            assert names == [
                f"materials/{material.id}-_当前材料_.pdf",
                "manifest.json",
                "data.json",
            ]
            manifest = json.loads(zip_file.read("manifest.json"))
            data = json.loads(zip_file.read("data.json"))
            assert manifest["format"] == "rulefolio-work-export"
            assert manifest["formatVersion"] == 1
            assert manifest["workspaceId"] == str(workspace_id)
            assert manifest["workId"] == str(work_id)
            assert manifest["files"] == [
                {
                    "contentType": "application/pdf",
                    "id": str(material.id),
                    "path": f"materials/{material.id}-_当前材料_.pdf",
                    "sha256": sha256(payload).hexdigest(),
                    "sizeBytes": len(payload),
                }
            ]
            assert set(data) == {"currentRule", "work"}
            assert zip_file.read(names[0]) == payload
    finally:
        archive.body.close()


def test_build_work_export_hides_unavailable_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "set_actor", lambda *_: None)
    monkeypatch.setattr(
        service.works_service,
        "read_work",
        lambda *_: (_ for _ in ()).throw(works_service.WorkUnavailable()),
    )

    with pytest.raises(service.WorkExportUnavailable):
        service.build_work_export(SessionStub(), uuid4(), uuid4(), uuid4())


def test_build_work_export_retries_when_current_material_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid4()
    workspace_id = uuid4()
    work_id = uuid4()
    material = file_materials.MaterialData(
        id=uuid4(),
        display_name="当前材料.pdf",
        detected_content_type="application/pdf",
        size_bytes=7,
        sha256="0" * 64,
        created_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    monkeypatch.setattr(service, "set_actor", lambda *_: None)
    monkeypatch.setattr(
        service.works_service,
        "read_work",
        lambda *_: _work(workspace_id, work_id),
    )
    monkeypatch.setattr(
        service.works_service,
        "read_rule_materials",
        lambda *_: _rule_materials(material),
    )
    monkeypatch.setattr(
        service.files_service,
        "open_material",
        lambda *_: (_ for _ in ()).throw(files_service.MaterialUnavailable()),
    )

    with pytest.raises(service.WorkExportRetryable):
        service.build_work_export(SessionStub(), actor_id, workspace_id, work_id)


def test_all_pages_reads_every_page() -> None:
    calls = []

    def fetch(page: int, size: int) -> tuple[list[str], int]:
        calls.append((page, size))
        return (["first"], 2) if page == 1 else (["second"], 2)

    assert service._all_pages(fetch) == ["first", "second"]
    assert calls == [(1, service.PAGE_SIZE), (2, service.PAGE_SIZE)]


def test_build_work_export_retries_when_current_material_hash_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid4()
    workspace_id = uuid4()
    work_id = uuid4()
    material = file_materials.MaterialData(
        id=uuid4(),
        display_name="当前材料.pdf",
        detected_content_type="application/pdf",
        size_bytes=7,
        sha256="0" * 64,
        created_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    monkeypatch.setattr(service, "set_actor", lambda *_: None)
    monkeypatch.setattr(
        service.works_service,
        "read_work",
        lambda *_: _work(workspace_id, work_id),
    )
    monkeypatch.setattr(
        service.works_service,
        "read_rule_materials",
        lambda *_: _rule_materials(material),
    )
    monkeypatch.setattr(
        service.files_service,
        "open_material",
        lambda *_: files_service.ImageStream(material, BytesIO(b"archive")),
    )

    with pytest.raises(service.WorkExportRetryable):
        service.build_work_export(SessionStub(), actor_id, workspace_id, work_id)
