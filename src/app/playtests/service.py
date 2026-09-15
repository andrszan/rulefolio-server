from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import (
    set_actor,
    set_file_lifecycle_scope,
    set_playtest_management_scope,
    set_playtest_participant_lookup_scope,
    set_playtest_session_scope,
)
from app.core.config import settings
from app.files import service as files_service
from app.files.models import StoredFile
from app.identity import service as identity_service
from app.identity.models import Account
from app.notifications.models import MailOutbox
from app.notifications.service import enqueue_business_mail, suppress_business_mails
from app.playtests.models import (
    PlaytestPlan,
    PlaytestSession,
    PlaytestSessionMaterial,
    PlaytestSessionParticipant,
)
from app.works import service as works_service

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


@dataclass(frozen=True)
class ConfirmationData:
    status: str
    confirmed_count: int
    capacity: int


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
        outbox = enqueue_business_mail(
            session,
            participant.account_id,
            purpose,
            subject,
            body,
            business_scope=_business_scope(item.id),
        )
        participant.latest_outbox_id = outbox.id


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
        participant = PlaytestSessionParticipant(
            session_id=item.id, account_id=account.id
        )
        session.add(participant)
        session.flush()
        subject, body = _mail_content(item.work_name, "playtest_invitation", item.id)
        outbox = enqueue_business_mail(
            session,
            account.id,
            "playtest_invitation",
            subject,
            body,
            business_scope=_business_scope(item.id),
        )
        participant.latest_outbox_id = outbox.id
    session.add_all(
        PlaytestSessionMaterial(session_id=item.id, file_id=file.id, sha256=file.sha256)
        for file in snapshot.materials
    )
    session.flush()
    return item


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
) -> PlanData:
    observation_goals = observation_goals.strip()
    recording_method = recording_method.strip()
    if not observation_goals or not recording_method or not drafts:
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
        for draft in drafts:
            _create_session(session, actor_id, workspace_id, work_id, plan, draft)
        _commit_or_rollback(session)
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
