import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import set_recovery_maintenance_scope
from app.audit.models import SecurityAudit
from app.core.config import settings
from app.core.database import engine
from app.files import storage
from app.files.models import StoredFile
from app.identity.service import revoke_for_restricted_recovery
from app.notifications.service import suppress_for_restricted_recovery
from app.recovery.models import RecoveryState

RECOVERY_LOCK_KEY = 4_580_015
RECOVERY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True)
class ManifestObject:
    key: str
    size_bytes: int
    sha256: bytes


@dataclass(frozen=True)
class RecoveryManifest:
    recovery_id: str
    target_at: datetime
    archive_sha256: bytes
    raw_sha256: bytes
    objects: tuple[ManifestObject, ...]


@dataclass(frozen=True)
class RecoveryResult:
    status: str
    phase: str
    counts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status, "phase": self.phase, "counts": self.counts}


@dataclass
class RecoveryFailed(Exception):
    phase: str
    counts: dict[str, int]


def _now() -> datetime:
    return datetime.now(UTC)


def _scope(recovery_id: str, authorization_mode: str | None = None) -> str:
    value = f"recovery:{recovery_id}"
    return f"{value};mode:{authorization_mode}" if authorization_mode else value


def _require_operator_and_reason(operator: str, reason: str) -> tuple[str, str]:
    operator, reason = operator.strip(), reason.strip()
    if not operator or len(operator) > 128 or not reason or len(reason) > 256:
        raise RecoveryFailed("request", {})
    return operator, reason


def _require_recovery_id(recovery_id: str) -> str:
    if not RECOVERY_ID_PATTERN.fullmatch(recovery_id):
        raise RecoveryFailed("request", {})
    return recovery_id


def _parse_sha256(value: object) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise RecoveryFailed("manifest", {})
    return bytes.fromhex(value)


def _object_digest(objects: tuple[ManifestObject, ...]) -> bytes:
    digest = hashlib.sha256()
    for item in sorted(objects, key=lambda value: value.key):
        digest.update(item.key.encode())
        digest.update(b"\0")
        digest.update(str(item.size_bytes).encode())
        digest.update(b"\0")
        digest.update(item.sha256.hex().encode())
        digest.update(b"\n")
    return digest.digest()


def load_manifest(path: Path) -> RecoveryManifest:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryFailed("manifest", {}) from error
    if not isinstance(value, dict) or value.get("formatVersion") != 1:
        raise RecoveryFailed("manifest", {})
    recovery_id = value.get("recoveryId")
    if not isinstance(recovery_id, str):
        raise RecoveryFailed("manifest", {})
    _require_recovery_id(recovery_id)
    target = value.get("target")
    if not isinstance(target, dict) or target.get("database") != settings.db_name:
        raise RecoveryFailed("manifest", {})
    if target.get("bucket") != settings.s3_bucket_name:
        raise RecoveryFailed("manifest", {})
    target_at = value.get("targetTime")
    try:
        parsed_target_at = datetime.fromisoformat(str(target_at).replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryFailed("manifest", {}) from error
    if parsed_target_at.tzinfo is None:
        raise RecoveryFailed("manifest", {})
    raw_objects = value.get("objects")
    if not isinstance(raw_objects, list):
        raise RecoveryFailed("manifest", {})
    objects: list[ManifestObject] = []
    keys: set[str] = set()
    for item in raw_objects:
        if not isinstance(item, dict):
            raise RecoveryFailed("manifest", {})
        key, size = item.get("key"), item.get("sizeBytes")
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 255
            or key in keys
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise RecoveryFailed("manifest", {})
        keys.add(key)
        objects.append(ManifestObject(key, size, _parse_sha256(item.get("sha256"))))
    manifest = RecoveryManifest(
        recovery_id=recovery_id,
        target_at=parsed_target_at,
        archive_sha256=_parse_sha256(value.get("archiveSha256")),
        raw_sha256=hashlib.sha256(raw).digest(),
        objects=tuple(objects),
    )
    if _object_digest(manifest.objects) != _parse_sha256(value.get("objectsSha256")):
        raise RecoveryFailed("manifest", {})
    return manifest


def _state(session: Session) -> RecoveryState:
    state = session.scalar(
        select(RecoveryState).where(RecoveryState.id == 1).with_for_update()
    )
    if state is None:
        raise RecoveryFailed("state", {})
    return state


def _audit(
    session: Session,
    action: str,
    operator: str,
    reason: str,
    recovery_id: str,
    authorization_mode: str | None = None,
) -> None:
    session.add(
        SecurityAudit(
            action=action,
            operator=operator,
            reason=reason,
            scope=_scope(recovery_id, authorization_mode),
        )
    )


def _commit(session: Session, phase: str, counts: dict[str, int]) -> None:
    try:
        session.commit()
    except SQLAlchemyError as error:
        session.rollback()
        raise RecoveryFailed(phase, counts) from error


def _begin_recovery(
    session: Session,
    manifest: RecoveryManifest,
    operator: str,
    reason: str,
) -> RecoveryState:
    set_recovery_maintenance_scope(session)
    state = _state(session)
    if state.stage == "api_open":
        raise RecoveryFailed("state", {})
    if state.stage == "recovering":
        if (
            state.recovery_id != manifest.recovery_id
            or state.manifest_sha256 != manifest.raw_sha256
            or state.authorization_mode != "restricted"
        ):
            raise RecoveryFailed("state", {})
        return state
    if state.stage != "open" or state.recovery_id == manifest.recovery_id:
        raise RecoveryFailed("state", {})
    state.stage = "recovering"
    state.recovery_id = manifest.recovery_id
    state.manifest_sha256 = manifest.raw_sha256
    state.recovery_target_at = manifest.target_at
    state.authorization_mode = "restricted"
    state.restricted_access_confirmed_at = None
    state.verified_at = None
    state.operator = operator
    _audit(
        session,
        "recovery_started",
        operator,
        reason,
        manifest.recovery_id,
        "restricted",
    )
    return state


def _restrict(
    session: Session, state: RecoveryState, operator: str, reason: str
) -> dict[str, int]:
    set_recovery_maintenance_scope(session)
    current = _state(session)
    if current.stage != "recovering" or current.recovery_id != state.recovery_id:
        raise RecoveryFailed("state", {})
    revoked_sessions, revoked_credentials = revoke_for_restricted_recovery(session)
    suppressed_outbox = suppress_for_restricted_recovery(session)
    counts = {
        "sessions": revoked_sessions,
        "credentials": len(revoked_credentials),
        "outbox": suppressed_outbox,
    }
    _audit(
        session,
        "recovery_restricted_converged",
        operator,
        reason,
        current.recovery_id,
        "restricted",
    )
    _commit(session, "restricted", counts)
    return counts


def _database_checks(session: Session) -> dict[str, int]:
    checks = {
        "schema": text("SELECT NOT public.recovery_schema_current()"),
        "runtime_permissions": text(
            "SELECT NOT ("
            "NOT has_database_privilege(current_database(), 'CREATE') "
            "AND NOT has_schema_privilege('public', 'CREATE') "
            "AND NOT has_table_privilege('public.alembic_version', 'SELECT') "
            "AND NOT (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
            ")"
        ),
        "workspace_owners": text(
            "SELECT count(*) FROM workspaces w LEFT JOIN workspace_members m "
            "ON m.workspace_id = w.id AND m.account_id = w.owner_account_id "
            "WHERE m.id IS NULL"
        ),
        "work_accesses": text(
            "SELECT count(*) FROM work_accesses a LEFT JOIN workspace_members m "
            "ON m.workspace_id = a.workspace_id AND m.account_id = a.account_id "
            "WHERE m.id IS NULL"
        ),
        "current_materials": text(
            "SELECT count(*) FROM work_material_files m LEFT JOIN files f ON f.id = m.file_id "
            "WHERE f.id IS NULL OR f.status <> 'ready'"
        ),
        "session_materials": text(
            "SELECT count(*) FROM playtest_session_materials m LEFT JOIN files f ON f.id = m.file_id "
            "WHERE f.id IS NULL OR f.status <> 'ready' OR m.sha256 <> f.sha256"
        ),
        "actual_materials": text(
            "SELECT count(*) FROM playtest_session_actual_materials m LEFT JOIN files f ON f.id = m.file_id "
            "WHERE f.id IS NULL OR f.status <> 'ready' OR m.sha256 <> f.sha256"
        ),
        "actual_participants": text(
            "SELECT count(*) FROM playtest_sessions s LEFT JOIN ("
            "SELECT session_id, count(*) AS count FROM playtest_session_actual_participants "
            "GROUP BY session_id"
            ") p ON p.session_id = s.id "
            "WHERE s.actual_headcount IS NOT NULL "
            "AND coalesce(p.count, 0) > s.actual_headcount"
        ),
        "feedback_recorders": text(
            "SELECT count(*) FROM playtest_feedback_submissions f "
            "JOIN playtest_sessions s ON s.id = f.session_id "
            "LEFT JOIN work_accesses a ON a.workspace_id = s.workspace_id "
            "AND a.work_id = s.work_id AND a.account_id = f.recorded_by_account_id "
            "AND a.role IN ('maintainer', 'organizer') "
            "WHERE f.source <> 'direct' AND a.account_id IS NULL"
        ),
        "feedback_answers": text(
            "SELECT count(*) FROM playtest_feedback_answers a "
            "JOIN playtest_feedback_submissions f ON f.id = a.submission_id "
            "AND f.session_id = a.session_id "
            "WHERE a.source IS DISTINCT FROM f.source "
            "OR a.status IS DISTINCT FROM f.status "
            "OR a.direct_author_account_id IS DISTINCT FROM f.direct_author_account_id "
            "OR a.recorded_by_account_id IS DISTINCT FROM f.recorded_by_account_id"
        ),
        "feedback_authors": text(
            "SELECT count(*) FROM playtest_feedback_submissions f "
            "LEFT JOIN playtest_session_participants p ON p.session_id = f.session_id "
            "AND p.account_id = f.direct_author_account_id "
            "WHERE f.source = 'direct' AND p.id IS NULL"
        ),
        "issue_retests": text(
            "SELECT count(*) FROM issue_retest_links l JOIN issues i ON i.id = l.issue_id "
            "JOIN playtest_sessions s ON s.id = l.session_id "
            "WHERE i.workspace_id <> s.workspace_id OR i.work_id <> s.work_id"
        ),
        "issue_evidence": text(
            "SELECT count(*) FROM issue_evidence_links l JOIN issues i ON i.id = l.issue_id "
            "LEFT JOIN playtest_observations o ON o.id = l.observation_id "
            "LEFT JOIN playtest_sessions os ON os.id = o.session_id "
            "LEFT JOIN playtest_feedback_submissions f ON f.id = l.feedback_submission_id "
            "LEFT JOIN playtest_sessions fs ON fs.id = f.session_id "
            "WHERE (o.id IS NOT NULL AND (os.workspace_id <> i.workspace_id OR os.work_id <> i.work_id)) "
            "OR (f.id IS NOT NULL AND (fs.workspace_id <> i.workspace_id OR fs.work_id <> i.work_id))"
        ),
    }
    return {
        name: int(session.scalar(statement) or 0) for name, statement in checks.items()
    }


def _object_checks(session: Session, manifest: RecoveryManifest) -> dict[str, int]:
    files = list(
        session.scalars(select(StoredFile).where(StoredFile.status == "ready"))
    )
    expected = {item.key: item for item in manifest.objects}
    database = {item.object_key: item for item in files}
    try:
        bucket_keys = set(storage.list_object_keys())
    except storage.StorageUnavailable as error:
        raise RecoveryFailed(
            "objects", {"files": len(database), "objects": 0}
        ) from error
    counts = {"files": len(database), "objects": len(bucket_keys)}
    if set(expected) != set(database) or set(expected) != bucket_keys:
        raise RecoveryFailed("objects", counts)
    for key, expected_object in expected.items():
        record = database[key]
        if (
            record.size_bytes != expected_object.size_bytes
            or record.sha256 != expected_object.sha256
        ):
            raise RecoveryFailed("objects", counts)
        try:
            source = storage.open_object(key)
        except storage.StorageUnavailable as error:
            raise RecoveryFailed("objects", counts) from error
        if source is None:
            raise RecoveryFailed("objects", counts)
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
                size += len(chunk)
        finally:
            source.close()
        if (
            size != expected_object.size_bytes
            or digest.digest() != expected_object.sha256
        ):
            raise RecoveryFailed("objects", counts)
    return counts


def _verify_and_open(
    session: Session,
    manifest: RecoveryManifest,
    operator: str,
    reason: str,
) -> RecoveryResult:
    try:
        set_recovery_maintenance_scope(session)
        state = _state(session)
        if (
            state.stage != "recovering"
            or state.recovery_id != manifest.recovery_id
            or state.manifest_sha256 != manifest.raw_sha256
            or state.authorization_mode is None
            or (
                state.authorization_mode == "restricted"
                and state.restricted_access_confirmed_at is None
            )
        ):
            raise RecoveryFailed("state", {})
        database_counts = _database_checks(session)
        if any(database_counts.values()):
            raise RecoveryFailed("verification", database_counts)
        object_counts = _object_checks(session, manifest)
        state.stage = "api_open"
        state.verified_at = _now()
        _audit(
            session,
            "recovery_integrity_verified",
            operator,
            reason,
            manifest.recovery_id,
            state.authorization_mode,
        )
        _audit(
            session,
            "recovery_api_ready",
            operator,
            reason,
            manifest.recovery_id,
            state.authorization_mode,
        )
        _commit(session, "verification", object_counts)
        return RecoveryResult("api_open", "complete", object_counts)
    except RecoveryFailed as error:
        session.rollback()
        _record_failure(session, manifest.recovery_id, operator, reason)
        raise error
    except Exception as error:
        session.rollback()
        _record_failure(session, manifest.recovery_id, operator, reason)
        raise RecoveryFailed("verification", {}) from error


def _record_failure(
    session: Session, recovery_id: str, operator: str, reason: str
) -> None:
    try:
        set_recovery_maintenance_scope(session)
        state = _state(session)
        if state.recovery_id == recovery_id:
            state.stage = "recovering"
            _audit(
                session,
                "recovery_verification_failed",
                operator,
                reason,
                recovery_id,
                state.authorization_mode,
            )
        session.commit()
    except SQLAlchemyError:
        session.rollback()


def recover(
    session: Session,
    manifest: RecoveryManifest,
    operator: str,
    reason: str,
) -> RecoveryResult:
    if not settings.recovery_deployment_frozen:
        raise RecoveryFailed("deployment_freeze", {})
    _require_recovery_id(manifest.recovery_id)
    operator, reason = _require_operator_and_reason(operator, reason)
    try:
        state = _begin_recovery(session, manifest, operator, reason)
        counts = _restrict(session, state, operator, reason)
    except RecoveryFailed:
        session.rollback()
        raise
    return RecoveryResult(
        "restricted_access_confirmation_required", "recovering", counts
    )


def confirm_restricted_access(
    session: Session,
    manifest: RecoveryManifest,
    operator: str,
    reason: str,
) -> RecoveryResult:
    if not settings.recovery_deployment_frozen:
        raise RecoveryFailed("deployment_freeze", {})
    recovery_id = _require_recovery_id(manifest.recovery_id)
    operator, reason = _require_operator_and_reason(operator, reason)
    try:
        set_recovery_maintenance_scope(session)
        state = _state(session)
        if (
            state.stage != "recovering"
            or state.recovery_id != recovery_id
            or state.manifest_sha256 != manifest.raw_sha256
            or state.authorization_mode != "restricted"
        ):
            raise RecoveryFailed("state", {})
        if state.restricted_access_confirmed_at is None:
            state.restricted_access_confirmed_at = _now()
            _audit(
                session,
                "recovery_restricted_access_confirmed",
                operator,
                reason,
                recovery_id,
                "restricted",
            )
            _commit(session, "authorization", {})
        else:
            session.rollback()
    except RecoveryFailed:
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise RecoveryFailed("authorization", {}) from error
    return _verify_and_open(session, manifest, operator, reason)


def open_dispatcher(
    session: Session, recovery_id: str, operator: str, reason: str
) -> RecoveryResult:
    if settings.recovery_deployment_frozen:
        raise RecoveryFailed("deployment_freeze", {})
    recovery_id = _require_recovery_id(recovery_id)
    operator, reason = _require_operator_and_reason(operator, reason)
    try:
        set_recovery_maintenance_scope(session)
        state = _state(session)
        if state.stage != "api_open" or state.recovery_id != recovery_id:
            raise RecoveryFailed("state", {})
        state.stage = "open"
        _audit(
            session,
            "recovery_dispatcher_opened",
            operator,
            reason,
            recovery_id,
            state.authorization_mode,
        )
        _commit(session, "dispatcher", {})
        return RecoveryResult("open", "complete", {})
    except RecoveryFailed:
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise RecoveryFailed("dispatcher", {}) from error


@contextmanager
def locked_recovery_session() -> Iterator[Session]:
    with engine.connect() as connection:
        # ponytail: 单个恢复锁覆盖低频受控操作；并行恢复需求出现时按隔离目标拆分锁。
        connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": RECOVERY_LOCK_KEY}
        )
        connection.commit()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            session.close()
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": RECOVERY_LOCK_KEY}
            )
            connection.commit()
