from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import set_work_management_scope
from app.audit.models import SecurityAudit
from app.works.models import Work
from app.workspaces import service as workspaces_service

WORK_ROLES = frozenset({"maintainer", "organizer", "collaborator"})
MAINTAINER = "maintainer"


class WorkspaceUnavailable(Exception):
    pass


class WorkUnavailable(Exception):
    pass


class WorkManagementForbidden(Exception):
    pass


class WorkRevisionConflict(Exception):
    pass


class WorkLastMaintainerRequired(Exception):
    pass


class WorkAccessMemberUnavailable(Exception):
    pass


class WorkAccessUnavailable(Exception):
    pass


class WorkOperationRetryable(Exception):
    pass


@dataclass(frozen=True)
class WorkData:
    id: UUID
    workspace_id: UUID
    name: str
    description: str
    creative_stage: str
    target_experience: str
    min_players: int
    max_players: int
    estimated_duration_minutes: int
    revision: int
    own_role: str
    can_manage_access: bool


@dataclass(frozen=True)
class WorkAccessData:
    account_id: UUID
    role: str


@dataclass(frozen=True)
class WorkAccessMemberData:
    account_id: UUID
    email: str
    role: str | None


def _commit_or_rollback(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _audit_scope(workspace_id: UUID, work_id: UUID) -> str:
    return f"workspace:{workspace_id}/work:{work_id}"


def _work_data(work: Work, own_role: str) -> WorkData:
    return WorkData(
        id=work.id,
        workspace_id=work.workspace_id,
        name=work.name,
        description=work.description,
        creative_stage=work.creative_stage,
        target_experience=work.target_experience,
        min_players=work.min_players,
        max_players=work.max_players,
        estimated_duration_minutes=work.estimated_duration_minutes,
        revision=work.revision,
        own_role=own_role,
        can_manage_access=own_role == MAINTAINER,
    )


def _validate_work_values(
    name: str,
    description: str,
    creative_stage: str,
    target_experience: str,
    min_players: int,
    max_players: int,
    estimated_duration_minutes: int,
) -> tuple[str, str, str, str]:
    values = tuple(
        value.strip()
        for value in (name, description, creative_stage, target_experience)
    )
    if not all(values) or min_players <= 0 or max_players < min_players:
        raise ValueError("作品资料不符合约束")
    if estimated_duration_minutes <= 0:
        raise ValueError("作品资料不符合约束")
    return values


def _require_workspace_member(
    session: Session, workspace_id: UUID, account_id: UUID
) -> None:
    if not workspaces_service.has_workspace_member(session, workspace_id, account_id):
        session.rollback()
        raise WorkspaceUnavailable


def _load_visible_work(session: Session, workspace_id: UUID, work_id: UUID) -> Work:
    work = session.scalar(
        select(Work).where(Work.id == work_id, Work.workspace_id == workspace_id)
    )
    if work is None:
        session.rollback()
        raise WorkUnavailable
    return work


def _own_access_role(session: Session, work_id: UUID, account_id: UUID) -> str | None:
    return workspaces_service.work_access_role(session, work_id, account_id)


def _require_manager(
    session: Session, workspace_id: UUID, work_id: UUID, actor_id: UUID
) -> Work:
    work = _load_visible_work(session, workspace_id, work_id)
    if _own_access_role(session, work.id, actor_id) != MAINTAINER:
        session.rollback()
        raise WorkManagementForbidden
    set_work_management_scope(session, work.id, work.workspace_id)
    return work


def _prepare_manager_write(
    session: Session,
    workspace_id: UUID,
    work_id: UUID,
    actor_id: UUID,
    target_account_id: UUID | None = None,
) -> tuple[Work, list[workspaces_service.WorkAccessRecord]]:
    work = _require_manager(session, workspace_id, work_id, actor_id)
    member_ids = workspaces_service.lock_work_members(
        session,
        work.workspace_id,
        {actor_id} if target_account_id is None else {actor_id, target_account_id},
    )
    if actor_id not in member_ids:
        session.rollback()
        raise WorkManagementForbidden
    if target_account_id is not None and target_account_id not in member_ids:
        session.rollback()
        raise WorkAccessMemberUnavailable
    accesses = workspaces_service.lock_work_accesses(session, work.id)
    own_access = next(
        (access for access in accesses if access.account_id == actor_id), None
    )
    if own_access is None or own_access.role != MAINTAINER:
        session.rollback()
        raise WorkManagementForbidden
    return work, accesses


def create_work(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    *,
    name: str,
    description: str,
    creative_stage: str,
    target_experience: str,
    min_players: int,
    max_players: int,
    estimated_duration_minutes: int,
) -> WorkData:
    name, description, creative_stage, target_experience = _validate_work_values(
        name,
        description,
        creative_stage,
        target_experience,
        min_players,
        max_players,
        estimated_duration_minutes,
    )
    try:
        _require_workspace_member(session, workspace_id, actor_id)
        work = Work(
            id=uuid4(),
            workspace_id=workspace_id,
            name=name,
            description=description,
            creative_stage=creative_stage,
            target_experience=target_experience,
            min_players=min_players,
            max_players=max_players,
            estimated_duration_minutes=estimated_duration_minutes,
        )
        session.add(work)
        set_work_management_scope(session, work.id, work.workspace_id)
        session.flush()
        if actor_id not in workspaces_service.lock_work_members(
            session, work.workspace_id, {actor_id}
        ):
            session.rollback()
            raise WorkspaceUnavailable
        workspaces_service.add_work_access(
            session, work.id, work.workspace_id, actor_id, MAINTAINER
        )
        session.add(
            SecurityAudit(
                action="work_access_granted",
                actor_account_id=actor_id,
                target_account_id=actor_id,
                scope=_audit_scope(workspace_id, work.id),
            )
        )
        _commit_or_rollback(session)
    except WorkspaceUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return _work_data(work, MAINTAINER)


def list_works(
    session: Session, actor_id: UUID, workspace_id: UUID, page: int, size: int
) -> tuple[list[WorkData], int]:
    try:
        _require_workspace_member(session, workspace_id, actor_id)
        total = (
            session.scalar(
                select(func.count())
                .select_from(Work)
                .where(Work.workspace_id == workspace_id)
            )
            or 0
        )
        access_roles = workspaces_service.work_access_roles_subquery(actor_id)
        rows = session.execute(
            select(Work, access_roles.c.role)
            .join(access_roles, access_roles.c.work_id == Work.id)
            .where(Work.workspace_id == workspace_id)
            .order_by(Work.created_at.desc(), Work.id)
            .offset((page - 1) * size)
            .limit(size)
        )
    except WorkspaceUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error
    return ([_work_data(work, role) for work, role in rows], total)


def ensure_work_access(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> None:
    try:
        work = _load_visible_work(session, workspace_id, work_id)
        if _own_access_role(session, work.id, actor_id) is None:
            session.rollback()
            raise WorkUnavailable
    except WorkUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error


def lock_work_for_files(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> None:
    try:
        session.execute(
            select(
                func.pg_advisory_xact_lock(
                    int.from_bytes(work_id.bytes[:8], byteorder="big", signed=True)
                )
            )
        )
        work = session.scalar(
            select(Work).where(Work.id == work_id, Work.workspace_id == workspace_id)
        )
        if work is None or _own_access_role(session, work_id, actor_id) is None:
            session.rollback()
            raise WorkUnavailable
    except WorkUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error


def read_work(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> WorkData:
    try:
        work = _load_visible_work(session, workspace_id, work_id)
        role = _own_access_role(session, work.id, actor_id)
        if role is None:
            session.rollback()
            raise WorkUnavailable
    except WorkUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error
    return _work_data(work, role)


def update_work(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    *,
    name: str,
    description: str,
    creative_stage: str,
    target_experience: str,
    min_players: int,
    max_players: int,
    estimated_duration_minutes: int,
    expected_revision: int,
) -> WorkData:
    name, description, creative_stage, target_experience = _validate_work_values(
        name,
        description,
        creative_stage,
        target_experience,
        min_players,
        max_players,
        estimated_duration_minutes,
    )
    try:
        work, _ = _prepare_manager_write(session, workspace_id, work_id, actor_id)
        updated = session.execute(
            update(Work)
            .where(Work.id == work.id, Work.revision == expected_revision)
            .values(
                name=name,
                description=description,
                creative_stage=creative_stage,
                target_experience=target_experience,
                min_players=min_players,
                max_players=max_players,
                estimated_duration_minutes=estimated_duration_minutes,
                revision=Work.revision + 1,
                updated_at=func.now(),
            )
            .returning(Work)
        ).scalar_one_or_none()
        if updated is None:
            session.rollback()
            raise WorkRevisionConflict
        _commit_or_rollback(session)
    except (
        WorkManagementForbidden,
        WorkRevisionConflict,
        WorkUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return _work_data(updated, MAINTAINER)


def list_access_members(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    page: int,
    size: int,
) -> tuple[list[WorkAccessMemberData], int]:
    try:
        work = _require_manager(session, workspace_id, work_id, actor_id)
        members, total = workspaces_service.list_work_access_members(
            session, work.workspace_id, work.id, page, size
        )
    except (WorkManagementForbidden, WorkUnavailable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error
    return (
        [
            WorkAccessMemberData(
                account_id=member.account_id, email=member.email, role=member.role
            )
            for member in members
        ],
        total,
    )


def set_work_access(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    account_id: UUID,
    role: str,
) -> WorkAccessData:
    if role not in WORK_ROLES:
        raise ValueError("作品角色不符合约束")
    try:
        work, accesses = _prepare_manager_write(
            session, workspace_id, work_id, actor_id, account_id
        )
        access = next(
            (candidate for candidate in accesses if candidate.account_id == account_id),
            None,
        )
        maintainers = sum(candidate.role == MAINTAINER for candidate in accesses)
        if (
            access is not None
            and access.role == MAINTAINER
            and role != MAINTAINER
            and maintainers == 1
        ):
            session.rollback()
            raise WorkLastMaintainerRequired
        if access is None:
            workspaces_service.add_work_access(
                session, work.id, work.workspace_id, account_id, role
            )
            action = "work_access_granted"
        elif access.role != role:
            workspaces_service.update_work_access_role(
                session, work.id, account_id, role
            )
            action = "work_access_role_changed"
        else:
            _commit_or_rollback(session)
            return WorkAccessData(account_id=account_id, role=role)
        session.add(
            SecurityAudit(
                action=action,
                actor_account_id=actor_id,
                target_account_id=account_id,
                scope=_audit_scope(workspace_id, work.id),
            )
        )
        _commit_or_rollback(session)
    except (
        WorkAccessMemberUnavailable,
        WorkLastMaintainerRequired,
        WorkManagementForbidden,
        WorkUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return WorkAccessData(account_id=account_id, role=role)


def revoke_work_access(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    account_id: UUID,
) -> None:
    try:
        work, accesses = _prepare_manager_write(
            session, workspace_id, work_id, actor_id, account_id
        )
        access = next(
            (candidate for candidate in accesses if candidate.account_id == account_id),
            None,
        )
        if access is None:
            session.rollback()
            raise WorkAccessUnavailable
        if (
            access.role == MAINTAINER
            and sum(candidate.role == MAINTAINER for candidate in accesses) == 1
        ):
            session.rollback()
            raise WorkLastMaintainerRequired
        workspaces_service.delete_work_access(session, work.id, account_id)
        session.add(
            SecurityAudit(
                action="work_access_revoked",
                actor_account_id=actor_id,
                target_account_id=account_id,
                scope=_audit_scope(workspace_id, work.id),
            )
        )
        _commit_or_rollback(session)
    except (
        WorkAccessMemberUnavailable,
        WorkAccessUnavailable,
        WorkLastMaintainerRequired,
        WorkManagementForbidden,
        WorkUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkOperationRetryable from error
    except Exception:
        session.rollback()
        raise
