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
from app.evidence import service as evidence_service
from app.evidence.models import (
    PlaytestFeedbackAnswer,
    PlaytestFeedbackItem,
    PlaytestFeedbackOption,
    PlaytestFeedbackSubmission,
    PlaytestObservation,
)
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
from app.issues import service as issues_service
from app.issues.models import Issue, IssueEvidenceLink
from app.notifications import dispatcher as mail_dispatcher
from app.notifications.models import MailOutbox
from app.playtests import service as playtests_service
from app.playtests.models import (
    PlaytestPlan,
    PlaytestSession,
    PlaytestSessionActualMaterial,
    PlaytestSessionActualParticipant,
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
        PlaytestSessionActualMaterial,
        PlaytestSessionActualParticipant,
        PlaytestSessionMaterial,
        PlaytestSessionParticipant,
        PlaytestFeedbackAnswer,
        PlaytestFeedbackSubmission,
        PlaytestFeedbackOption,
        PlaytestFeedbackItem,
        PlaytestObservation,
        IssueEvidenceLink,
        Issue,
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
        "playtest_actual_participants": 2,
        "playtest_observations": 3,
        "playtest_feedback_items": 3,
        "playtest_feedback_submissions": 2,
        "playtest_feedback_answers": 4,
        "issues": 3,
        "issue_evidence_links": 5,
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

        first_started = playtests_service.start_session(
            session,
            owner.id,
            workspace.id,
            work.id,
            plan.sessions[0].id,
            plan.sessions[0].revision,
        )
        first_result = playtests_service.save_result(
            session,
            owner.id,
            workspace.id,
            work.id,
            first_started.id,
            first_started.revision,
            playtests_service.ResultDraft(
                actual_headcount=1,
                actual_duration_minutes=55,
                completion_status="completed",
                actual_material=playtests_service.ActualMaterialDraft(
                    rule_name=first_started.rule_name,
                    rule_description=first_started.rule_description,
                    rule_content=first_started.rule_content,
                    material_file_ids=tuple(
                        material.id for material in first_started.materials
                    ),
                    change_reason=None,
                ),
                actual_participants=(
                    playtests_service.ActualParticipantDraft(
                        planned_account_id=playtester.id,
                        temporary_code=None,
                        seat_or_faction="向导",
                        score_or_outcome="完成返回",
                    ),
                ),
            ),
        )
        first_fact = playtests_service.create_observation(
            session,
            owner.id,
            workspace.id,
            work.id,
            first_started.id,
            first_result.session.revision,
            "fact",
            "玩家在第三回合前主动分工记录线索。",
        )
        first_interpretation = playtests_service.create_observation(
            session,
            owner.id,
            workspace.id,
            work.id,
            first_started.id,
            first_fact.revision,
            "organizer_interpretation",
            "分工出现得早，现有提示已经足以引导协作。",
        )
        feedback_items = (
            playtests_service.create_feedback_item(
                session,
                owner.id,
                workspace.id,
                work.id,
                first_started.id,
                "baseline-feedback-item-open",
                evidence_service.FeedbackItemDraft(
                    kind="short_text",
                    question="哪一段规则最需要进一步说明？",
                    options=(),
                ),
            ),
            playtests_service.create_feedback_item(
                session,
                owner.id,
                workspace.id,
                work.id,
                first_started.id,
                "baseline-feedback-item-choice",
                evidence_service.FeedbackItemDraft(
                    kind="single_choice",
                    question="本场协作节奏如何？",
                    options=("过慢", "合适", "过快"),
                ),
            ),
            playtests_service.create_feedback_item(
                session,
                owner.id,
                workspace.id,
                work.id,
                first_started.id,
                "baseline-feedback-item-number",
                evidence_service.FeedbackItemDraft(
                    kind="number",
                    question="你愿意再次试玩的意愿（0 到 10 分）",
                    options=(),
                ),
            ),
        )
        direct_feedback = playtests_service.save_participant_feedback(
            session,
            playtester.id,
            first_started.id,
            "baseline-feedback-direct",
            None,
            evidence_service.FeedbackSubmissionDraft(
                source="direct",
                temporary_alias=None,
                status="submitted",
                answers=(
                    evidence_service.FeedbackAnswerDraft(
                        item_id=feedback_items[0].id,
                        text_value="终局结算的触发顺序还需要举例说明。",
                    ),
                    evidence_service.FeedbackAnswerDraft(
                        item_id=feedback_items[1].id,
                        option_id=feedback_items[1].options[1].id,
                    ),
                    evidence_service.FeedbackAnswerDraft(
                        item_id=feedback_items[2].id,
                        number_value=8,
                    ),
                ),
            ),
        )
        organizer_feedback = playtests_service.create_feedback_submission(
            session,
            owner.id,
            workspace.id,
            work.id,
            first_started.id,
            "baseline-feedback-organizer",
            evidence_service.FeedbackSubmissionDraft(
                source="oral_discussion",
                temporary_alias=None,
                status="submitted",
                answers=(
                    evidence_service.FeedbackAnswerDraft(
                        item_id=feedback_items[0].id,
                        text_value="口头复盘也提到终局说明需要更直观。",
                    ),
                ),
            ),
        )

        second_started = playtests_service.start_session(
            session,
            owner.id,
            workspace.id,
            work.id,
            plan.sessions[1].id,
            plan.sessions[1].revision,
        )
        second_result = playtests_service.save_result(
            session,
            owner.id,
            workspace.id,
            work.id,
            second_started.id,
            second_started.revision,
            playtests_service.ResultDraft(
                actual_headcount=None,
                actual_duration_minutes=None,
                completion_status="interrupted",
                actual_material=playtests_service.ActualMaterialDraft(
                    rule_name=second_started.rule_name,
                    rule_description=second_started.rule_description,
                    rule_content=second_started.rule_content,
                    material_file_ids=(materials[1].id,),
                    change_reason="现场改用打印辅助页进行口头讲解。",
                ),
                actual_participants=(
                    playtests_service.ActualParticipantDraft(
                        planned_account_id=None,
                        temporary_code="临场观察者",
                        seat_or_faction=None,
                        score_or_outcome=None,
                    ),
                ),
            ),
        )
        second_variant = playtests_service.create_observation(
            session,
            owner.id,
            workspace.id,
            work.id,
            second_started.id,
            second_result.session.revision,
            "temporary_variant",
            "因时间不足跳过终局结算，改为口头复盘。",
        )

        phase = "issues"
        issues_service.create_issue(
            session,
            owner.id,
            workspace.id,
            work.id,
            "baseline-issue-modify",
            description="终局结算的触发顺序需要补充示例。",
            decision="modify",
            reason="直接反馈和现场记录都指向同一理解障碍。",
            status="open",
            references=(
                evidence_service.IssueEvidenceReference(
                    source_type="observation", source_id=first_fact.observation.id
                ),
                evidence_service.IssueEvidenceReference(
                    source_type="feedback_submission", source_id=direct_feedback.id
                ),
            ),
        )
        issues_service.create_issue(
            session,
            owner.id,
            workspace.id,
            work.id,
            "baseline-issue-observe",
            description="现有提示对早期协作的引导效果继续观察。",
            decision="observe",
            reason="组织者记录显示协作已经较早出现。",
            status="closed",
            references=(
                evidence_service.IssueEvidenceReference(
                    source_type="observation",
                    source_id=first_interpretation.observation.id,
                ),
                evidence_service.IssueEvidenceReference(
                    source_type="feedback_submission", source_id=organizer_feedback.id
                ),
            ),
        )
        issues_service.create_issue(
            session,
            owner.id,
            workspace.id,
            work.id,
            "baseline-issue-reject",
            description="时间不足时跳过终局结算不作为当前规则问题。",
            decision="reject",
            reason="该场采用临时变体，不能代表当前规则体验。",
            status="open",
            references=(
                evidence_service.IssueEvidenceReference(
                    source_type="observation", source_id=second_variant.observation.id
                ),
            ),
        )

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
        IssueEvidenceLink,
        Issue,
        PlaytestFeedbackAnswer,
        PlaytestFeedbackSubmission,
        PlaytestFeedbackOption,
        PlaytestFeedbackItem,
        PlaytestObservation,
        PlaytestSessionActualMaterial,
        PlaytestSessionActualParticipant,
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
