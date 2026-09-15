from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import set_actor
from app.evidence import service as evidence_service
from app.issues.models import Issue, IssueEvidenceLink
from app.works import service as works_service
from app.works.models import Work

DECISIONS = frozenset({"modify", "observe", "reject"})
STATUSES = frozenset({"open", "closed"})


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


@dataclass(frozen=True)
class IssueData:
    id: UUID
    description: str
    decision: str
    reason: str
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime
    source_count: int


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
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> None:
    set_actor(session, actor_id)
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


def _data(issue: Issue, source_count: int) -> IssueData:
    return IssueData(
        id=issue.id,
        description=issue.description,
        decision=issue.decision,
        reason=issue.reason,
        status=issue.status,
        revision=issue.revision,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        source_count=source_count,
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
    return [_data(issue, source_count) for issue, source_count in rows], total


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
        _require_management(session, actor_id, workspace_id, work_id)
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
            return _data(existing, len(existing_references))
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
    return _data(issue, len(references))


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
        return _data(issue, _source_count(session, issue.id))
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
    status: str,
    expected_revision: int,
) -> IssueData:
    description, decision, reason, status = _values(
        description, decision, reason, status
    )
    if expected_revision <= 0:
        raise IssueInvalid
    try:
        _require_management(session, actor_id, workspace_id, work_id)
        issue = _load_issue(session, issue_id, lock=True)
        if issue.revision != expected_revision:
            session.rollback()
            raise IssueRevisionConflict
        issue.description = description
        issue.decision = decision
        issue.reason = reason
        issue.status = status
        issue.revision += 1
        issue.updated_at = _now()
        session.flush()
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
    return _data(issue, source_count)


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
        _require_management(session, actor_id, workspace_id, work_id)
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
    return _data(issue, source_count)


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
        _require_management(session, actor_id, workspace_id, work_id)
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
    return _data(issue, source_count)


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
