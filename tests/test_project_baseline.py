from uuid import UUID

import pytest
from sqlalchemy import func, select

from app import project_baseline
from app.access.context import set_actor, set_project_baseline_scope
from app.core.config import settings
from app.core.database import SessionLocal
from app.evidence.models import PlaytestObservation
from app.files import service as files_service
from app.files import storage
from app.files.models import StoredFile
from app.identity import service as identity_service
from app.identity.models import Account, OneTimeCredential, SessionRecord
from app.notifications.models import MailOutbox
from app.playtests.models import (
    PlaytestPlan,
    PlaytestSession,
    PlaytestSessionActualMaterial,
    PlaytestSessionActualParticipant,
    PlaytestSessionParticipant,
)
from app.project_baseline import (
    BASELINE_ACCOUNTS,
    BASELINE_IMAGE,
    BASELINE_PLAYER_AID,
    BASELINE_RULES,
    BaselineOperationFailed,
    initialize,
    reset,
    reset_confirmation,
)
from app.works.models import Work, WorkMaterialFile
from app.workspaces.models import WorkAccess, Workspace, WorkspaceMember

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test"
    or settings.s3_bucket_name != settings.s3_test_bucket_name,
    reason="需要 TEST_DB_NAME 和 S3_TEST_BUCKET_NAME",
)


def _reset() -> None:
    with SessionLocal() as session:
        result = reset(session, "pytest", "BR-006 集成验证", reset_confirmation())
    assert result.status == "reset"


def _baseline_state() -> tuple[str, UUID, UUID, UUID, UUID, tuple[UUID, ...]]:
    with SessionLocal() as session:
        set_project_baseline_scope(session)
        accounts = list(session.scalars(select(Account).order_by(Account.email)))
        workspaces = list(session.scalars(select(Workspace)))
        works = list(session.scalars(select(Work)))
        accesses = list(session.scalars(select(WorkAccess)))
        files = list(session.scalars(select(StoredFile)))
        material_relations = list(session.scalars(select(WorkMaterialFile)))
        plans = list(session.scalars(select(PlaytestPlan)))
        sessions = list(session.scalars(select(PlaytestSession)))
        participants = list(session.scalars(select(PlaytestSessionParticipant)))
        actual_materials = list(session.scalars(select(PlaytestSessionActualMaterial)))
        actual_participants = list(
            session.scalars(select(PlaytestSessionActualParticipant))
        )
        observations = list(session.scalars(select(PlaytestObservation)))
        assert [account.email for account in accounts] == sorted(
            account.email for account in BASELINE_ACCOUNTS
        )
        assert len(workspaces) == len(works) == len(plans) == 1
        assert len(accesses) == 3
        assert len(sessions) == 2
        assert {item.completion_status for item in sessions} == {
            "completed",
            "interrupted",
        }
        assert sum(item.actual_headcount is None for item in sessions) == 1
        assert len(participants) == 2
        assert len(actual_materials) == 3
        assert len(actual_participants) == 2
        assert {item.kind for item in observations} == {
            "fact",
            "organizer_interpretation",
            "temporary_variant",
        }
        assert (
            sum(participant.status == "confirmed" for participant in participants) == 1
        )
        playtester = next(
            account
            for account in accounts
            if account.email == "playtest-guest@example.com"
        )
        assert (
            session.scalar(
                select(WorkspaceMember.id).where(
                    WorkspaceMember.account_id == playtester.id
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(WorkAccess.account_id).where(
                    WorkAccess.account_id == playtester.id
                )
            )
            is None
        )
        images = [file for file in files if file.kind == "image"]
        materials = sorted(
            (file for file in files if file.kind == "material"),
            key=lambda file: file.display_name,
        )
        assert len(images) == 1
        assert len(materials) == len(material_relations) == 2
        assert {relation.file_id for relation in material_relations} == {
            material.id for material in materials
        }
        assert session.scalar(select(func.count()).select_from(OneTimeCredential)) == 0
        outbox_statuses = list(session.scalars(select(MailOutbox.status)))
        assert len(outbox_statuses) == 2
        assert any(
            status in {"accepted", "failed", "unknown", "pending"}
            for status in outbox_statuses
        )
        assert session.scalar(select(func.count()).select_from(SessionRecord)) == 0
        owner = next(
            account
            for account in accounts
            if account.email == BASELINE_ACCOUNTS[0].email
        )
        workspace, work, image = workspaces[0], works[0], images[0]
        result = (
            owner.email,
            owner.id,
            workspace.id,
            work.id,
            image.id,
            tuple(material.id for material in materials),
        )
        object_keys = sorted(file.object_key for file in files)
        session.rollback()
    assert storage.list_object_keys() == object_keys
    return result


def test_reset_initializes_rejects_reentry_and_restores_real_image() -> None:
    _reset()
    owner_email, owner_id, workspace_id, work_id, image_id, _ = _baseline_state()

    with SessionLocal() as session:
        rejected = initialize(session, "pytest", "重复初始化验证")
    assert rejected.status == "not_initialized"
    assert rejected.counts["records"] > 0

    with SessionLocal() as session:
        first_session = identity_service.login(
            session,
            owner_email,
            settings.baseline_password.get_secret_value(),
        )
        set_actor(session, owner_id)
        with BASELINE_IMAGE.open("rb") as source:
            files_service.upload_image(
                session,
                owner_id,
                workspace_id,
                work_id,
                source,
                "额外图片.jpg",
                "image/jpeg",
            )

    _reset()
    with SessionLocal() as session:
        with pytest.raises(identity_service.SessionUnavailable):
            identity_service.authenticate(session, first_session.token)
    owner_email, owner_id, workspace_id, work_id, image_id, material_ids = (
        _baseline_state()
    )

    with SessionLocal() as session:
        for account in BASELINE_ACCOUNTS:
            identity_service.login(
                session,
                account.email,
                settings.baseline_password.get_secret_value(),
            )
        set_actor(session, owner_id)
        stream = files_service.open_image(
            session, owner_id, workspace_id, work_id, image_id
        )
        try:
            assert stream.body.read() == BASELINE_IMAGE.read_bytes()
        finally:
            stream.body.close()
        for material_id, source_path in zip(
            material_ids, (BASELINE_RULES, BASELINE_PLAYER_AID), strict=True
        ):
            stream = files_service.open_material(
                session, owner_id, workspace_id, work_id, material_id
            )
            try:
                assert stream.body.read() == source_path.read_bytes()
            finally:
                stream.body.close()

    _reset()
    _baseline_state()


def test_reset_requires_confirmation_and_preserves_database_on_object_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset()
    with SessionLocal() as session:
        result = reset(session, "pytest", "确认验证", None)
    assert result.status == "confirmation_required"
    assert result.confirmation_target == reset_confirmation()

    def fail_delete(_: list[str]) -> int:
        raise storage.StorageUnavailable(1)

    monkeypatch.setattr(storage, "delete_objects", fail_delete)
    with SessionLocal() as session, pytest.raises(BaselineOperationFailed) as error:
        reset(session, "pytest", "对象失败验证", reset_confirmation())
    assert error.value.phase == "object_cleanup"
    assert error.value.counts == {"records": 0, "objects": 1}
    _baseline_state()


def test_reset_reports_only_completed_object_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset()

    def delete_objects(keys: list[str]) -> int:
        return len(keys)

    def fail_database_cleanup(_: object) -> int:
        raise BaselineOperationFailed("database_cleanup", {})

    monkeypatch.setattr(storage, "delete_objects", delete_objects)
    monkeypatch.setattr(project_baseline, "_clear_database", fail_database_cleanup)
    with SessionLocal() as session, pytest.raises(BaselineOperationFailed) as error:
        reset(session, "pytest", "数据库失败验证", reset_confirmation())
    assert error.value.phase == "database_cleanup"
    assert error.value.counts == {"records": 0, "objects": 3}
    _baseline_state()


def test_initialize_reports_only_committed_counts_on_image_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset()
    storage.delete_objects(storage.list_object_keys())
    with SessionLocal() as session:
        project_baseline._clear_database(session)

    def fail_upload(*_: object) -> None:
        raise files_service.FileOperationRetryable

    monkeypatch.setattr(files_service, "upload_image", fail_upload)
    with SessionLocal() as session, pytest.raises(BaselineOperationFailed) as error:
        initialize(session, "pytest", "图片失败验证")
    assert error.value.phase == "image"
    assert error.value.counts == {
        "accounts": 4,
        "workspaces": 1,
        "works": 1,
        "accesses": 3,
    }
    assert storage.list_object_keys() == []
    with SessionLocal() as session:
        set_project_baseline_scope(session)
        assert session.scalar(select(func.count()).select_from(Account)) == 4
        assert session.scalar(select(func.count()).select_from(Workspace)) == 1
        assert session.scalar(select(func.count()).select_from(Work)) == 1
        assert session.scalar(select(func.count()).select_from(WorkAccess)) == 3
        assert session.scalar(select(func.count()).select_from(StoredFile)) == 0
        session.rollback()

    monkeypatch.undo()
    _reset()
    _baseline_state()
