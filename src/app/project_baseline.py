from __future__ import annotations

import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import set_actor, set_project_baseline_scope
from app.audit.models import SecurityAudit
from app.core.config import settings
from app.core.database import engine
from app.files import service as files_service
from app.files import storage
from app.files.models import StoredFile
from app.identity import service as identity_service
from app.identity.models import (
    Account,
    AttemptRecord,
    OneTimeCredential,
    PasswordCredential,
    RecoveryRequestJob,
    SessionRecord,
)
from app.notifications import dispatcher as mail_dispatcher
from app.notifications.models import MailOutbox
from app.playtests import service as playtests_service
from app.playtests.models import (
    PlaytestPlan,
    PlaytestSession,
    PlaytestSessionMaterial,
    PlaytestSessionParticipant,
)
from app.works import service as works_service
from app.works.models import Work, WorkMaterialFile
from app.workspaces import service as workspaces_service
from app.workspaces.models import (
    WorkAccess,
    Workspace,
    WorkspaceInvitation,
    WorkspaceInvitationAttempt,
    WorkspaceMember,
)


@dataclass(frozen=True)
class BaselineAccount:
    email: str
    role: str


@dataclass(frozen=True)
class BaselineResult:
    status: str
    phase: str
    counts: dict[str, int]
    confirmation_target: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "phase": self.phase,
            "counts": self.counts,
        }
        if self.confirmation_target:
            result["confirmation_target"] = self.confirmation_target
        return result


class BaselineOperationFailed(Exception):
    def __init__(self, phase: str, counts: dict[str, int]) -> None:
        self.phase = phase
        self.counts = counts


BASELINE_ACCOUNTS = (
    BaselineAccount("studio-owner@example.com", "maintainer"),
    BaselineAccount("session-organizer@example.com", "organizer"),
    BaselineAccount("rules-collaborator@example.com", "collaborator"),
    BaselineAccount("playtest-guest@example.com", "playtester"),
)
BASELINE_SCOPE = "project_baseline"
BASELINE_IMAGE = Path(__file__).with_name("baseline-work.jpg")
BASELINE_RULES = Path(__file__).with_name("baseline-rules.pdf")
BASELINE_PLAYER_AID = Path(__file__).with_name("baseline-player-aid.pdf")
PROJECT_BASELINE_LOCK_KEY = 4_580_051


def reset_confirmation() -> str:
    return f"{settings.db_name}:{settings.s3_bucket_name}"


@contextmanager
def maintenance_lock() -> Iterator[None]:
    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": PROJECT_BASELINE_LOCK_KEY}
        )
        connection.commit()
        try:
            # ponytail: 受控开发环境需先停止 API/派发；在线重置时改为正式维护门禁。
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": PROJECT_BASELINE_LOCK_KEY},
            )
            connection.commit()


def _require_maintenance_details(operator: str, reason: str) -> tuple[str, str]:
    operator = operator.strip()
    reason = reason.strip()
    if not operator or not reason:
        raise BaselineOperationFailed("request", {})
    return operator, reason


def _commit(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _record_count(session: Session) -> int:
    set_project_baseline_scope(session)
    models = (
        Account,
        PasswordCredential,
        SessionRecord,
        OneTimeCredential,
        AttemptRecord,
        RecoveryRequestJob,
        MailOutbox,
        SecurityAudit,
        Workspace,
        WorkspaceMember,
        WorkspaceInvitation,
        WorkspaceInvitationAttempt,
        Work,
        WorkAccess,
        WorkMaterialFile,
        PlaytestPlan,
        PlaytestSession,
        PlaytestSessionMaterial,
        PlaytestSessionParticipant,
        StoredFile,
    )
    return sum(
        session.scalar(select(func.count()).select_from(model)) or 0 for model in models
    )


def _preflight(session: Session) -> tuple[list[str], int]:
    try:
        object_keys = storage.list_object_keys()
        records = _record_count(session)
    except (SQLAlchemyError, storage.StorageUnavailable) as error:
        session.rollback()
        raise BaselineOperationFailed("precheck", {}) from error
    session.rollback()
    return object_keys, records


def _baseline_counts() -> dict[str, int]:
    return {
        "accounts": 4,
        "workspaces": 1,
        "works": 1,
        "accesses": 3,
        "images": 1,
        "materials": 2,
        "playtest_plans": 1,
        "playtest_sessions": 2,
        "playtest_confirmations": 1,
    }


def initialize(session: Session, operator: str, reason: str) -> BaselineResult:
    operator, reason = _require_maintenance_details(operator, reason)
    object_keys, records = _preflight(session)
    if records or object_keys:
        return BaselineResult(
            "not_initialized",
            "precheck",
            {"records": records, "objects": len(object_keys)},
        )

    password = settings.baseline_password.get_secret_value()
    if not password or not all(
        path.is_file() for path in (BASELINE_IMAGE, BASELINE_RULES, BASELINE_PLAYER_AID)
    ):
        raise BaselineOperationFailed("configuration", {})

    counts: dict[str, int] = {}
    phase = "accounts"
    try:
        accounts = [
            identity_service.create_active_baseline_account(
                session, account.email, password
            )
            for account in BASELINE_ACCOUNTS
        ]
        session.add(
            SecurityAudit(
                action="project_baseline_accounts_created",
                operator=operator,
                reason=reason,
                scope=BASELINE_SCOPE,
            )
        )
        _commit(session)
        counts["accounts"] = len(accounts)

        phase = "workspace"
        owner = accounts[0]
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session,
            owner.id,
            "雾林桌游工作室",
            "围绕实体桌游原型持续记录规则、试玩与调整。",
        )

        phase = "members"
        set_actor(session, owner.id)
        for account in accounts[1:3]:
            workspaces_service.add_baseline_member(session, workspace.id, account.id)
        _commit(session)
        counts["workspaces"] = 1

        phase = "work"
        set_actor(session, owner.id)
        work = works_service.create_work(
            session,
            owner.id,
            workspace.id,
            name="雾林棋局",
            description="一款围绕路径规划与有限信息协作展开的双至四人桌游原型。",
            creative_stage="原型测试",
            target_experience="在有限线索中共同推理，并为每次选择承担后果。",
            min_players=2,
            max_players=4,
            estimated_duration_minutes=60,
        )
        counts["works"] = 1
        counts["accesses"] = 1

        phase = "access"
        for account, baseline_account in zip(
            accounts[1:3], BASELINE_ACCOUNTS[1:3], strict=True
        ):
            set_actor(session, owner.id)
            works_service.set_work_access(
                session,
                owner.id,
                workspace.id,
                work.id,
                account.id,
                baseline_account.role,
            )
            counts["accesses"] += 1

        phase = "image"
        with BASELINE_IMAGE.open("rb") as source:
            files_service.upload_image(
                session,
                owner.id,
                workspace.id,
                work.id,
                source,
                "雾林棋局.jpg",
                "image/jpeg",
            )

        phase = "rule_materials"
        materials = []
        for source_path, display_name in (
            (BASELINE_RULES, "雾林棋局当前规则书.pdf"),
            (BASELINE_PLAYER_AID, "雾林棋局打印辅助页.pdf"),
        ):
            with source_path.open("rb") as source:
                materials.append(
                    files_service.upload_material(
                        session,
                        owner.id,
                        workspace.id,
                        work.id,
                        source,
                        display_name,
                        "application/pdf",
                    )
                )
        works_service.update_rule_materials(
            session,
            owner.id,
            workspace.id,
            work.id,
            rule_name="雾林棋局当前规则",
            rule_description="供下一次试玩使用的基础规则与打印辅助页。",
            rule_content=(
                "两至四名玩家共同穿过雾林，在路径封闭前找到三枚线索并回到营地。"
                "每回合选择探索、协助或记录。"
            ),
            material_file_ids=[material.id for material in materials],
            expected_revision=work.revision,
        )

        phase = "playtests"
        playtester = accounts[3]
        plan = playtests_service.create_plan(
            session,
            owner.id,
            workspace.id,
            work.id,
            "观察玩家在信息不足时是否会主动协作并记录线索。",
            "由组织者记录每轮决策和出现的讨论。",
            (
                playtests_service.SessionDraft(
                    scheduled_at=datetime.now(UTC) + timedelta(days=2),
                    location="工作室试玩桌 A",
                    capacity=4,
                    material_file_ids=tuple(material.id for material in materials),
                    participant_emails=(playtester.email,),
                ),
                playtests_service.SessionDraft(
                    scheduled_at=datetime.now(UTC) + timedelta(days=5),
                    location="工作室试玩桌 B",
                    capacity=4,
                    material_file_ids=(materials[0].id,),
                    participant_emails=(playtester.email,),
                ),
            ),
        )
        playtests_service.confirm_participation(
            session, playtester.id, plan.sessions[0].id
        )
        mail_dispatcher.dispatch_one(session)

        phase = "audit"
        session.add(
            SecurityAudit(
                action="project_baseline_initialized",
                operator=operator,
                reason=reason,
                scope=BASELINE_SCOPE,
            )
        )
        _commit(session)
        counts = _baseline_counts()
    except (OSError, SQLAlchemyError, files_service.FileOperationRetryable) as error:
        session.rollback()
        raise BaselineOperationFailed(phase, counts) from error
    except Exception as error:
        session.rollback()
        raise BaselineOperationFailed(phase, counts) from error

    return BaselineResult("initialized", "complete", counts)


def _clear_database(session: Session) -> int:
    set_project_baseline_scope(session)
    models = (
        PlaytestSessionMaterial,
        PlaytestSessionParticipant,
        PlaytestSession,
        PlaytestPlan,
        WorkMaterialFile,
        StoredFile,
        WorkAccess,
        Work,
        WorkspaceInvitation,
        WorkspaceMember,
        WorkspaceInvitationAttempt,
        Workspace,
        MailOutbox,
        OneTimeCredential,
        SessionRecord,
        PasswordCredential,
        AttemptRecord,
        RecoveryRequestJob,
        SecurityAudit,
        Account,
    )
    deleted = 0
    try:
        for model in models:
            deleted += session.execute(delete(model)).rowcount or 0
        _commit(session)
    except SQLAlchemyError as error:
        session.rollback()
        raise BaselineOperationFailed("database_cleanup", {}) from error
    return deleted


def reset(
    session: Session, operator: str, reason: str, confirmation: str | None
) -> BaselineResult:
    operator, reason = _require_maintenance_details(operator, reason)
    if confirmation is None or not hmac.compare_digest(
        confirmation, reset_confirmation()
    ):
        return BaselineResult(
            "confirmation_required", "precheck", {}, reset_confirmation()
        )

    object_keys, records = _preflight(session)
    try:
        deleted_objects = storage.delete_objects(object_keys)
    except storage.StorageUnavailable as error:
        raise BaselineOperationFailed(
            "object_cleanup", {"records": 0, "objects": error.deleted}
        ) from error

    try:
        _clear_database(session)
    except BaselineOperationFailed as error:
        raise BaselineOperationFailed(
            error.phase, {"records": 0, "objects": deleted_objects}
        ) from error
    result = initialize(session, operator, reason)
    if result.status != "initialized":
        raise BaselineOperationFailed("initialize", result.counts)

    try:
        session.add(
            SecurityAudit(
                action="project_baseline_reset",
                operator=operator,
                reason=reason,
                scope=BASELINE_SCOPE,
            )
        )
        _commit(session)
    except SQLAlchemyError as error:
        session.rollback()
        raise BaselineOperationFailed("audit", result.counts) from error
    return BaselineResult("reset", "complete", result.counts)
