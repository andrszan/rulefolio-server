from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.access.context import (
    set_actor,
    set_playtest_management_scope,
    set_work_management_scope,
)
from app.evidence import service as evidence_service
from app.issues.models import Issue, IssueEvidenceLink, IssueRetestLink
from app.notifications.models import NotificationTodo
from app.notifications.service import (
    cancel_todo_by_source,
    cancel_todos_for_target,
    complete_todos_for_target,
    create_todo,
    enqueue_business_mail,
)
from app.playtests.models import PlaytestSession
from app.works import service as works_service
from app.works.models import Work
from app.workspaces import service as workspaces_service

DECISIONS = frozenset({"modify", "observe", "reject"})
STATUSES = frozenset({"open", "closed"})
CONCLUSIONS = frozenset(
    {"verified", "continue_observing", "adjust_again", "insufficient_evidence"}
)


class IssueUnavailable(Exception):
    pass


class IssueManagementForbidden(Exception):
    pass


class IssueInvalid(Exception):
    pass


class IssueRevisionConflict(Exception):
    pass


class IssueOperationConflict(Exception):
    pass


class IssueEvidenceAlreadyLinked(Exception):
    pass


class IssueLastEvidenceRequired(Exception):
    pass


class IssueOperationRetryable(Exception):
    pass


class IssueRetestResultRequired(Exception):
    pass


class IssueRetestEvidenceRequired(Exception):
    pass


@dataclass(frozen=True)
class IssueConclusionData:
    retest_id: UUID
    conclusion: str
    reason: str


@dataclass(frozen=True)
class IssueRetestData:
    id: UUID
    plan_id: UUID
    session_id: UUID
    scheduled_at: datetime
    location: str
    status: str
    rule_name: str
    actual_material_recorded: bool
    current_adjustment: bool
    conclusion: str | None
    conclusion_reason: str | None


@dataclass(frozen=True)
class IssueData:
    id: UUID
    description: str
    decision: str
    reason: str
    adjustment_note: str | None
    verification_status: str
    current_conclusion: IssueConclusionData | None
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime
    source_count: int


@dataclass(frozen=True)
class OverviewIssueData:
    id: UUID
    description: str
    decision: str
    status: str
    verification_status: str
    current_conclusion_type: str | None
    current_conclusion_session_id: UUID | None


@dataclass(frozen=True)
class IssueEvidenceData:
    link_id: UUID
    source: evidence_service.IssueEvidenceSourceData


def _now() -> datetime:
    return datetime.now(UTC)


def _commit_or_rollback(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _required_text(value: str, limit: int) -> str:
    if not isinstance(value, str):
        raise IssueInvalid
    value = value.strip()
    if not value or len(value) > limit:
        raise IssueInvalid
    return value


def _optional_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IssueInvalid
    value = value.strip()
    if len(value) > limit:
        raise IssueInvalid
    return value or None


def _values(
    description: str, decision: str, reason: str, status: str
) -> tuple[str, str, str, str]:
    description = _required_text(description, 4_000)
    reason = _required_text(reason, 4_000)
    if (
        not isinstance(decision, str)
        or not isinstance(status, str)
        or decision not in DECISIONS
        or status not in STATUSES
    ):
        raise IssueInvalid
    return description, decision, reason, status


def _require_management(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    *,
    writable: bool = False,
) -> None:
    set_actor(session, actor_id)
    if writable:
        workspaces_service.ensure_workspace_writable(session, workspace_id)
    try:
        works_service.ensure_work_management(session, actor_id, workspace_id, work_id)
    except works_service.WorkManagementForbidden as error:
        raise IssueManagementForbidden from error
    except works_service.WorkUnavailable as error:
        raise IssueUnavailable from error
    except works_service.WorkOperationRetryable as error:
        raise IssueOperationRetryable from error


def _load_issue(session: Session, issue_id: UUID, *, lock: bool) -> Issue:
    statement = select(Issue).where(Issue.id == issue_id)
    if lock:
        statement = statement.with_for_update()
    issue = session.scalar(statement)
    if issue is None:
        session.rollback()
        raise IssueUnavailable
    return issue


def _references_for_issue(
    session: Session, issue_id: UUID
) -> tuple[evidence_service.IssueEvidenceReference, ...]:
    links = session.scalars(
        select(IssueEvidenceLink)
        .where(IssueEvidenceLink.issue_id == issue_id)
        .order_by(IssueEvidenceLink.id)
    )
    result: list[evidence_service.IssueEvidenceReference] = []
    for link in links:
        if link.observation_id is not None:
            result.append(
                evidence_service.IssueEvidenceReference(
                    source_type="observation", source_id=link.observation_id
                )
            )
        elif link.feedback_submission_id is not None:
            result.append(
                evidence_service.IssueEvidenceReference(
                    source_type="feedback_submission",
                    source_id=link.feedback_submission_id,
                )
            )
        else:
            raise IssueUnavailable
    return tuple(result)


def _source_count(session: Session, issue_id: UUID) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(IssueEvidenceLink)
            .where(IssueEvidenceLink.issue_id == issue_id)
        )
        or 0
    )


def _current_conclusion(session: Session, issue: Issue) -> IssueConclusionData | None:
    set_work_management_scope(session, issue.work_id, issue.workspace_id)
    link = session.scalar(
        select(IssueRetestLink)
        .where(
            IssueRetestLink.issue_id == issue.id,
            IssueRetestLink.adjustment_generation == issue.adjustment_generation,
            IssueRetestLink.conclusion.is_not(None),
        )
        .order_by(IssueRetestLink.id)
    )
    if link is None:
        return None
    if link.conclusion_reason is None:
        raise IssueUnavailable
    return IssueConclusionData(
        retest_id=link.id, conclusion=link.conclusion, reason=link.conclusion_reason
    )


def _data(session: Session, issue: Issue, source_count: int) -> IssueData:
    current_conclusion = _current_conclusion(session, issue)
    verification_status = (
        current_conclusion.conclusion
        if current_conclusion is not None
        else "pending"
        if issue.adjustment_note is not None
        else "not_recorded"
    )
    return IssueData(
        id=issue.id,
        description=issue.description,
        decision=issue.decision,
        reason=issue.reason,
        adjustment_note=issue.adjustment_note,
        verification_status=verification_status,
        current_conclusion=current_conclusion,
        status=issue.status,
        revision=issue.revision,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        source_count=source_count,
    )


def _retest_data(
    link: IssueRetestLink, item: PlaytestSession, adjustment_generation: int
) -> IssueRetestData:
    return IssueRetestData(
        id=link.id,
        plan_id=item.plan_id,
        session_id=item.id,
        scheduled_at=item.scheduled_at,
        location=item.location,
        status=item.status,
        rule_name=item.rule_name,
        actual_material_recorded=item.actual_material_recorded,
        current_adjustment=link.adjustment_generation == adjustment_generation,
        conclusion=link.conclusion,
        conclusion_reason=link.conclusion_reason,
    )


def _add_links(
    session: Session,
    issue_id: UUID,
    references: tuple[evidence_service.IssueEvidenceReference, ...],
) -> None:
    session.add_all(
        IssueEvidenceLink(
            issue_id=issue_id,
            observation_id=(
                reference.source_id if reference.source_type == "observation" else None
            ),
            feedback_submission_id=(
                reference.source_id
                if reference.source_type == "feedback_submission"
                else None
            ),
        )
        for reference in references
    )
    session.flush()


def _lock_work_for_creation(
    session: Session, workspace_id: UUID, work_id: UUID
) -> None:
    work = session.scalar(
        select(Work)
        .where(Work.id == work_id, Work.workspace_id == workspace_id)
        .with_for_update()
    )
    if work is None:
        session.rollback()
        raise IssueUnavailable


def notification_todo_eligible(session: Session, todo: NotificationTodo) -> bool:
    if todo.work_id is None:
        return False
    work_id = todo.work_id
    workspace_id = todo.workspace_id
    recipient_account_id = todo.recipient_account_id
    kind = todo.kind
    target_kind = todo.target_kind
    target_id = todo.target_id
    set_work_management_scope(session, work_id, workspace_id)
    if recipient_account_id not in workspaces_service.current_work_maintainer_ids(
        session, work_id
    ):
        return False
    if kind == "feedback_submitted":
        return target_kind == "playtest_session"
    if kind in {"issue_opened", "retest_arrangement_needed"}:
        issue = session.scalar(
            select(Issue).where(
                Issue.id == target_id,
                Issue.workspace_id == workspace_id,
                Issue.work_id == work_id,
                Issue.status == "open",
            )
        )
        return issue is not None
    if kind == "retest_arranged" and target_kind == "issue":
        try:
            link_id = UUID(todo.source_key)
        except ValueError:
            return False
        set_playtest_management_scope(session, workspace_id, work_id)
        link = session.scalar(
            select(IssueRetestLink)
            .join(PlaytestSession, PlaytestSession.id == IssueRetestLink.session_id)
            .where(
                IssueRetestLink.id == link_id,
                IssueRetestLink.issue_id == target_id,
                PlaytestSession.workspace_id == workspace_id,
                PlaytestSession.work_id == work_id,
                PlaytestSession.status != "cancelled",
            )
        )
        return link is not None
    return False


def _notify_issue_maintainers(session: Session, issue: Issue) -> None:
    source_key = f"{issue.id}:open:{issue.revision}"
    for account_id in workspaces_service.current_work_maintainer_ids(
        session, issue.work_id
    ):
        todo, created = create_todo(
            session,
            recipient_account_id=account_id,
            workspace_id=issue.workspace_id,
            work_id=issue.work_id,
            kind="issue_opened",
            target_kind="issue",
            target_id=issue.id,
            source_key=source_key,
            summary="有新的问题需要处理",
            context_label=f"问题：{issue.description[:197]}",
        )
        if created:
            enqueue_business_mail(
                session,
                account_id,
                "issue_opened",
                "有新的问题需要处理",
                "有新的作品问题需要处理，请登录 Rulefolio 查看。",
                business_scope=f"issue:{issue.id}",
                todo_id=todo.id,
            )


def _notify_retest_needed(session: Session, issue: Issue) -> None:
    source_key = f"{issue.id}:adjustment:{issue.adjustment_generation}"
    for account_id in workspaces_service.current_work_maintainer_ids(
        session, issue.work_id
    ):
        todo, created = create_todo(
            session,
            recipient_account_id=account_id,
            workspace_id=issue.workspace_id,
            work_id=issue.work_id,
            kind="retest_arrangement_needed",
            target_kind="issue",
            target_id=issue.id,
            source_key=source_key,
            summary="需要安排针对性复测",
            context_label=f"问题：{issue.description[:197]}",
        )
        if created:
            enqueue_business_mail(
                session,
                account_id,
                "retest_arrangement_needed",
                "需要安排针对性复测",
                "作品问题的当前调整需要安排针对性复测，请登录 Rulefolio 查看。",
                business_scope=f"issue-retest-needed:{issue.id}:{issue.adjustment_generation}",
                todo_id=todo.id,
            )


def _notify_retest_arranged(
    session: Session, issue: Issue, link: IssueRetestLink, creator_id: UUID
) -> None:
    for account_id in workspaces_service.current_work_maintainer_ids(
        session, issue.work_id
    ):
        if account_id == creator_id:
            continue
        todo, created = create_todo(
            session,
            recipient_account_id=account_id,
            workspace_id=issue.workspace_id,
            work_id=issue.work_id,
            kind="retest_arranged",
            target_kind="issue",
            target_id=issue.id,
            source_key=str(link.id),
            summary="已安排针对性复测",
            context_label=f"问题：{issue.description[:197]}",
        )
        if created:
            enqueue_business_mail(
                session,
                account_id,
                "retest_arranged",
                "已安排针对性复测",
                "其他维护者已安排针对性复测，请登录 Rulefolio 查看。",
                business_scope=f"issue-retest:{link.id}",
                todo_id=todo.id,
            )


def cancel_retest_arranged_for_session(
    session: Session, workspace_id: UUID, work_id: UUID, session_id: UUID
) -> None:
    set_work_management_scope(session, work_id, workspace_id)
    links = list(
        session.scalars(
            select(IssueRetestLink)
            .join(Issue, Issue.id == IssueRetestLink.issue_id)
            .where(
                IssueRetestLink.session_id == session_id,
                Issue.workspace_id == workspace_id,
                Issue.work_id == work_id,
            )
        )
    )
    recipient_ids = workspaces_service.current_work_maintainer_ids(session, work_id)
    for link in links:
        for recipient_id in recipient_ids:
            cancel_todo_by_source(
                session, recipient_id, "retest_arranged", str(link.id)
            )


def list_issues(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    page: int,
    size: int,
) -> tuple[list[IssueData], int]:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        source_counts = (
            select(
                IssueEvidenceLink.issue_id.label("issue_id"),
                func.count().label("source_count"),
            )
            .group_by(IssueEvidenceLink.issue_id)
            .subquery()
        )
        total = (
            session.scalar(
                select(func.count())
                .select_from(Issue)
                .where(Issue.workspace_id == workspace_id, Issue.work_id == work_id)
            )
            or 0
        )
        rows = session.execute(
            select(Issue, func.coalesce(source_counts.c.source_count, 0))
            .outerjoin(source_counts, source_counts.c.issue_id == Issue.id)
            .where(Issue.workspace_id == workspace_id, Issue.work_id == work_id)
            .order_by(Issue.updated_at.desc(), Issue.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    except (IssueManagementForbidden, IssueUnavailable, IssueOperationRetryable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return [_data(session, issue, source_count) for issue, source_count in rows], total


def list_overview_issues(
    session: Session,
    workspace_id: UUID,
    work_id: UUID,
    kind: str,
    page: int,
    size: int,
) -> tuple[list[OverviewIssueData], int]:
    if kind not in {"pending-retest", "needs-action"}:
        raise IssueInvalid
    current_link = aliased(IssueRetestLink)
    current_link_condition = (
        (current_link.issue_id == Issue.id)
        & (current_link.adjustment_generation == Issue.adjustment_generation)
        & current_link.conclusion.is_not(None)
    )
    pending_retest = Issue.adjustment_note.is_not(None) & current_link.conclusion.is_(
        None
    )
    verification_status = case(
        (current_link.conclusion.is_not(None), current_link.conclusion),
        (Issue.adjustment_note.is_not(None), literal("pending")),
        else_=literal("not_recorded"),
    ).label("verification_status")
    conditions = [
        Issue.workspace_id == workspace_id,
        Issue.work_id == work_id,
        Issue.status == "open",
        pending_retest
        if kind == "pending-retest"
        else or_(Issue.adjustment_note.is_(None), current_link.conclusion.is_not(None)),
    ]
    try:
        total = (
            session.scalar(
                select(func.count())
                .select_from(Issue)
                .outerjoin(current_link, current_link_condition)
                .where(*conditions)
            )
            or 0
        )
        rows = session.execute(
            select(
                Issue.id,
                Issue.description,
                Issue.decision,
                Issue.status,
                verification_status,
                current_link.conclusion,
                current_link.session_id,
            )
            .outerjoin(current_link, current_link_condition)
            .where(*conditions)
            .order_by(Issue.updated_at.desc(), Issue.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return [
        OverviewIssueData(
            id=row.id,
            description=row.description,
            decision=row.decision,
            status=row.status,
            verification_status=row.verification_status,
            current_conclusion_type=row.conclusion,
            current_conclusion_session_id=row.session_id,
        )
        for row in rows
    ], total


def create_issue(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    operation_key: str,
    *,
    description: str,
    decision: str,
    reason: str,
    status: str,
    references: tuple[evidence_service.IssueEvidenceReference, ...],
) -> IssueData:
    description, decision, reason, status = _values(
        description, decision, reason, status
    )
    if not operation_key or len(operation_key) > 128:
        raise IssueInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id, writable=True)
        _lock_work_for_creation(session, workspace_id, work_id)
        existing = session.scalar(
            select(Issue).where(
                Issue.work_id == work_id,
                Issue.creation_operation_key == operation_key,
            )
        )
        if existing is not None:
            existing_references = _references_for_issue(session, existing.id)
            if (
                existing.description != description
                or existing.decision != decision
                or existing.reason != reason
                or existing.status != status
                or set(existing_references) != set(references)
            ):
                session.rollback()
                raise IssueOperationConflict
            return _data(session, existing, len(existing_references))
        references = evidence_service.validate_issue_evidence_sources(
            session, workspace_id, work_id, references
        )
        issue = Issue(
            workspace_id=workspace_id,
            work_id=work_id,
            description=description,
            decision=decision,
            reason=reason,
            status=status,
            creation_operation_key=operation_key,
        )
        session.add(issue)
        session.flush()
        _add_links(session, issue.id, references)
        if issue.status == "open":
            _notify_issue_maintainers(session, issue)
        _commit_or_rollback(session)
    except evidence_service.IssueEvidenceInvalid as error:
        session.rollback()
        raise IssueInvalid from error
    except evidence_service.IssueEvidenceUnavailable as error:
        session.rollback()
        raise IssueUnavailable from error
    except (
        IssueInvalid,
        IssueManagementForbidden,
        IssueOperationConflict,
        IssueUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return _data(session, issue, len(references))


def read_issue(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
) -> IssueData:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        issue = _load_issue(session, issue_id, lock=False)
        return _data(session, issue, _source_count(session, issue.id))
    except (IssueManagementForbidden, IssueUnavailable, IssueOperationRetryable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error


def update_issue(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    *,
    description: str,
    decision: str,
    reason: str,
    adjustment_note: str | None,
    status: str,
    expected_revision: int,
) -> IssueData:
    description, decision, reason, status = _values(
        description, decision, reason, status
    )
    adjustment_note = _optional_text(adjustment_note, 4_000)
    if expected_revision <= 0:
        raise IssueInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id, writable=True)
        issue = _load_issue(session, issue_id, lock=True)
        if issue.revision != expected_revision:
            session.rollback()
            raise IssueRevisionConflict
        was_open = issue.status == "open"
        adjustment_changed = adjustment_note != issue.adjustment_note
        issue.description = description
        issue.decision = decision
        issue.reason = reason
        issue.adjustment_note = adjustment_note
        issue.status = status
        issue.revision += 1
        issue.updated_at = _now()
        if adjustment_changed:
            issue.adjustment_generation += 1
            for link in session.scalars(
                select(IssueRetestLink)
                .where(IssueRetestLink.issue_id == issue.id)
                .with_for_update()
            ):
                link.conclusion = None
                link.conclusion_reason = None
                link.updated_at = _now()
        session.flush()
        if issue.status == "closed":
            complete_todos_for_target(
                session, "issue", issue.id, kinds={"issue_opened"}
            )
            cancel_todos_for_target(
                session, "issue", issue.id, kinds={"retest_arrangement_needed"}
            )
        else:
            if not was_open:
                _notify_issue_maintainers(session, issue)
            if adjustment_changed:
                cancel_todos_for_target(
                    session,
                    "issue",
                    issue.id,
                    kinds={"retest_arrangement_needed"},
                )
            if issue.adjustment_note is not None and (
                adjustment_changed or not was_open
            ):
                _notify_retest_needed(session, issue)
        source_count = _source_count(session, issue.id)
        _commit_or_rollback(session)
    except (
        IssueInvalid,
        IssueManagementForbidden,
        IssueRevisionConflict,
        IssueUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return _data(session, issue, source_count)


def bind_retest_session(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    session_id: UUID,
    expected_revision: int,
) -> IssueRetestLink:
    if expected_revision <= 0:
        raise IssueInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id, writable=True)
        issue = _load_issue(session, issue_id, lock=True)
        if issue.revision != expected_revision:
            session.rollback()
            raise IssueRevisionConflict
        if issue.adjustment_note is None:
            session.rollback()
            raise IssueInvalid
        set_playtest_management_scope(session, workspace_id, work_id)
        item = session.scalar(
            select(PlaytestSession)
            .where(
                PlaytestSession.id == session_id,
                PlaytestSession.workspace_id == workspace_id,
                PlaytestSession.work_id == work_id,
            )
            .with_for_update()
        )
        if item is None:
            session.rollback()
            raise IssueUnavailable
        link = IssueRetestLink(
            issue_id=issue.id,
            session_id=item.id,
            adjustment_generation=issue.adjustment_generation,
        )
        session.add(link)
        issue.revision += 1
        issue.updated_at = _now()
        session.flush()
        complete_todos_for_target(
            session,
            "issue",
            issue.id,
            kinds={"retest_arrangement_needed"},
        )
        _notify_retest_arranged(session, issue, link, actor_id)
    except (
        IssueInvalid,
        IssueManagementForbidden,
        IssueRevisionConflict,
        IssueUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return link


def list_retests(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    page: int,
    size: int,
) -> tuple[list[IssueRetestData], int]:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        issue = _load_issue(session, issue_id, lock=False)
        set_playtest_management_scope(session, workspace_id, work_id)
        total = (
            session.scalar(
                select(func.count())
                .select_from(IssueRetestLink)
                .where(IssueRetestLink.issue_id == issue.id)
            )
            or 0
        )
        rows = session.execute(
            select(IssueRetestLink, PlaytestSession)
            .join(PlaytestSession, PlaytestSession.id == IssueRetestLink.session_id)
            .where(IssueRetestLink.issue_id == issue.id)
            .order_by(PlaytestSession.scheduled_at.desc(), PlaytestSession.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    except (IssueManagementForbidden, IssueUnavailable, IssueOperationRetryable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return [
        _retest_data(link, item, issue.adjustment_generation) for link, item in rows
    ], total


def save_retest_conclusion(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    retest_id: UUID,
    *,
    conclusion: str,
    reason: str,
    status: str,
    expected_revision: int,
) -> IssueData:
    reason = _required_text(reason, 4_000)
    if (
        not isinstance(conclusion, str)
        or conclusion not in CONCLUSIONS
        or not isinstance(status, str)
        or status not in STATUSES
        or expected_revision <= 0
    ):
        raise IssueInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id, writable=True)
        issue = _load_issue(session, issue_id, lock=True)
        if issue.revision != expected_revision:
            session.rollback()
            raise IssueRevisionConflict
        retest = session.scalar(
            select(IssueRetestLink)
            .where(
                IssueRetestLink.id == retest_id, IssueRetestLink.issue_id == issue.id
            )
            .with_for_update()
        )
        if retest is None:
            session.rollback()
            raise IssueUnavailable
        if retest.adjustment_generation != issue.adjustment_generation:
            session.rollback()
            raise IssueInvalid
        set_playtest_management_scope(session, workspace_id, work_id)
        item = session.scalar(
            select(PlaytestSession).where(
                PlaytestSession.id == retest.session_id,
                PlaytestSession.workspace_id == workspace_id,
                PlaytestSession.work_id == work_id,
            )
        )
        if item is None:
            session.rollback()
            raise IssueUnavailable
        if item.status != "started" or not item.actual_material_recorded:
            session.rollback()
            raise IssueRetestResultRequired
        if (
            conclusion != "insufficient_evidence"
            and not evidence_service.has_issue_evidence_from_session(
                session, _references_for_issue(session, issue.id), item.id
            )
        ):
            session.rollback()
            raise IssueRetestEvidenceRequired
        previous = list(
            session.scalars(
                select(IssueRetestLink)
                .where(
                    IssueRetestLink.issue_id == issue.id,
                    IssueRetestLink.id != retest.id,
                    IssueRetestLink.conclusion.is_not(None),
                )
                .with_for_update()
            )
        )
        for link in previous:
            link.conclusion = None
            link.conclusion_reason = None
            link.updated_at = _now()
        session.flush()
        retest.conclusion = conclusion
        retest.conclusion_reason = reason
        retest.updated_at = _now()
        issue.status = status
        issue.revision += 1
        issue.updated_at = _now()
        session.flush()
        source_count = _source_count(session, issue.id)
        _commit_or_rollback(session)
    except (
        IssueInvalid,
        IssueManagementForbidden,
        IssueRetestEvidenceRequired,
        IssueRetestResultRequired,
        IssueRevisionConflict,
        IssueUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return _data(session, issue, source_count)


def add_issue_evidence(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    expected_revision: int,
    reference: evidence_service.IssueEvidenceReference,
) -> IssueData:
    if expected_revision <= 0:
        raise IssueInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id, writable=True)
        issue = _load_issue(session, issue_id, lock=True)
        if issue.revision != expected_revision:
            session.rollback()
            raise IssueRevisionConflict
        references = evidence_service.validate_issue_evidence_sources(
            session, workspace_id, work_id, (reference,)
        )
        existing = set(_references_for_issue(session, issue.id))
        if references[0] in existing:
            session.rollback()
            raise IssueEvidenceAlreadyLinked
        _add_links(session, issue.id, references)
        issue.revision += 1
        issue.updated_at = _now()
        session.flush()
        source_count = len(existing) + 1
        _commit_or_rollback(session)
    except evidence_service.IssueEvidenceInvalid as error:
        session.rollback()
        raise IssueInvalid from error
    except evidence_service.IssueEvidenceUnavailable as error:
        session.rollback()
        raise IssueUnavailable from error
    except (
        IssueEvidenceAlreadyLinked,
        IssueInvalid,
        IssueManagementForbidden,
        IssueRevisionConflict,
        IssueUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return _data(session, issue, source_count)


def remove_issue_evidence(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    link_id: UUID,
    expected_revision: int,
) -> IssueData:
    if expected_revision <= 0:
        raise IssueInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id, writable=True)
        issue = _load_issue(session, issue_id, lock=True)
        if issue.revision != expected_revision:
            session.rollback()
            raise IssueRevisionConflict
        links = list(
            session.scalars(
                select(IssueEvidenceLink)
                .where(IssueEvidenceLink.issue_id == issue.id)
                .order_by(IssueEvidenceLink.id)
            )
        )
        target = next((link for link in links if link.id == link_id), None)
        if target is None:
            session.rollback()
            raise IssueUnavailable
        if len(links) == 1:
            session.rollback()
            raise IssueLastEvidenceRequired
        session.delete(target)
        issue.revision += 1
        issue.updated_at = _now()
        session.flush()
        source_count = len(links) - 1
        _commit_or_rollback(session)
    except (
        IssueInvalid,
        IssueLastEvidenceRequired,
        IssueManagementForbidden,
        IssueRevisionConflict,
        IssueUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    return _data(session, issue, source_count)


def list_issue_evidence(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    page: int,
    size: int,
) -> tuple[list[IssueEvidenceData], int]:
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        issue = _load_issue(session, issue_id, lock=False)
        links = list(
            session.scalars(
                select(IssueEvidenceLink)
                .where(IssueEvidenceLink.issue_id == issue.id)
                .order_by(IssueEvidenceLink.id)
            )
        )
        references = _references_for_issue(session, issue.id)
        sources = evidence_service.read_linked_issue_evidence(
            session, issue.id, references
        )
    except evidence_service.IssueEvidenceUnavailable as error:
        session.rollback()
        raise IssueUnavailable from error
    except (IssueManagementForbidden, IssueUnavailable, IssueOperationRetryable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise IssueOperationRetryable from error
    link_ids: dict[tuple[str, UUID], UUID] = {}
    for link in links:
        if link.observation_id is not None:
            link_ids[("observation", link.observation_id)] = link.id
        elif link.feedback_submission_id is not None:
            link_ids[("feedback_submission", link.feedback_submission_id)] = link.id
        else:
            raise IssueUnavailable
    items = [
        IssueEvidenceData(
            link_id=link_ids[(source.source_type, source.source_id)], source=source
        )
        for source in sources
    ]
    return items[(page - 1) * size : page * size], len(items)
