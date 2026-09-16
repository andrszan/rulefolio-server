from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import (
    set_actor,
    set_feedback_management_scope,
    set_feedback_participant_scope,
    set_file_lifecycle_scope,
    set_playtest_management_scope,
    set_playtest_participant_lookup_scope,
    set_playtest_session_scope,
    set_work_management_scope,
)
from app.core.config import settings
from app.evidence import service as evidence_service
from app.evidence.models import PlaytestFeedbackSubmission
from app.files import materials as file_materials
from app.files import service as files_service
from app.files.models import StoredFile
from app.identity import service as identity_service
from app.identity.models import Account
from app.issues import service as issues_service
from app.notifications.models import MailOutbox, NotificationTodo
from app.notifications.service import (
    cancel_todos_for_target,
    complete_todo_by_source,
    create_todo,
    enqueue_business_mail,
    suppress_business_mails,
)
from app.playtests.models import (
    PlaytestPlan,
    PlaytestSession,
    PlaytestSessionActualMaterial,
    PlaytestSessionActualParticipant,
    PlaytestSessionMaterial,
    PlaytestSessionParticipant,
)
from app.works import service as works_service
from app.workspaces import service as workspaces_service

SCHEDULED = "scheduled"
STARTED = "started"
CANCELLED = "cancelled"
INVITED = "invited"
CONFIRMED = "confirmed"


class PlaytestUnavailable(Exception):
    pass


class PlaytestManagementForbidden(Exception):
    pass


class PlaytestParticipantUnavailable(Exception):
    pass


class PlaytestMaterialSelectionInvalid(Exception):
    pass


class PlaytestSessionRevisionConflict(Exception):
    pass


class PlaytestCapacityExceeded(Exception):
    pass


class PlaytestCapacityBelowConfirmed(Exception):
    pass


class PlaytestSessionStateInvalid(Exception):
    pass


class PlaytestOperationRetryable(Exception):
    pass


class PlaytestRetestInvalid(Exception):
    pass


class PlaytestRetestUnavailable(Exception):
    pass


class PlaytestRetestRevisionConflict(Exception):
    pass


class PlaytestOverviewInvalid(Exception):
    pass


class PlaytestOverviewUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SessionDraft:
    scheduled_at: datetime
    location: str
    capacity: int
    material_file_ids: tuple[UUID, ...]
    participant_emails: tuple[str, ...]


@dataclass(frozen=True)
class MaterialData:
    id: UUID
    display_name: str
    detected_content_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class NotificationData:
    status: str | None
    attempt_count: int | None


@dataclass(frozen=True)
class ParticipantData:
    account_id: UUID
    email: str
    status: str
    latest_notification: NotificationData


@dataclass(frozen=True)
class SessionData:
    id: UUID
    scheduled_at: datetime
    location: str
    capacity: int
    status: str
    revision: int
    started_at: datetime | None
    observation_goals: str
    recording_method: str
    work_name: str
    rule_name: str
    rule_description: str | None
    rule_content: str
    confirmed_count: int
    materials: tuple[MaterialData, ...]
    participants: tuple[ParticipantData, ...]


@dataclass(frozen=True)
class PlanData:
    id: UUID
    workspace_id: UUID
    work_id: UUID
    observation_goals: str
    recording_method: str
    created_at: datetime
    sessions: tuple[SessionData, ...]


@dataclass(frozen=True)
class PlanSummaryData:
    id: UUID
    observation_goals: str
    recording_method: str
    created_at: datetime
    session_count: int
    next_session: SessionData | None


@dataclass(frozen=True)
class ParticipantSessionData:
    id: UUID
    work_name: str
    observation_goals: str
    recording_method: str
    scheduled_at: datetime
    location: str
    capacity: int
    confirmed_count: int
    status: str
    own_status: str
    rule_name: str
    rule_description: str | None
    rule_content: str
    materials: tuple[MaterialData, ...]
    latest_notification: NotificationData
    feedback_items: tuple[evidence_service.FeedbackItemData, ...]
    own_feedback: evidence_service.FeedbackSubmissionData | None


@dataclass(frozen=True)
class ManagedFeedbackData:
    session: SessionData
    actual_material_recorded: bool
    feedback: evidence_service.FeedbackData


@dataclass(frozen=True)
class ConfirmationData:
    status: str
    confirmed_count: int
    capacity: int


@dataclass(frozen=True)
class ActualMaterialData:
    rule_name: str
    rule_description: str | None
    rule_content: str
    change_reason: str | None
    materials: tuple[MaterialData, ...]


@dataclass(frozen=True)
class ActualParticipantData:
    planned_account_id: UUID | None
    email: str | None
    temporary_code: str | None
    seat_or_faction: str | None
    score_or_outcome: str | None


@dataclass(frozen=True)
class ResultData:
    session: SessionData
    actual_headcount: int | None
    actual_duration_minutes: int | None
    completion_status: str | None
    actual_play_mode: str | None
    material_candidates: tuple[MaterialData, ...]
    actual_material: ActualMaterialData | None
    actual_participants: tuple[ActualParticipantData, ...]
    observations: tuple[evidence_service.ObservationData, ...]


@dataclass(frozen=True)
class ActualMaterialDraft:
    rule_name: str
    rule_description: str | None
    rule_content: str
    material_file_ids: tuple[UUID, ...]
    change_reason: str | None


@dataclass(frozen=True)
class ActualParticipantDraft:
    planned_account_id: UUID | None
    temporary_code: str | None
    seat_or_faction: str | None
    score_or_outcome: str | None


@dataclass(frozen=True)
class ResultDraft:
    actual_headcount: int | None
    actual_duration_minutes: int | None
    completion_status: str | None
    actual_material: ActualMaterialDraft | None
    actual_participants: tuple[ActualParticipantDraft, ...]
    actual_play_mode: str | None = None


@dataclass(frozen=True)
class OverviewFilters:
    scheduled_from: datetime | None = None
    scheduled_before: datetime | None = None
    actual_headcount_min: int | None = None
    actual_headcount_max: int | None = None
    actual_play_mode: str | None = None


@dataclass(frozen=True)
class HeadcountCoverageData:
    actual_headcount: int
    completed_count: int
    interrupted_count: int
    unrecorded_completion_count: int
    temporary_variant_count: int


@dataclass(frozen=True)
class OverviewData:
    filters: OverviewFilters
    included_session_count: int
    missing_actual_headcount_count: int
    missing_actual_duration_count: int
    missing_completion_status_count: int
    missing_actual_play_mode_count: int
    temporary_variant_count: int
    headcount_coverage: tuple[HeadcountCoverageData, ...]
    play_modes: tuple[str, ...]


@dataclass(frozen=True)
class OverviewSessionData:
    id: UUID
    scheduled_at: datetime
    actual_headcount: int | None
    actual_duration_minutes: int | None
    completion_status: str | None
    actual_play_mode: str | None
    has_temporary_variant: bool


@dataclass(frozen=True)
class ObservationMutationData:
    observation: evidence_service.ObservationData
    revision: int


class PlaytestResultInvalid(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _commit_or_rollback(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _scope(session: Session, workspace_id: UUID, work_id: UUID) -> None:
    set_playtest_management_scope(session, workspace_id, work_id)


def _require_management(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> None:
    set_actor(session, actor_id)
    try:
        works_service.ensure_playtest_management(
            session, actor_id, workspace_id, work_id
        )
    except works_service.WorkManagementForbidden as error:
        raise PlaytestManagementForbidden from error
    except works_service.WorkUnavailable as error:
        raise PlaytestUnavailable from error
    except works_service.WorkOperationRetryable as error:
        raise PlaytestOperationRetryable from error
    _scope(session, workspace_id, work_id)


def _require_overview(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> None:
    set_actor(session, actor_id)
    try:
        works_service.ensure_work_access(session, actor_id, workspace_id, work_id)
    except works_service.WorkUnavailable as error:
        raise PlaytestOverviewUnavailable from error
    except works_service.WorkOperationRetryable as error:
        raise PlaytestOperationRetryable from error


def _load_plan(
    session: Session, workspace_id: UUID, work_id: UUID, plan_id: UUID
) -> PlaytestPlan:
    plan = session.scalar(
        select(PlaytestPlan).where(
            PlaytestPlan.id == plan_id,
            PlaytestPlan.workspace_id == workspace_id,
            PlaytestPlan.work_id == work_id,
        )
    )
    if plan is None:
        session.rollback()
        raise PlaytestUnavailable
    return plan


def _load_managed_session(
    session: Session, workspace_id: UUID, work_id: UUID, session_id: UUID, *, lock: bool
) -> PlaytestSession:
    statement = select(PlaytestSession).where(
        PlaytestSession.id == session_id,
        PlaytestSession.workspace_id == workspace_id,
        PlaytestSession.work_id == work_id,
    )
    if lock:
        statement = statement.with_for_update()
    item = session.scalar(statement)
    if item is None:
        session.rollback()
        raise PlaytestUnavailable
    set_playtest_session_scope(session, item.id)
    return item


def _mail_data(session: Session, outbox_id: UUID | None) -> NotificationData:
    if outbox_id is None:
        return NotificationData(status=None, attempt_count=None)
    outbox = session.get(MailOutbox, outbox_id)
    if outbox is None:
        return NotificationData(status=None, attempt_count=None)
    return NotificationData(status=outbox.status, attempt_count=outbox.attempt_count)


def _materials(session: Session, session_id: UUID) -> tuple[MaterialData, ...]:
    relations = list(
        session.scalars(
            select(PlaytestSessionMaterial)
            .where(PlaytestSessionMaterial.session_id == session_id)
            .order_by(PlaytestSessionMaterial.file_id)
        )
    )
    result: list[MaterialData] = []
    for relation in relations:
        # 场次关系已核验后，files 只读取精确材料 ID。
        set_file_lifecycle_scope(session, relation.file_id)
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == relation.file_id,
                StoredFile.kind == "material",
                StoredFile.status == "ready",
            )
        )
        if file is None or file.sha256 != relation.sha256:
            raise PlaytestUnavailable
        result.append(
            MaterialData(
                id=file.id,
                display_name=file.display_name,
                detected_content_type=file.detected_content_type,
                size_bytes=file.size_bytes,
                sha256=relation.sha256.hex(),
            )
        )
    return tuple(result)


def _participants(session: Session, session_id: UUID) -> tuple[ParticipantData, ...]:
    rows = session.execute(
        select(PlaytestSessionParticipant, Account)
        .join(Account, Account.id == PlaytestSessionParticipant.account_id)
        .where(PlaytestSessionParticipant.session_id == session_id)
        .order_by(Account.email, PlaytestSessionParticipant.account_id)
    )
    return tuple(
        ParticipantData(
            account_id=participant.account_id,
            email=account.email,
            status=participant.status,
            latest_notification=_mail_data(session, participant.latest_outbox_id),
        )
        for participant, account in rows
    )


def _confirmed_count(session: Session, session_id: UUID) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(PlaytestSessionParticipant)
            .where(
                PlaytestSessionParticipant.session_id == session_id,
                PlaytestSessionParticipant.status == CONFIRMED,
            )
        )
        or 0
    )


def _session_data(
    session: Session, item: PlaytestSession, *, include_participants: bool
) -> SessionData:
    set_playtest_session_scope(session, item.id)
    return SessionData(
        id=item.id,
        scheduled_at=item.scheduled_at,
        location=item.location,
        capacity=item.capacity,
        status=item.status,
        revision=item.revision,
        started_at=item.started_at,
        observation_goals=item.observation_goals,
        recording_method=item.recording_method,
        work_name=item.work_name,
        rule_name=item.rule_name,
        rule_description=item.rule_description,
        rule_content=item.rule_content,
        confirmed_count=_confirmed_count(session, item.id),
        materials=_materials(session, item.id),
        participants=_participants(session, item.id) if include_participants else (),
    )


def _resolve_participants(
    session: Session, emails: tuple[str, ...]
) -> tuple[Account, ...]:
    if not emails:
        return ()
    accounts: list[Account] = []
    seen: set[str] = set()
    try:
        for email in emails:
            normalized = identity_service.normalize_email(email)
            if normalized in seen:
                raise PlaytestParticipantUnavailable
            seen.add(normalized)
            account = identity_service.find_active_account_by_email(session, normalized)
            if account is None:
                raise PlaytestParticipantUnavailable
            accounts.append(account)
    except ValueError as error:
        raise PlaytestParticipantUnavailable from error
    return tuple(accounts)


def _validate_draft(draft: SessionDraft) -> SessionDraft:
    location = draft.location.strip()
    if (
        not location
        or draft.capacity <= 0
        or draft.scheduled_at.tzinfo is None
        or not draft.material_file_ids
        or len(set(draft.material_file_ids)) != len(draft.material_file_ids)
    ):
        raise PlaytestMaterialSelectionInvalid
    return SessionDraft(
        scheduled_at=draft.scheduled_at,
        location=location,
        capacity=draft.capacity,
        material_file_ids=draft.material_file_ids,
        participant_emails=draft.participant_emails,
    )


def _business_scope(session_id: UUID) -> str:
    return f"playtest-session:{session_id}"


def _mail_content(work_name: str, purpose: str, session_id: UUID) -> tuple[str, str]:
    if purpose == "playtest_invitation":
        subject = "你受邀参加试玩场次"
        action = "你受邀参加"
    elif purpose == "playtest_arrangement_updated":
        subject = "试玩场次安排已更新"
        action = "你参与的试玩场次安排已更新"
    elif purpose == "playtest_material_updated":
        subject = "试玩场次材料已更新"
        action = "你参与的试玩场次材料已更新"
    elif purpose == "playtest_cancelled":
        subject = "试玩场次已取消"
        action = "你参与的试玩场次已取消"
    else:
        raise ValueError("不支持的试玩邮件用途")
    link = f"{settings.app_public_url.rstrip('/')}/playtest-sessions/{session_id}"
    return subject, f"{action}《{work_name}》。请登录 Rulefolio 查看本场安排：\n{link}"


def notification_todo_eligible(session: Session, todo: NotificationTodo) -> bool:
    if todo.target_kind != "playtest_session":
        return False
    set_actor(session, todo.recipient_account_id)
    set_playtest_participant_lookup_scope(session, todo.target_id)
    participant = session.scalar(
        select(PlaytestSessionParticipant).where(
            PlaytestSessionParticipant.session_id == todo.target_id,
            PlaytestSessionParticipant.account_id == todo.recipient_account_id,
        )
    )
    if participant is None:
        return False
    set_playtest_session_scope(session, todo.target_id)
    item = session.scalar(
        select(PlaytestSession).where(PlaytestSession.id == todo.target_id)
    )
    if item is None:
        return False
    return (
        item.status == CANCELLED
        if todo.kind == "playtest_cancelled"
        else item.status == SCHEDULED
    )


def _todo_details(
    item: PlaytestSession, purpose: str, participant_id: UUID
) -> tuple[str, str, str]:
    if purpose == "playtest_invitation":
        return "playtest_invitation", str(participant_id), "确认试玩邀请"
    if purpose == "playtest_arrangement_updated":
        return (
            "playtest_arrangement_updated",
            f"{item.id}:revision:{item.revision}",
            "查看场次安排变更",
        )
    if purpose == "playtest_material_updated":
        return (
            "playtest_material_updated",
            f"{item.id}:revision:{item.revision}",
            "查看场次材料更新",
        )
    if purpose == "playtest_cancelled":
        return "playtest_cancelled", str(participant_id), "查看场次取消说明"
    raise ValueError("不支持的试玩待办用途")


def _notify_participants(session: Session, item: PlaytestSession, purpose: str) -> None:
    set_playtest_session_scope(session, item.id)
    participants = list(
        session.scalars(
            select(PlaytestSessionParticipant).where(
                PlaytestSessionParticipant.session_id == item.id
            )
        )
    )
    subject, body = _mail_content(item.work_name, purpose, item.id)
    for participant in participants:
        kind, source_key, summary = _todo_details(item, purpose, participant.id)
        todo, created = create_todo(
            session,
            recipient_account_id=participant.account_id,
            workspace_id=item.workspace_id,
            work_id=item.work_id,
            kind=kind,
            target_kind="playtest_session",
            target_id=item.id,
            source_key=source_key,
            summary=summary,
            context_label=f"作品：{item.work_name}",
        )
        if created:
            outbox = enqueue_business_mail(
                session,
                participant.account_id,
                purpose,
                subject,
                body,
                business_scope=_business_scope(item.id),
                todo_id=todo.id,
            )
            participant.latest_outbox_id = outbox.id


def _notify_feedback_maintainers(
    session: Session, item: PlaytestSession, submission_id: UUID
) -> None:
    set_work_management_scope(session, item.work_id, item.workspace_id)
    for account_id in workspaces_service.current_work_maintainer_ids(
        session, item.work_id
    ):
        todo, created = create_todo(
            session,
            recipient_account_id=account_id,
            workspace_id=item.workspace_id,
            work_id=item.work_id,
            kind="feedback_submitted",
            target_kind="playtest_session",
            target_id=item.id,
            source_key=str(submission_id),
            summary="有新的试玩反馈需要处理",
            context_label=f"作品：{item.work_name}",
        )
        if created:
            enqueue_business_mail(
                session,
                account_id,
                "feedback_submitted",
                "有新的试玩反馈需要处理",
                f"《{item.work_name}》有新的试玩反馈，请登录 Rulefolio 查看。",
                business_scope=f"feedback-submission:{submission_id}",
                todo_id=todo.id,
            )


def _create_session(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    plan: PlaytestPlan,
    draft: SessionDraft,
) -> PlaytestSession:
    draft = _validate_draft(draft)
    participants = _resolve_participants(session, draft.participant_emails)
    try:
        snapshot = works_service.snapshot_current_rule_materials_for_playtest(
            session,
            actor_id,
            workspace_id,
            work_id,
            set(draft.material_file_ids),
        )
    except works_service.MaterialSelectionInvalid as error:
        raise PlaytestMaterialSelectionInvalid from error
    except works_service.WorkManagementForbidden as error:
        raise PlaytestManagementForbidden from error
    except works_service.WorkUnavailable as error:
        raise PlaytestUnavailable from error
    except works_service.WorkOperationRetryable as error:
        raise PlaytestOperationRetryable from error
    item = PlaytestSession(
        plan_id=plan.id,
        workspace_id=workspace_id,
        work_id=work_id,
        scheduled_at=draft.scheduled_at,
        location=draft.location,
        capacity=draft.capacity,
        observation_goals=plan.observation_goals,
        recording_method=plan.recording_method,
        work_name=snapshot.work_name,
        rule_name=snapshot.rule_name,
        rule_description=snapshot.rule_description,
        rule_content=snapshot.rule_content,
    )
    session.add(item)
    session.flush()
    set_playtest_session_scope(session, item.id)
    for account in participants:
        session.add(
            PlaytestSessionParticipant(session_id=item.id, account_id=account.id)
        )
    session.flush()
    _notify_participants(session, item, "playtest_invitation")
    session.add_all(
        PlaytestSessionMaterial(session_id=item.id, file_id=file.id, sha256=file.sha256)
        for file in snapshot.materials
    )
    session.flush()
    return item


def _validated_overview_filters(filters: OverviewFilters) -> OverviewFilters:
    if (
        (filters.scheduled_from is not None and filters.scheduled_from.tzinfo is None)
        or (
            filters.scheduled_before is not None
            and filters.scheduled_before.tzinfo is None
        )
        or (
            filters.actual_headcount_min is not None
            and (
                not isinstance(filters.actual_headcount_min, int)
                or isinstance(filters.actual_headcount_min, bool)
                or filters.actual_headcount_min < 0
            )
        )
        or (
            filters.actual_headcount_max is not None
            and (
                not isinstance(filters.actual_headcount_max, int)
                or isinstance(filters.actual_headcount_max, bool)
                or filters.actual_headcount_max < 0
            )
        )
        or (
            filters.actual_headcount_min is not None
            and filters.actual_headcount_max is not None
            and filters.actual_headcount_max < filters.actual_headcount_min
        )
    ):
        raise PlaytestOverviewInvalid
    try:
        actual_play_mode = _optional_text(filters.actual_play_mode, 160)
    except PlaytestResultInvalid as error:
        raise PlaytestOverviewInvalid from error
    return OverviewFilters(
        scheduled_from=filters.scheduled_from,
        scheduled_before=filters.scheduled_before,
        actual_headcount_min=filters.actual_headcount_min,
        actual_headcount_max=filters.actual_headcount_max,
        actual_play_mode=actual_play_mode,
    )


def _overview_parameters(
    workspace_id: UUID, work_id: UUID, filters: OverviewFilters
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "work_id": work_id,
        "scheduled_from": filters.scheduled_from,
        "scheduled_before": filters.scheduled_before,
        "actual_headcount_min": filters.actual_headcount_min,
        "actual_headcount_max": filters.actual_headcount_max,
        "actual_play_mode": filters.actual_play_mode,
    }


def read_overview(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    filters: OverviewFilters,
) -> OverviewData:
    filters = _validated_overview_filters(filters)
    try:
        _require_overview(session, actor_id, workspace_id, work_id)
        payload = session.scalar(
            text(
                "SELECT public.playtest_overview_summary("
                ":workspace_id, :work_id, :scheduled_from, :scheduled_before, "
                ":actual_headcount_min, :actual_headcount_max, :actual_play_mode)"
            ),
            _overview_parameters(workspace_id, work_id, filters),
        )
        if not isinstance(payload, dict):
            raise PlaytestOperationRetryable
        coverage = tuple(
            HeadcountCoverageData(
                actual_headcount=item["actual_headcount"],
                completed_count=item["completed_count"],
                interrupted_count=item["interrupted_count"],
                unrecorded_completion_count=item["unrecorded_completion_count"],
                temporary_variant_count=item["temporary_variant_count"],
            )
            for item in payload["headcount_coverage"]
        )
    except (
        PlaytestOverviewInvalid,
        PlaytestOverviewUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return OverviewData(
        filters=filters,
        included_session_count=payload["included_session_count"],
        missing_actual_headcount_count=payload["missing_actual_headcount_count"],
        missing_actual_duration_count=payload["missing_actual_duration_count"],
        missing_completion_status_count=payload["missing_completion_status_count"],
        missing_actual_play_mode_count=payload["missing_actual_play_mode_count"],
        temporary_variant_count=payload["temporary_variant_count"],
        headcount_coverage=coverage,
        play_modes=tuple(payload["play_modes"]),
    )


def list_overview_sessions(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    filters: OverviewFilters,
    page: int,
    size: int,
) -> tuple[list[OverviewSessionData], int]:
    filters = _validated_overview_filters(filters)
    try:
        _require_overview(session, actor_id, workspace_id, work_id)
        rows = list(
            session.execute(
                text(
                    "SELECT * FROM public.playtest_overview_sessions("
                    ":workspace_id, :work_id, :scheduled_from, :scheduled_before, "
                    ":actual_headcount_min, :actual_headcount_max, :actual_play_mode, "
                    ":page, :size)"
                ),
                _overview_parameters(workspace_id, work_id, filters)
                | {"page": page, "size": size},
            ).mappings()
        )
    except (
        PlaytestOverviewInvalid,
        PlaytestOverviewUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return (
        [
            OverviewSessionData(
                id=row["id"],
                scheduled_at=row["scheduled_at"],
                actual_headcount=row["actual_headcount"],
                actual_duration_minutes=row["actual_duration_minutes"],
                completion_status=row["completion_status"],
                actual_play_mode=row["actual_play_mode"],
                has_temporary_variant=row["has_temporary_variant"],
            )
            for row in rows
        ],
        int(rows[0]["total"]) if rows else 0,
    )


def list_overview_issues(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    kind: str,
    page: int,
    size: int,
) -> tuple[list[issues_service.OverviewIssueData], int]:
    if kind not in {"pending-retest", "needs-action"}:
        raise PlaytestOverviewInvalid
    try:
        _require_overview(session, actor_id, workspace_id, work_id)
        rows = list(
            session.execute(
                text(
                    "SELECT * FROM public.playtest_overview_issues("
                    ":workspace_id, :work_id, :kind, :page, :size)"
                ),
                {
                    "workspace_id": workspace_id,
                    "work_id": work_id,
                    "kind": kind,
                    "page": page,
                    "size": size,
                },
            ).mappings()
        )
    except (
        PlaytestOverviewInvalid,
        PlaytestOverviewUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return (
        [
            issues_service.OverviewIssueData(
                id=row["id"],
                description=row["description"],
                decision=row["decision"],
                status=row["status"],
                verification_status=row["verification_status"],
                current_conclusion_type=row["current_conclusion_type"],
                current_conclusion_session_id=row["current_conclusion_session_id"],
            )
            for row in rows
        ],
        int(rows[0]["total"]) if rows else 0,
    )


def list_plans(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    page: int,
    size: int,
) -> tuple[list[PlanSummaryData], int]:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        total = (
            session.scalar(
                select(func.count())
                .select_from(PlaytestPlan)
                .where(
                    PlaytestPlan.workspace_id == workspace_id,
                    PlaytestPlan.work_id == work_id,
                )
            )
            or 0
        )
        latest_session_at = (
            select(func.max(PlaytestSession.scheduled_at))
            .where(PlaytestSession.plan_id == PlaytestPlan.id)
            .correlate(PlaytestPlan)
            .scalar_subquery()
        )
        plans = list(
            session.scalars(
                select(PlaytestPlan)
                .where(
                    PlaytestPlan.workspace_id == workspace_id,
                    PlaytestPlan.work_id == work_id,
                )
                .order_by(
                    latest_session_at.desc().nulls_last(),
                    PlaytestPlan.created_at.desc(),
                    PlaytestPlan.id,
                )
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        result: list[PlanSummaryData] = []
        for plan in plans:
            session_count = (
                session.scalar(
                    select(func.count())
                    .select_from(PlaytestSession)
                    .where(PlaytestSession.plan_id == plan.id)
                )
                or 0
            )
            next_session = session.scalar(
                select(PlaytestSession)
                .where(
                    PlaytestSession.plan_id == plan.id,
                    PlaytestSession.status == SCHEDULED,
                )
                .order_by(PlaytestSession.scheduled_at, PlaytestSession.id)
                .limit(1)
            )
            result.append(
                PlanSummaryData(
                    id=plan.id,
                    observation_goals=plan.observation_goals,
                    recording_method=plan.recording_method,
                    created_at=plan.created_at,
                    session_count=session_count,
                    next_session=(
                        _session_data(session, next_session, include_participants=False)
                        if next_session is not None
                        else None
                    ),
                )
            )
    except (
        PlaytestManagementForbidden,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return result, total


def read_plan(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    plan_id: UUID,
) -> PlanData:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        plan = _load_plan(session, workspace_id, work_id, plan_id)
        sessions = list(
            session.scalars(
                select(PlaytestSession)
                .where(PlaytestSession.plan_id == plan.id)
                .order_by(PlaytestSession.scheduled_at, PlaytestSession.id)
            )
        )
        return PlanData(
            id=plan.id,
            workspace_id=plan.workspace_id,
            work_id=plan.work_id,
            observation_goals=plan.observation_goals,
            recording_method=plan.recording_method,
            created_at=plan.created_at,
            sessions=tuple(
                _session_data(session, item, include_participants=True)
                for item in sessions
            ),
        )
    except (
        PlaytestManagementForbidden,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def create_plan(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    observation_goals: str,
    recording_method: str,
    drafts: tuple[SessionDraft, ...],
    *,
    retest_issue_id: UUID | None = None,
    retest_issue_expected_revision: int | None = None,
) -> PlanData:
    observation_goals = observation_goals.strip()
    recording_method = recording_method.strip()
    if (
        not observation_goals
        or not recording_method
        or not drafts
        or (retest_issue_id is None) != (retest_issue_expected_revision is None)
        or (retest_issue_id is not None and len(drafts) != 1)
    ):
        raise PlaytestMaterialSelectionInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        plan = PlaytestPlan(
            workspace_id=workspace_id,
            work_id=work_id,
            observation_goals=observation_goals,
            recording_method=recording_method,
            creator_account_id=actor_id,
        )
        session.add(plan)
        session.flush()
        sessions = tuple(
            _create_session(session, actor_id, workspace_id, work_id, plan, draft)
            for draft in drafts
        )
        if retest_issue_id is not None:
            issues_service.bind_retest_session(
                session,
                actor_id,
                workspace_id,
                work_id,
                retest_issue_id,
                sessions[0].id,
                retest_issue_expected_revision,
            )
        _commit_or_rollback(session)
    except issues_service.IssueRevisionConflict as error:
        session.rollback()
        raise PlaytestRetestRevisionConflict from error
    except (
        issues_service.IssueManagementForbidden,
        issues_service.IssueUnavailable,
    ) as error:
        session.rollback()
        raise PlaytestRetestUnavailable from error
    except issues_service.IssueInvalid as error:
        session.rollback()
        raise PlaytestRetestInvalid from error
    except issues_service.IssueOperationRetryable as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    except (
        PlaytestManagementForbidden,
        PlaytestParticipantUnavailable,
        PlaytestMaterialSelectionInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return read_plan(session, actor_id, workspace_id, work_id, plan.id)


def append_session(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    plan_id: UUID,
    draft: SessionDraft,
) -> SessionData:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        plan = _load_plan(session, workspace_id, work_id, plan_id)
        item = _create_session(session, actor_id, workspace_id, work_id, plan, draft)
        item_id = item.id
        _commit_or_rollback(session)
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, item_id, lock=False
        )
        return _session_data(session, item, include_participants=True)
    except (
        PlaytestManagementForbidden,
        PlaytestParticipantUnavailable,
        PlaytestMaterialSelectionInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def update_arrangement(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    *,
    scheduled_at: datetime,
    location: str,
    capacity: int,
    expected_revision: int,
) -> SessionData:
    location = location.strip()
    if not location or capacity <= 0 or scheduled_at.tzinfo is None:
        raise PlaytestMaterialSelectionInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=True
        )
        if item.revision != expected_revision:
            session.rollback()
            raise PlaytestSessionRevisionConflict
        if item.status != SCHEDULED:
            session.rollback()
            raise PlaytestSessionStateInvalid
        if capacity < _confirmed_count(session, item.id):
            session.rollback()
            raise PlaytestCapacityBelowConfirmed
        item.scheduled_at = scheduled_at
        item.location = location
        item.capacity = capacity
        item.revision += 1
        cancel_todos_for_target(
            session,
            "playtest_session",
            item.id,
            kinds={"playtest_arrangement_updated"},
        )
        _notify_participants(session, item, "playtest_arrangement_updated")
        _commit_or_rollback(session)
    except (
        PlaytestCapacityBelowConfirmed,
        PlaytestManagementForbidden,
        PlaytestMaterialSelectionInvalid,
        PlaytestSessionRevisionConflict,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return read_managed_session(session, actor_id, workspace_id, work_id, session_id)


def replace_materials(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    material_file_ids: tuple[UUID, ...],
    expected_revision: int,
) -> SessionData:
    if not material_file_ids or len(set(material_file_ids)) != len(material_file_ids):
        raise PlaytestMaterialSelectionInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=True
        )
        if item.revision != expected_revision:
            session.rollback()
            raise PlaytestSessionRevisionConflict
        if item.status != SCHEDULED:
            session.rollback()
            raise PlaytestSessionStateInvalid
        try:
            snapshot = works_service.snapshot_current_rule_materials_for_playtest(
                session,
                actor_id,
                workspace_id,
                work_id,
                set(material_file_ids),
            )
        except works_service.MaterialSelectionInvalid as error:
            raise PlaytestMaterialSelectionInvalid from error
        item.work_name = snapshot.work_name
        item.rule_name = snapshot.rule_name
        item.rule_description = snapshot.rule_description
        item.rule_content = snapshot.rule_content
        item.revision += 1
        set_playtest_session_scope(session, item.id)
        session.execute(
            delete(PlaytestSessionMaterial).where(
                PlaytestSessionMaterial.session_id == item.id
            )
        )
        session.add_all(
            PlaytestSessionMaterial(
                session_id=item.id, file_id=file.id, sha256=file.sha256
            )
            for file in snapshot.materials
        )
        cancel_todos_for_target(
            session,
            "playtest_session",
            item.id,
            kinds={"playtest_material_updated"},
        )
        _notify_participants(session, item, "playtest_material_updated")
        _commit_or_rollback(session)
    except (
        PlaytestManagementForbidden,
        PlaytestMaterialSelectionInvalid,
        PlaytestSessionRevisionConflict,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return read_managed_session(session, actor_id, workspace_id, work_id, session_id)


def start_session(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    expected_revision: int,
) -> SessionData:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=True
        )
        if item.revision != expected_revision:
            session.rollback()
            raise PlaytestSessionRevisionConflict
        if item.status != SCHEDULED:
            session.rollback()
            raise PlaytestSessionStateInvalid
        item.status = STARTED
        item.started_at = _now()
        item.revision += 1
        _commit_or_rollback(session)
    except (
        PlaytestManagementForbidden,
        PlaytestSessionRevisionConflict,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return read_managed_session(session, actor_id, workspace_id, work_id, session_id)


def cancel_session(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    expected_revision: int,
) -> SessionData:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=True
        )
        if item.revision != expected_revision:
            session.rollback()
            raise PlaytestSessionRevisionConflict
        if item.status != SCHEDULED:
            session.rollback()
            raise PlaytestSessionStateInvalid
        item.status = CANCELLED
        item.revision += 1
        suppress_business_mails(session, _business_scope(item.id))
        cancel_todos_for_target(
            session,
            "playtest_session",
            item.id,
            kinds={
                "playtest_invitation",
                "playtest_arrangement_updated",
                "playtest_material_updated",
            },
        )
        issues_service.cancel_retest_arranged_for_session(
            session, workspace_id, work_id, item.id
        )
        _notify_participants(session, item, "playtest_cancelled")
        _commit_or_rollback(session)
    except (
        PlaytestManagementForbidden,
        PlaytestSessionRevisionConflict,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return read_managed_session(session, actor_id, workspace_id, work_id, session_id)


def _actual_materials(
    session: Session, item: PlaytestSession
) -> ActualMaterialData | None:
    if not item.actual_material_recorded:
        return None
    if item.actual_rule_name is None or item.actual_rule_content is None:
        raise PlaytestUnavailable
    relations = list(
        session.scalars(
            select(PlaytestSessionActualMaterial)
            .where(PlaytestSessionActualMaterial.session_id == item.id)
            .order_by(PlaytestSessionActualMaterial.file_id)
        )
    )
    materials: list[MaterialData] = []
    for relation in relations:
        set_file_lifecycle_scope(session, relation.file_id)
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == relation.file_id,
                StoredFile.kind == "material",
                StoredFile.status == "ready",
            )
        )
        if file is None or file.sha256 != relation.sha256:
            raise PlaytestUnavailable
        materials.append(
            MaterialData(
                id=file.id,
                display_name=file.display_name,
                detected_content_type=file.detected_content_type,
                size_bytes=file.size_bytes,
                sha256=relation.sha256.hex(),
            )
        )
    return ActualMaterialData(
        rule_name=item.actual_rule_name,
        rule_description=item.actual_rule_description,
        rule_content=item.actual_rule_content,
        change_reason=item.actual_material_change_reason,
        materials=tuple(materials),
    )


def _actual_participants(
    session: Session, session_id: UUID
) -> tuple[ActualParticipantData, ...]:
    rows = session.execute(
        select(PlaytestSessionActualParticipant, Account.email)
        .outerjoin(
            Account,
            Account.id == PlaytestSessionActualParticipant.planned_account_id,
        )
        .where(PlaytestSessionActualParticipant.session_id == session_id)
        .order_by(PlaytestSessionActualParticipant.id)
    )
    return tuple(
        ActualParticipantData(
            planned_account_id=participant.planned_account_id,
            email=email,
            temporary_code=participant.temporary_code,
            seat_or_faction=participant.seat_or_faction,
            score_or_outcome=participant.score_or_outcome,
        )
        for participant, email in rows
    )


def _result_data(session: Session, item: PlaytestSession) -> ResultData:
    set_playtest_session_scope(session, item.id)
    return ResultData(
        session=_session_data(session, item, include_participants=True),
        actual_headcount=item.actual_headcount,
        actual_duration_minutes=item.actual_duration_minutes,
        completion_status=item.completion_status,
        actual_play_mode=item.actual_play_mode,
        material_candidates=tuple(
            MaterialData(
                id=file.id,
                display_name=file.display_name,
                detected_content_type=file.detected_content_type,
                size_bytes=file.size_bytes,
                sha256=file.sha256.hex(),
            )
            for file in file_materials.playtest_result_ready_materials(
                session, item.workspace_id, item.work_id
            )
        ),
        actual_material=_actual_materials(session, item),
        actual_participants=_actual_participants(session, item.id),
        observations=evidence_service.list_observations(session, item.id),
    )


def _optional_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlaytestResultInvalid
    value = value.strip()
    if len(value) > limit:
        raise PlaytestResultInvalid
    return value or None


def _required_text(value: str, limit: int) -> str:
    normalized = _optional_text(value, limit)
    if normalized is None:
        raise PlaytestResultInvalid
    return normalized


def _validate_result_draft(
    session: Session, item: PlaytestSession, draft: ResultDraft
) -> ResultDraft:
    if draft.actual_headcount is not None and (
        not isinstance(draft.actual_headcount, int)
        or isinstance(draft.actual_headcount, bool)
        or draft.actual_headcount < 0
    ):
        raise PlaytestResultInvalid
    if draft.actual_duration_minutes is not None and (
        not isinstance(draft.actual_duration_minutes, int)
        or isinstance(draft.actual_duration_minutes, bool)
        or draft.actual_duration_minutes < 0
    ):
        raise PlaytestResultInvalid
    if draft.completion_status not in {None, "completed", "interrupted"}:
        raise PlaytestResultInvalid
    actual_play_mode = _optional_text(draft.actual_play_mode, 160)

    participant_ids = set(
        session.scalars(
            select(PlaytestSessionParticipant.account_id).where(
                PlaytestSessionParticipant.session_id == item.id
            )
        )
    )
    accounts: set[UUID] = set()
    temporary_codes: set[str] = set()
    actual_participants: list[ActualParticipantDraft] = []
    for participant in draft.actual_participants:
        account_id = participant.planned_account_id
        temporary_code = _optional_text(participant.temporary_code, 160)
        seat_or_faction = _optional_text(participant.seat_or_faction, 160)
        score_or_outcome = _optional_text(participant.score_or_outcome, 160)
        if (account_id is None) == (temporary_code is None):
            raise PlaytestResultInvalid
        if account_id is not None:
            if account_id not in participant_ids or account_id in accounts:
                raise PlaytestResultInvalid
            accounts.add(account_id)
        elif temporary_code in temporary_codes:
            raise PlaytestResultInvalid
        else:
            temporary_codes.add(temporary_code)
        actual_participants.append(
            ActualParticipantDraft(
                planned_account_id=account_id,
                temporary_code=temporary_code,
                seat_or_faction=seat_or_faction,
                score_or_outcome=score_or_outcome,
            )
        )
    if draft.actual_headcount is not None and draft.actual_headcount < len(
        actual_participants
    ):
        raise PlaytestResultInvalid

    actual_material = draft.actual_material
    if actual_material is not None:
        rule_name = _required_text(actual_material.rule_name, 160)
        rule_description = _optional_text(actual_material.rule_description, 4_000)
        rule_content = _required_text(actual_material.rule_content, 20_000)
        material_file_ids = actual_material.material_file_ids
        if len(set(material_file_ids)) != len(material_file_ids):
            raise PlaytestResultInvalid
        files = file_materials.playtest_result_ready_materials(
            session,
            item.workspace_id,
            item.work_id,
            set(material_file_ids),
        )
        if len(files) != len(material_file_ids):
            raise PlaytestResultInvalid
        scheduled = {
            (material.id, material.sha256) for material in _materials(session, item.id)
        }
        actual = {(file.id, file.sha256.hex()) for file in files}
        changed = (
            rule_name != item.rule_name
            or rule_description != item.rule_description
            or rule_content != item.rule_content
            or actual != scheduled
        )
        change_reason = _optional_text(actual_material.change_reason, 4_000)
        if changed and change_reason is None:
            raise PlaytestResultInvalid
        actual_material = ActualMaterialDraft(
            rule_name=rule_name,
            rule_description=rule_description,
            rule_content=rule_content,
            material_file_ids=tuple(file.id for file in files),
            change_reason=change_reason if changed else None,
        )

    return ResultDraft(
        actual_headcount=draft.actual_headcount,
        actual_duration_minutes=draft.actual_duration_minutes,
        completion_status=draft.completion_status,
        actual_material=actual_material,
        actual_participants=tuple(actual_participants),
        actual_play_mode=actual_play_mode,
    )


def _require_started(session: Session, item: PlaytestSession) -> None:
    if item.status != STARTED:
        session.rollback()
        raise PlaytestSessionStateInvalid


def read_result(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
) -> ResultData:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=False
        )
        _require_started(session, item)
        return _result_data(session, item)
    except (
        PlaytestManagementForbidden,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def save_result(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    expected_revision: int,
    draft: ResultDraft,
) -> ResultData:
    if expected_revision <= 0:
        raise PlaytestResultInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=True
        )
        if item.revision != expected_revision:
            session.rollback()
            raise PlaytestSessionRevisionConflict
        _require_started(session, item)
        draft = _validate_result_draft(session, item, draft)
        item.actual_headcount = draft.actual_headcount
        item.actual_duration_minutes = draft.actual_duration_minutes
        item.completion_status = draft.completion_status
        item.actual_play_mode = draft.actual_play_mode
        item.actual_material_recorded = draft.actual_material is not None
        item.actual_rule_name = (
            draft.actual_material.rule_name
            if draft.actual_material is not None
            else None
        )
        item.actual_rule_description = (
            draft.actual_material.rule_description
            if draft.actual_material is not None
            else None
        )
        item.actual_rule_content = (
            draft.actual_material.rule_content
            if draft.actual_material is not None
            else None
        )
        item.actual_material_change_reason = (
            draft.actual_material.change_reason
            if draft.actual_material is not None
            else None
        )
        set_playtest_session_scope(session, item.id)
        session.execute(
            delete(PlaytestSessionActualMaterial).where(
                PlaytestSessionActualMaterial.session_id == item.id
            )
        )
        session.execute(
            delete(PlaytestSessionActualParticipant).where(
                PlaytestSessionActualParticipant.session_id == item.id
            )
        )
        if draft.actual_material is not None:
            files = file_materials.playtest_result_ready_materials(
                session,
                item.workspace_id,
                item.work_id,
                set(draft.actual_material.material_file_ids),
            )
            session.add_all(
                PlaytestSessionActualMaterial(
                    session_id=item.id,
                    file_id=file.id,
                    sha256=file.sha256,
                )
                for file in files
            )
        session.add_all(
            PlaytestSessionActualParticipant(
                session_id=item.id,
                planned_account_id=participant.planned_account_id,
                temporary_code=participant.temporary_code,
                seat_or_faction=participant.seat_or_faction,
                score_or_outcome=participant.score_or_outcome,
            )
            for participant in draft.actual_participants
        )
        item.revision += 1
        _commit_or_rollback(session)
    except (
        PlaytestManagementForbidden,
        PlaytestResultInvalid,
        PlaytestSessionRevisionConflict,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return read_result(session, actor_id, workspace_id, work_id, session_id)


def create_observation(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    expected_revision: int,
    kind: str,
    content: str,
) -> ObservationMutationData:
    if expected_revision <= 0:
        raise PlaytestResultInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=True
        )
        if item.revision != expected_revision:
            session.rollback()
            raise PlaytestSessionRevisionConflict
        _require_started(session, item)
        observation = evidence_service.create_observation(
            session, item.id, actor_id, kind, content
        )
        item.revision += 1
        revision = item.revision
        _commit_or_rollback(session)
    except evidence_service.ObservationInvalid as error:
        session.rollback()
        raise PlaytestResultInvalid from error
    except (
        PlaytestManagementForbidden,
        PlaytestResultInvalid,
        PlaytestSessionRevisionConflict,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return ObservationMutationData(observation=observation, revision=revision)


def update_observation(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    observation_id: UUID,
    expected_revision: int,
    kind: str,
    content: str,
) -> ObservationMutationData:
    if expected_revision <= 0:
        raise PlaytestResultInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=True
        )
        if item.revision != expected_revision:
            session.rollback()
            raise PlaytestSessionRevisionConflict
        _require_started(session, item)
        observation = evidence_service.update_observation(
            session, item.id, observation_id, actor_id, kind, content
        )
        item.revision += 1
        revision = item.revision
        _commit_or_rollback(session)
    except evidence_service.ObservationInvalid as error:
        session.rollback()
        raise PlaytestResultInvalid from error
    except evidence_service.ObservationUnavailable as error:
        session.rollback()
        raise PlaytestUnavailable from error
    except (
        PlaytestManagementForbidden,
        PlaytestResultInvalid,
        PlaytestSessionRevisionConflict,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return ObservationMutationData(observation=observation, revision=revision)


def read_managed_session(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
) -> SessionData:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        item = _load_managed_session(
            session, workspace_id, work_id, session_id, lock=False
        )
        return _session_data(session, item, include_participants=True)
    except (
        PlaytestManagementForbidden,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def _participant_session(
    session: Session, actor_id: UUID, session_id: UUID, *, lock: bool
) -> tuple[PlaytestSessionParticipant, PlaytestSession]:
    set_actor(session, actor_id)
    set_playtest_participant_lookup_scope(session, session_id)
    participant = session.scalar(
        select(PlaytestSessionParticipant).where(
            PlaytestSessionParticipant.session_id == session_id,
            PlaytestSessionParticipant.account_id == actor_id,
        )
    )
    if participant is None:
        session.rollback()
        raise PlaytestUnavailable
    set_playtest_session_scope(session, session_id)
    item_statement = select(PlaytestSession).where(PlaytestSession.id == session_id)
    if lock:
        item_statement = item_statement.with_for_update()
    item = session.scalar(item_statement)
    if item is None:
        session.rollback()
        raise PlaytestUnavailable
    return participant, item


def read_participant_session(
    session: Session, actor_id: UUID, session_id: UUID
) -> ParticipantSessionData:
    try:
        participant, item = _participant_session(
            session, actor_id, session_id, lock=False
        )
        set_playtest_session_scope(session, item.id)
        feedback = None
        if item.status != CANCELLED:
            set_feedback_participant_scope(session, item.id)
            feedback = evidence_service.list_feedback(session, item.id)
        return ParticipantSessionData(
            id=item.id,
            work_name=item.work_name,
            observation_goals=item.observation_goals,
            recording_method=item.recording_method,
            scheduled_at=item.scheduled_at,
            location=item.location,
            capacity=item.capacity,
            confirmed_count=_confirmed_count(session, item.id),
            status=item.status,
            own_status=participant.status,
            rule_name=item.rule_name,
            rule_description=item.rule_description,
            rule_content=item.rule_content,
            materials=_materials(session, item.id) if item.status != CANCELLED else (),
            latest_notification=_mail_data(session, participant.latest_outbox_id),
            feedback_items=feedback.items if feedback is not None else (),
            own_feedback=(
                feedback.submissions[0] if feedback and feedback.submissions else None
            ),
        )
    except PlaytestUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def confirm_participation(
    session: Session, actor_id: UUID, session_id: UUID
) -> ConfirmationData:
    try:
        participant, item = _participant_session(
            session, actor_id, session_id, lock=True
        )
        set_playtest_session_scope(session, item.id)
        participant = session.scalar(
            select(PlaytestSessionParticipant)
            .where(
                PlaytestSessionParticipant.id == participant.id,
                PlaytestSessionParticipant.account_id == actor_id,
            )
            .with_for_update()
        )
        if participant is None:
            session.rollback()
            raise PlaytestUnavailable
        if item.status != SCHEDULED:
            session.rollback()
            raise PlaytestSessionStateInvalid
        confirmed_count = _confirmed_count(session, item.id)
        if participant.status == CONFIRMED:
            _commit_or_rollback(session)
            return ConfirmationData(CONFIRMED, confirmed_count, item.capacity)
        if confirmed_count >= item.capacity:
            session.rollback()
            raise PlaytestCapacityExceeded
        participant.status = CONFIRMED
        complete_todo_by_source(
            session,
            actor_id,
            "playtest_invitation",
            str(participant.id),
        )
        _commit_or_rollback(session)
        return ConfirmationData(CONFIRMED, confirmed_count + 1, item.capacity)
    except (
        PlaytestCapacityExceeded,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def open_participant_material(
    session: Session, actor_id: UUID, session_id: UUID, file_id: UUID
) -> files_service.ImageStream:
    try:
        _, item = _participant_session(session, actor_id, session_id, lock=False)
        if item.status not in {SCHEDULED, STARTED}:
            session.rollback()
            raise PlaytestUnavailable
        set_playtest_session_scope(session, item.id)
        relation = session.scalar(
            select(PlaytestSessionMaterial).where(
                PlaytestSessionMaterial.session_id == item.id,
                PlaytestSessionMaterial.file_id == file_id,
            )
        )
        if relation is None:
            session.rollback()
            raise PlaytestUnavailable
        stream = files_service.open_playtest_material(session, file_id)
    except PlaytestUnavailable:
        raise
    except files_service.MaterialUnavailable as error:
        raise PlaytestUnavailable from error
    except files_service.FileOperationRetryable as error:
        raise PlaytestOperationRetryable from error
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
    return stream


def _feedback_managed_session(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    *,
    lock: bool,
) -> PlaytestSession:
    _require_management(session, actor_id, workspace_id, work_id)
    item = _load_managed_session(session, workspace_id, work_id, session_id, lock=lock)
    if item.status == CANCELLED:
        session.rollback()
        raise PlaytestUnavailable
    set_feedback_management_scope(session, item.id)
    return item


def _feedback_data(session: Session, item: PlaytestSession) -> ManagedFeedbackData:
    set_feedback_management_scope(session, item.id)
    return ManagedFeedbackData(
        session=_session_data(session, item, include_participants=True),
        actual_material_recorded=item.actual_material_recorded,
        feedback=evidence_service.list_feedback(session, item.id),
    )


def read_feedback(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
) -> ManagedFeedbackData:
    try:
        item = _feedback_managed_session(
            session, actor_id, workspace_id, work_id, session_id, lock=False
        )
        return _feedback_data(session, item)
    except (
        PlaytestManagementForbidden,
        PlaytestUnavailable,
        PlaytestOperationRetryable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def _require_feedback_mutable(item: PlaytestSession, session: Session) -> None:
    if item.status not in {SCHEDULED, STARTED}:
        session.rollback()
        raise PlaytestSessionStateInvalid


def create_feedback_item(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    operation_key: str,
    draft: evidence_service.FeedbackItemDraft,
) -> evidence_service.FeedbackItemData:
    try:
        item = _feedback_managed_session(
            session, actor_id, workspace_id, work_id, session_id, lock=True
        )
        _require_feedback_mutable(item, session)
        result = evidence_service.create_feedback_item(
            session, item.id, operation_key, draft
        )
        _commit_or_rollback(session)
        return result
    except (
        evidence_service.FeedbackInvalid,
        evidence_service.FeedbackOperationConflict,
        PlaytestManagementForbidden,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def update_feedback_item(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    item_id: UUID,
    expected_revision: int,
    draft: evidence_service.FeedbackItemDraft,
) -> evidence_service.FeedbackItemData:
    try:
        item = _feedback_managed_session(
            session, actor_id, workspace_id, work_id, session_id, lock=True
        )
        _require_feedback_mutable(item, session)
        result = evidence_service.update_feedback_item(
            session, item.id, item_id, expected_revision, draft
        )
        _commit_or_rollback(session)
        return result
    except (
        evidence_service.FeedbackInvalid,
        evidence_service.FeedbackItemLocked,
        evidence_service.FeedbackItemRevisionConflict,
        evidence_service.FeedbackItemUnavailable,
        PlaytestManagementForbidden,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def delete_feedback_item(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    item_id: UUID,
) -> None:
    try:
        item = _feedback_managed_session(
            session, actor_id, workspace_id, work_id, session_id, lock=True
        )
        _require_feedback_mutable(item, session)
        evidence_service.delete_feedback_item(session, item.id, item_id)
        _commit_or_rollback(session)
    except (
        evidence_service.FeedbackItemLocked,
        evidence_service.FeedbackItemUnavailable,
        PlaytestManagementForbidden,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def create_feedback_submission(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    operation_key: str,
    draft: evidence_service.FeedbackSubmissionDraft,
) -> evidence_service.FeedbackSubmissionData:
    try:
        item = _feedback_managed_session(
            session, actor_id, workspace_id, work_id, session_id, lock=True
        )
        _require_feedback_mutable(item, session)
        existing = session.scalar(
            select(PlaytestFeedbackSubmission.id).where(
                PlaytestFeedbackSubmission.session_id == item.id,
                PlaytestFeedbackSubmission.creation_operation_key == operation_key,
            )
        )
        result = evidence_service.create_organizer_submission(
            session, item.id, actor_id, operation_key, draft
        )
        if existing is None:
            _notify_feedback_maintainers(session, item, result.id)
        _commit_or_rollback(session)
        return result
    except (
        evidence_service.FeedbackInvalid,
        evidence_service.FeedbackOperationConflict,
        PlaytestManagementForbidden,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def update_feedback_submission(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    submission_id: UUID,
    expected_revision: int,
    draft: evidence_service.FeedbackSubmissionDraft,
) -> evidence_service.FeedbackSubmissionData:
    try:
        item = _feedback_managed_session(
            session, actor_id, workspace_id, work_id, session_id, lock=True
        )
        _require_feedback_mutable(item, session)
        result = evidence_service.update_organizer_submission(
            session,
            item.id,
            submission_id,
            actor_id,
            expected_revision,
            draft,
        )
        _commit_or_rollback(session)
        return result
    except (
        evidence_service.FeedbackInvalid,
        evidence_service.FeedbackSubmissionForbidden,
        evidence_service.FeedbackSubmissionRevisionConflict,
        evidence_service.FeedbackSubmissionUnavailable,
        PlaytestManagementForbidden,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error


def save_participant_feedback(
    session: Session,
    actor_id: UUID,
    session_id: UUID,
    operation_key: str | None,
    expected_revision: int | None,
    draft: evidence_service.FeedbackSubmissionDraft,
) -> evidence_service.FeedbackSubmissionData:
    try:
        _, item = _participant_session(session, actor_id, session_id, lock=True)
        if item.status != STARTED:
            session.rollback()
            raise PlaytestSessionStateInvalid
        set_feedback_participant_scope(session, item.id)
        previous = session.scalar(
            select(PlaytestFeedbackSubmission).where(
                PlaytestFeedbackSubmission.session_id == item.id,
                PlaytestFeedbackSubmission.direct_author_account_id == actor_id,
            )
        )
        was_submitted = previous is not None and previous.status == "submitted"
        result = evidence_service.save_direct_submission(
            session,
            item.id,
            actor_id,
            operation_key,
            expected_revision,
            draft,
        )
        if result.status == "submitted" and not was_submitted:
            _notify_feedback_maintainers(session, item, result.id)
        _commit_or_rollback(session)
        return result
    except IntegrityError as error:
        session.rollback()
        if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == (
            "ck_linked_feedback_submission_status"
        ):
            raise evidence_service.FeedbackSubmissionLinked from error
        raise PlaytestOperationRetryable from error
    except (
        evidence_service.FeedbackInvalid,
        evidence_service.FeedbackOperationConflict,
        evidence_service.FeedbackSubmissionLinked,
        evidence_service.FeedbackSubmissionRevisionConflict,
        PlaytestSessionStateInvalid,
        PlaytestUnavailable,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise PlaytestOperationRetryable from error
