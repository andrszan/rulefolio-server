import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text, update

from app.access.context import set_recovery_maintenance_scope
from app.core.config import settings
from app.core.database import SessionLocal
from app.files.models import StoredFile
from app.identity import service as identity_service
from app.identity.models import OneTimeCredential, SessionRecord
from app.notifications import dispatcher
from app.notifications.models import MailOutbox
from app.project_baseline import reset, reset_confirmation
from app.recovery import service as recovery_service
from app.recovery.gate import api_requests_allowed, dispatcher_allowed
from app.recovery.models import RecoveryState
from app.recovery.service import (
    ManifestObject,
    RecoveryFailed,
    confirm_restricted_access,
    load_manifest,
    open_dispatcher,
    recover,
)

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test"
    or settings.s3_bucket_name != settings.s3_test_bucket_name,
    reason="需要 TEST_DB_NAME 和 S3_TEST_BUCKET_NAME",
)


def _reset() -> None:
    with SessionLocal() as session:
        assert (
            reset(session, "pytest", "BR-015 集成验证", reset_confirmation()).status
            == "reset"
        )


def _manifest(path: Path, recovery_id: str, *, wrong_size: bool = False) -> Path:
    with SessionLocal() as session:
        set_recovery_maintenance_scope(session)
        files = [
            (file.object_key, file.size_bytes, file.sha256)
            for file in session.scalars(
                select(StoredFile)
                .where(StoredFile.status == "ready")
                .order_by(StoredFile.object_key)
            )
        ]
        session.rollback()
    objects = [
        ManifestObject(
            key,
            size + (1 if wrong_size and index == 0 else 0),
            sha256,
        )
        for index, (key, size, sha256) in enumerate(files)
    ]
    digest = hashlib.sha256()
    for item in objects:
        digest.update(item.key.encode())
        digest.update(b"\0")
        digest.update(str(item.size_bytes).encode())
        digest.update(b"\0")
        digest.update(item.sha256.hex().encode())
        digest.update(b"\n")
    path.write_text(
        json.dumps(
            {
                "formatVersion": 1,
                "recoveryId": recovery_id,
                "target": {
                    "database": settings.db_name,
                    "bucket": settings.s3_bucket_name,
                },
                "targetTime": datetime.now(UTC).isoformat(),
                "archiveSha256": "0" * 64,
                "objects": [
                    {
                        "key": item.key,
                        "sizeBytes": item.size_bytes,
                        "sha256": item.sha256.hex(),
                    }
                    for item in objects
                ],
                "objectsSha256": digest.hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return path


def _restore_open_state() -> None:
    with SessionLocal() as session:
        set_recovery_maintenance_scope(session)
        state = session.get(RecoveryState, 1)
        assert state is not None
        state.stage = "open"
        state.recovery_id = None
        state.manifest_sha256 = None
        state.recovery_target_at = None
        state.authorization_mode = None
        state.restricted_access_confirmed_at = None
        state.verified_at = None
        state.operator = None
        session.commit()


def _provision_active_credential_and_session() -> tuple[object, object, object]:
    suffix = uuid4().hex
    with SessionLocal() as session:
        account_id = identity_service.provision_account(
            session, f"recovery-{suffix}@example.com", "pytest", "BR-015 验证"
        )
        credential = session.scalar(
            select(OneTimeCredential).where(OneTimeCredential.account_id == account_id)
        )
        outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.recipient_account_id == account_id)
        )
        assert credential is not None and outbox is not None
        record = SessionRecord(
            account_id=account_id,
            token_hash=uuid4().bytes + uuid4().bytes,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(record)
        session.commit()
        return record.id, credential.id, outbox.id


def test_restricted_recovery_revokes_credentials_and_allows_next_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    try:
        session_id, credential_id, outbox_id = (
            _provision_active_credential_and_session()
        )
        with SessionLocal() as session:
            session.execute(
                update(MailOutbox)
                .where(MailOutbox.id == outbox_id)
                .values(status="unknown")
            )
            session.commit()
        manifest = load_manifest(
            _manifest(tmp_path / "manifest.json", "restricted-001")
        )
        monkeypatch.setattr(settings, "recovery_deployment_frozen", True)
        with SessionLocal() as session:
            result = recover(session, manifest, "pytest", "受限恢复演练")
        assert result.status == "restricted_access_confirmation_required"
        with SessionLocal() as session:
            assert session.get(SessionRecord, session_id).revoked_at is not None
            assert session.get(OneTimeCredential, credential_id).status == "revoked"
            assert session.get(MailOutbox, outbox_id).status == "suppressed"
            assert not api_requests_allowed(session)
            assert not dispatcher_allowed(session)
            session.rollback()
        with SessionLocal() as session:
            result = confirm_restricted_access(
                session, manifest, "pytest", "负责人已完成脱机复核"
            )
        assert result.status == "api_open"
        monkeypatch.setattr(settings, "recovery_deployment_frozen", False)
        with SessionLocal() as session:
            assert api_requests_allowed(session)
            assert not dispatcher_allowed(session)
            session.rollback()
        with SessionLocal() as session:
            result = open_dispatcher(
                session, "restricted-001", "pytest", "API 在线核验通过"
            )
        assert result.status == "open"
        monkeypatch.setattr(settings, "recovery_deployment_frozen", True)
        next_manifest = load_manifest(
            _manifest(tmp_path / "next-manifest.json", "restricted-002")
        )
        with SessionLocal() as session:
            next_result = recover(session, next_manifest, "pytest", "下一轮恢复演练")
        assert next_result.status == "restricted_access_confirmation_required"
        with SessionLocal() as session:
            assert session.get(RecoveryState, 1).recovery_id == "restricted-002"
            session.rollback()
    finally:
        monkeypatch.setattr(settings, "recovery_deployment_frozen", False)
        _restore_open_state()
        _reset()


def test_restricted_recovery_rolls_back_when_convergence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    try:
        manifest = load_manifest(_manifest(tmp_path / "manifest.json", "atomic-001"))
        monkeypatch.setattr(settings, "recovery_deployment_frozen", True)

        def fail_convergence(_: object) -> int:
            raise RecoveryFailed("restricted", {})

        monkeypatch.setattr(
            recovery_service, "suppress_for_restricted_recovery", fail_convergence
        )
        with SessionLocal() as session, pytest.raises(RecoveryFailed):
            recover(session, manifest, "pytest", "原子收敛演练")
        with SessionLocal() as session:
            state = session.get(RecoveryState, 1)
            assert state is not None
            assert state.stage == "open"
            assert state.recovery_id is None
            session.rollback()
    finally:
        monkeypatch.setattr(settings, "recovery_deployment_frozen", False)
        _restore_open_state()
        _reset()


def test_verification_failure_keeps_api_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    try:
        manifest = load_manifest(
            _manifest(tmp_path / "manifest.json", "mismatch-001", wrong_size=True)
        )
        monkeypatch.setattr(settings, "recovery_deployment_frozen", True)
        with SessionLocal() as session:
            recover(session, manifest, "pytest", "对象不一致演练")
        with SessionLocal() as session, pytest.raises(RecoveryFailed) as error:
            confirm_restricted_access(
                session, manifest, "pytest", "负责人已完成脱机复核"
            )
        assert error.value.phase == "objects"
        with SessionLocal() as session:
            assert session.get(RecoveryState, 1).stage == "recovering"
            assert not api_requests_allowed(session)
            session.rollback()
    finally:
        monkeypatch.setattr(settings, "recovery_deployment_frozen", False)
        _restore_open_state()
        _reset()


def test_verification_detects_participant_and_feedback_relationship_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    try:
        migrator_engine = create_engine(settings.migrator_database_url)
        with migrator_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE playtest_sessions NO FORCE ROW LEVEL SECURITY")
            )
            connection.execute(
                text(
                    "ALTER TABLE playtest_feedback_answers NO FORCE ROW LEVEL SECURITY"
                )
            )
            try:
                connection.execute(
                    text(
                        "UPDATE playtest_sessions SET actual_headcount = 0 "
                        "WHERE actual_headcount IS NOT NULL"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE playtest_feedback_answers SET source = 'oral_discussion', "
                        "direct_author_account_id = NULL WHERE submission_id = ("
                        "SELECT id FROM playtest_feedback_submissions "
                        "WHERE source = 'direct' LIMIT 1)"
                    )
                )
            finally:
                connection.execute(
                    text(
                        "ALTER TABLE playtest_feedback_answers FORCE ROW LEVEL SECURITY"
                    )
                )
                connection.execute(
                    text("ALTER TABLE playtest_sessions FORCE ROW LEVEL SECURITY")
                )
        migrator_engine.dispose()
        manifest = load_manifest(
            _manifest(tmp_path / "manifest.json", "relationships-001")
        )
        monkeypatch.setattr(settings, "recovery_deployment_frozen", True)
        with SessionLocal() as session:
            recover(session, manifest, "pytest", "关系完整性演练")
        with SessionLocal() as session, pytest.raises(RecoveryFailed) as error:
            confirm_restricted_access(
                session, manifest, "pytest", "负责人已完成脱机复核"
            )
        assert error.value.phase == "verification"
        assert error.value.counts["actual_participants"] > 0
        assert error.value.counts["feedback_answers"] > 0
    finally:
        monkeypatch.setattr(settings, "recovery_deployment_frozen", False)
        _restore_open_state()
        _reset()


def test_recovery_verifies_expired_workspace_exit_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    try:
        migrator_engine = create_engine(settings.migrator_database_url)
        with migrator_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY")
            )
            try:
                connection.execute(
                    text(
                        "UPDATE workspaces SET "
                        "exit_requested_at = CURRENT_TIMESTAMP - interval '2 seconds', "
                        "exit_read_until = CURRENT_TIMESTAMP - interval '1 second', "
                        "exit_requested_by_account_id = owner_account_id, "
                        "exit_operation_key = 'recovery-scope-test'"
                    )
                )
            finally:
                connection.execute(
                    text("ALTER TABLE workspaces FORCE ROW LEVEL SECURITY")
                )
        migrator_engine.dispose()
        manifest = load_manifest(
            _manifest(tmp_path / "manifest.json", "exit-scope-001")
        )
        monkeypatch.setattr(settings, "recovery_deployment_frozen", True)
        with SessionLocal() as session:
            recover(session, manifest, "pytest", "退出读取守卫演练")
        with SessionLocal() as session:
            result = confirm_restricted_access(
                session, manifest, "pytest", "负责人已完成脱机复核"
            )
        assert result.status == "api_open"
    finally:
        restoration_engine = create_engine(settings.migrator_database_url)
        with restoration_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY")
            )
            connection.execute(
                text(
                    "UPDATE workspaces SET exit_requested_at = NULL, "
                    "exit_read_until = NULL, exit_requested_by_account_id = NULL, "
                    "exit_operation_key = NULL"
                )
            )
            connection.execute(text("ALTER TABLE workspaces FORCE ROW LEVEL SECURITY"))
        restoration_engine.dispose()
        monkeypatch.setattr(settings, "recovery_deployment_frozen", False)
        _restore_open_state()
        _reset()


def test_smtp_start_stops_after_recovery_begins() -> None:
    _reset()
    try:
        with SessionLocal() as session:
            session.execute(
                update(MailOutbox)
                .where(MailOutbox.status == "pending")
                .values(status="suppressed")
            )
            session.commit()
        _, _, outbox_id = _provision_active_credential_and_session()
        with SessionLocal() as session:
            claim = dispatcher._claim_next(session)
        assert claim is not None and claim.outbox_id == outbox_id
        with SessionLocal() as session:
            set_recovery_maintenance_scope(session)
            state = session.get(RecoveryState, 1)
            assert state is not None
            state.stage = "recovering"
            session.commit()
        with SessionLocal() as session:
            assert not dispatcher._start_smtp(session, claim)
            assert session.get(MailOutbox, outbox_id).smtp_started_at is None
            session.rollback()
    finally:
        _restore_open_state()
        _reset()
