from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import (
    set_actor,
    set_invitation_credential,
    set_maintenance_workspace_scope,
    set_work_access_cleanup_scope,
    set_workspace_exit_maintenance_scope,
    set_workspace_exit_processor,
    set_workspace_invitation_inbox_scope,
    set_workspace_management_scope,
)
from app.audit.models import SecurityAudit
from app.core.config import settings
from app.identity import service as identity_service
from app.identity.models import Account, OneTimeCredential
from app.notifications.models import MailOutbox
from app.notifications.service import (
    cancel_todo_by_source,
    cancel_workspace_todos,
    clear_outbox_envelopes,
    complete_todo_by_source,
    create_todo,
    delete_work_access_todos,
    enqueue_token_mail,
)
from app.workspaces.models import (
    WorkAccess,
    Workspace,
    WorkspaceInvitation,
    WorkspaceInvitationAttempt,
    WorkspaceMember,
)

INVITATION_ATTEMPT_PURPOSE = "workspace_invitation_exchange"
CURRENT_SESSION_OPERATION = "current_session"
ACCOUNT_ACTIVATION_OPERATION = "account_activation"


class WorkspaceUnavailable(Exception):
    pass


class WorkspaceManagementForbidden(Exception):
    pass


class InvitationRecipientUnavailable(Exception):
    pass


class WorkspaceMemberExists(Exception):
    pass


class WorkspaceInvitationExists(Exception):
    pass


class WorkspaceOwnerCannotBeRemoved(Exception):
    pass


class WorkspaceMemberUnavailable(Exception):
    pass


class WorkspaceMemberLastMaintainerRequired(Exception):
    pass


class WorkspaceInvitationUnavailable(Exception):
    pass


class WorkspaceInvitationAccountMismatch(Exception):
    pass


class WorkspaceOperationRetryable(Exception):
    pass


class WorkspaceExitReauthenticationFailed(Exception):
    pass


class WorkspaceRevisionConflict(Exception):
    pass


class WorkspaceExitInProgress(Exception):
    pass


class WorkspaceExitInvalid(Exception):
    pass


@dataclass(frozen=True)
class WorkspaceInvitationRateLimited(Exception):
    retry_after: int


@dataclass(frozen=True)
class WorkspaceData:
    id: UUID
    name: str
    description: str | None
    is_owner: bool
    revision: int = 1
    access_state: str = "active"
    read_until: datetime | None = None


@dataclass(frozen=True)
class WorkspaceMemberData:
    account_id: UUID
    email: str
    is_owner: bool
    joined_at: datetime


@dataclass(frozen=True)
class WorkAccessRecord:
    account_id: UUID
    role: str


@dataclass(frozen=True)
class WorkAccessMemberRecord:
    account_id: UUID
    email: str
    role: str | None


@dataclass(frozen=True)
class WorkspaceInvitationData:
    id: UUID
    email: str
    status: str
    mail_status: str
    expires_at: datetime


@dataclass(frozen=True)
class InvitationExchangeResult:
    workspace: WorkspaceData
    login_required: bool = False
    session_result: identity_service.SessionResult | None = None


def has_workspace_member(
    session: Session, workspace_id: UUID, account_id: UUID
) -> bool:
    return (
        session.scalar(
            select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.account_id == account_id,
            )
        )
        is not None
    )


def work_access_role(session: Session, work_id: UUID, account_id: UUID) -> str | None:
    return session.scalar(
        select(WorkAccess.role).where(
            WorkAccess.work_id == work_id, WorkAccess.account_id == account_id
        )
    )


def work_access_roles_subquery(account_id: UUID):
    return (
        select(WorkAccess.work_id.label("work_id"), WorkAccess.role.label("role"))
        .where(WorkAccess.account_id == account_id)
        .subquery()
    )


def lock_work_members(
    session: Session, workspace_id: UUID, account_ids: set[UUID]
) -> frozenset[UUID]:
    return frozenset(
        session.scalars(
            select(WorkspaceMember.account_id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.account_id.in_(account_ids),
            )
            .order_by(WorkspaceMember.account_id)
            .with_for_update()
        )
    )


def lock_work_accesses(session: Session, work_id: UUID) -> list[WorkAccessRecord]:
    return [
        WorkAccessRecord(account_id=account_id, role=role)
        for account_id, role in session.execute(
            select(WorkAccess.account_id, WorkAccess.role)
            .where(WorkAccess.work_id == work_id)
            .order_by(WorkAccess.account_id)
            .with_for_update()
        )
    ]


def current_work_maintainer_ids(session: Session, work_id: UUID) -> tuple[UUID, ...]:
    return tuple(
        session.scalars(
            select(WorkAccess.account_id)
            .where(WorkAccess.work_id == work_id, WorkAccess.role == "maintainer")
            .order_by(WorkAccess.account_id)
        )
    )


def add_work_access(
    session: Session,
    work_id: UUID,
    workspace_id: UUID,
    account_id: UUID,
    role: str,
) -> None:
    session.add(
        WorkAccess(
            work_id=work_id,
            workspace_id=workspace_id,
            account_id=account_id,
            role=role,
        )
    )


def update_work_access_role(
    session: Session, work_id: UUID, account_id: UUID, role: str
) -> None:
    session.execute(
        update(WorkAccess)
        .where(WorkAccess.work_id == work_id, WorkAccess.account_id == account_id)
        .values(role=role)
    )


def delete_work_access(session: Session, work_id: UUID, account_id: UUID) -> None:
    session.execute(
        delete(WorkAccess).where(
            WorkAccess.work_id == work_id, WorkAccess.account_id == account_id
        )
    )


def list_work_access_members(
    session: Session, workspace_id: UUID, work_id: UUID, page: int, size: int
) -> tuple[list[WorkAccessMemberRecord], int]:
    total = (
        session.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
        )
        or 0
    )
    rows = session.execute(
        select(WorkspaceMember.account_id, Account.email, WorkAccess.role)
        .join(Account, Account.id == WorkspaceMember.account_id)
        .outerjoin(
            WorkAccess,
            (WorkAccess.work_id == work_id)
            & (WorkAccess.account_id == WorkspaceMember.account_id),
        )
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.joined_at, WorkspaceMember.account_id)
        .offset((page - 1) * size)
        .limit(size)
    )
    return (
        [
            WorkAccessMemberRecord(account_id=account_id, email=email, role=role)
            for account_id, email, role in rows
        ],
        total,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _commit_or_rollback(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _workspace_data(workspace: Workspace, account_id: UUID) -> WorkspaceData:
    return WorkspaceData(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        is_owner=workspace.owner_account_id == account_id,
        revision=workspace.revision,
        access_state="exiting" if workspace.exit_requested_at is not None else "active",
        read_until=workspace.exit_read_until,
    )


def ensure_workspace_writable(session: Session, workspace_id: UUID) -> None:
    workspace = _load_visible_workspace(session, workspace_id)
    if workspace.exit_requested_at is not None:
        session.rollback()
        raise WorkspaceExitInProgress


def ensure_authorized_workspace_writable(session: Session, workspace_id: UUID) -> None:
    if session.scalar(
        text("SELECT public.workspace_exit_allows_write(:workspace_id)"),
        {"workspace_id": workspace_id},
    ):
        return
    session.rollback()
    raise WorkspaceExitInProgress


def _load_visible_workspace(session: Session, workspace_id: UUID) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        session.rollback()
        raise WorkspaceUnavailable
    return workspace


def _require_workspace_manager(
    session: Session, workspace_id: UUID, actor_account_id: UUID
) -> Workspace:
    workspace = _load_visible_workspace(session, workspace_id)
    if workspace.owner_account_id != actor_account_id:
        session.rollback()
        raise WorkspaceManagementForbidden
    set_workspace_management_scope(session, workspace.id)
    return workspace


def _invitation_status(
    invitation: WorkspaceInvitation, credential: OneTimeCredential | None, now: datetime
) -> str:
    if invitation.status == "active" and (
        credential is None
        or credential.status != identity_service.TOKEN_ACTIVE
        or credential.expires_at <= now
    ):
        return "expired"
    return invitation.status


def _mail_status(outbox: MailOutbox | None) -> str:
    if outbox is None or outbox.status in {"cancelled", "suppressed"}:
        return "cancelled"
    if outbox.status in {"pending", "sending"}:
        return "pending"
    if outbox.status == "accepted":
        return "accepted"
    if outbox.status == "failed":
        return "failed"
    return "unknown"


def _invitation_data(
    invitation: WorkspaceInvitation,
    account: Account,
    credential: OneTimeCredential,
    outbox: MailOutbox | None,
    now: datetime,
) -> WorkspaceInvitationData:
    return WorkspaceInvitationData(
        id=invitation.id,
        email=account.email,
        status=_invitation_status(invitation, credential, now),
        mail_status=_mail_status(outbox),
        expires_at=credential.expires_at,
    )


def _attempt_retry_after(session: Session, subject_hash: bytes) -> int | None:
    now = _now()
    attempt = session.scalar(
        select(WorkspaceInvitationAttempt)
        .where(
            WorkspaceInvitationAttempt.purpose == INVITATION_ATTEMPT_PURPOSE,
            WorkspaceInvitationAttempt.subject_hash == subject_hash,
        )
        .with_for_update()
    )
    if attempt is None:
        return None
    window = timedelta(minutes=settings.auth_attempt_window_minutes)
    if now - attempt.window_started_at >= window:
        attempt.window_started_at = now
        attempt.count = 0
        return None
    if attempt.count >= settings.recovery_exchange_max_attempts:
        return max(1, int((window - (now - attempt.window_started_at)).total_seconds()))
    return None


def _record_attempt(session: Session, subject_hash: bytes) -> None:
    now = _now()
    attempt = session.scalar(
        select(WorkspaceInvitationAttempt)
        .where(
            WorkspaceInvitationAttempt.purpose == INVITATION_ATTEMPT_PURPOSE,
            WorkspaceInvitationAttempt.subject_hash == subject_hash,
        )
        .with_for_update()
    )
    if attempt is None:
        session.add(
            WorkspaceInvitationAttempt(
                purpose=INVITATION_ATTEMPT_PURPOSE,
                subject_hash=subject_hash,
                window_started_at=now,
                count=1,
            )
        )
        return
    if now - attempt.window_started_at >= timedelta(
        minutes=settings.auth_attempt_window_minutes
    ):
        attempt.window_started_at = now
        attempt.count = 1
        return
    attempt.count += 1


def _clear_attempt(session: Session, subject_hash: bytes) -> None:
    attempt = session.scalar(
        select(WorkspaceInvitationAttempt).where(
            WorkspaceInvitationAttempt.purpose == INVITATION_ATTEMPT_PURPOSE,
            WorkspaceInvitationAttempt.subject_hash == subject_hash,
        )
    )
    if attempt is not None:
        session.delete(attempt)


def _record_unavailable(session: Session, subject_hash: bytes) -> None:
    _record_attempt(session, subject_hash)
    _commit_or_rollback(session)
    raise WorkspaceInvitationUnavailable


def _lock_outboxes(
    session: Session, credential_ids: list[UUID]
) -> dict[UUID, MailOutbox]:
    if not credential_ids:
        return {}
    return {
        outbox.credential_id: outbox
        for outbox in session.scalars(
            select(MailOutbox)
            .where(MailOutbox.credential_id.in_(credential_ids))
            .order_by(MailOutbox.credential_id)
            .with_for_update()
        )
    }


def _lock_credentials(
    session: Session, credential_ids: list[UUID]
) -> dict[UUID, OneTimeCredential]:
    if not credential_ids:
        return {}
    return {
        credential.id: credential
        for credential in session.scalars(
            select(OneTimeCredential)
            .where(OneTimeCredential.id.in_(credential_ids))
            .order_by(OneTimeCredential.id)
            .with_for_update()
        )
    }


def _lock_invitations(
    session: Session, invitation_ids: list[UUID]
) -> dict[UUID, WorkspaceInvitation]:
    if not invitation_ids:
        return {}
    return {
        invitation.id: invitation
        for invitation in session.scalars(
            select(WorkspaceInvitation)
            .where(WorkspaceInvitation.id.in_(invitation_ids))
            .order_by(WorkspaceInvitation.id)
            .with_for_update()
        )
    }


def _expire_invitation(
    session: Session,
    invitation: WorkspaceInvitation,
    credential: OneTimeCredential | None,
    now: datetime,
) -> bool:
    if _invitation_status(invitation, credential, now) != "expired":
        return False
    invitation.status = "expired"
    cancel_todo_by_source(
        session,
        invitation.account_id,
        "workspace_invitation",
        str(invitation.id),
    )
    if credential is not None:
        identity_service.revoke_workspace_invitation_credential(
            session, credential, now
        )
        clear_outbox_envelopes(session, [credential.id])
    return True


def expire_inbox_invitation(
    session: Session, account_id: UUID, invitation_id: UUID, now: datetime
) -> bool:
    set_actor(session, account_id)
    set_workspace_invitation_inbox_scope(session, invitation_id)
    invitation = session.scalar(
        select(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.account_id == account_id,
        )
        .with_for_update()
    )
    if invitation is None:
        return False
    set_invitation_credential(session, invitation.credential_id)
    _lock_outboxes(session, [invitation.credential_id])
    credential = session.scalar(
        select(OneTimeCredential)
        .where(OneTimeCredential.id == invitation.credential_id)
        .with_for_update()
    )
    return _expire_invitation(session, invitation, credential, now)


def _expire_active_invitations(
    session: Session, workspace_id: UUID, account_id: UUID, now: datetime
) -> WorkspaceInvitation | None:
    candidates = list(
        session.execute(
            select(WorkspaceInvitation.id, WorkspaceInvitation.credential_id).where(
                WorkspaceInvitation.workspace_id == workspace_id,
                WorkspaceInvitation.account_id == account_id,
                WorkspaceInvitation.status == "active",
            )
        )
    )
    invitation_ids = [candidate.id for candidate in candidates]
    credential_ids = [candidate.credential_id for candidate in candidates]
    credential_ids_by_invitation = {
        candidate.id: candidate.credential_id for candidate in candidates
    }
    _lock_outboxes(session, credential_ids)
    credentials = _lock_credentials(session, credential_ids)
    invitations = _lock_invitations(session, invitation_ids)

    active: WorkspaceInvitation | None = None
    for invitation_id in invitation_ids:
        invitation = invitations.get(invitation_id)
        if (
            invitation is None
            or invitation.workspace_id != workspace_id
            or invitation.account_id != account_id
            or invitation.credential_id != credential_ids_by_invitation[invitation_id]
            or invitation.status != "active"
        ):
            continue
        credential = credentials.get(invitation.credential_id)
        if not _expire_invitation(session, invitation, credential, now):
            active = invitation
    return active


def _lock_invitation_for_exchange(
    session: Session, token: str
) -> tuple[OneTimeCredential | None, WorkspaceInvitation | None]:
    credential_id = identity_service.find_workspace_invitation_credential_id(
        session, token
    )
    if credential_id is None:
        return None, None
    _lock_outboxes(session, [credential_id])
    credential = identity_service.lock_workspace_invitation_credential(session, token)
    if credential is None:
        return None, None
    set_invitation_credential(session, credential.id)
    invitation = session.scalar(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.credential_id == credential.id)
        .with_for_update()
    )
    return credential, invitation


def _is_exchange_available(
    credential: OneTimeCredential | None,
    invitation: WorkspaceInvitation | None,
    now: datetime,
) -> bool:
    return (
        credential is not None
        and invitation is not None
        and credential.status == identity_service.TOKEN_ACTIVE
        and credential.expires_at > now
        and invitation.status == "active"
    )


def create_workspace(
    session: Session, actor_account_id: UUID, name: str, description: str | None
) -> WorkspaceData:
    name = name.strip()
    description = description.strip() if description is not None else None
    if not name:
        raise ValueError("工作空间名称不能为空")
    if description == "":
        description = None
    workspace = Workspace(
        name=name, description=description, owner_account_id=actor_account_id
    )
    try:
        session.add(workspace)
        session.flush()
        set_workspace_management_scope(session, workspace.id)
        session.add(
            WorkspaceMember(workspace_id=workspace.id, account_id=actor_account_id)
        )
        session.add(
            SecurityAudit(
                action="workspace_member_granted",
                actor_account_id=actor_account_id,
                target_account_id=actor_account_id,
                scope=str(workspace.id),
            )
        )
        _commit_or_rollback(session)
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return _workspace_data(workspace, actor_account_id)


def add_baseline_member(session: Session, workspace_id: UUID, account_id: UUID) -> None:
    set_workspace_management_scope(session, workspace_id)
    session.add(WorkspaceMember(workspace_id=workspace_id, account_id=account_id))
    session.flush()


def list_workspaces(
    session: Session, actor_account_id: UUID, page: int, size: int
) -> tuple[list[WorkspaceData], int]:
    total = session.scalar(select(func.count()).select_from(Workspace)) or 0
    workspaces = list(
        session.scalars(
            select(Workspace)
            .order_by(Workspace.created_at.desc(), Workspace.id)
            .offset((page - 1) * size)
            .limit(size)
        )
    )
    return (
        [_workspace_data(workspace, actor_account_id) for workspace in workspaces],
        total,
    )


def read_workspace(
    session: Session, actor_account_id: UUID, workspace_id: UUID
) -> WorkspaceData:
    return _workspace_data(
        _load_visible_workspace(session, workspace_id), actor_account_id
    )


def list_members(
    session: Session,
    actor_account_id: UUID,
    workspace_id: UUID,
    page: int,
    size: int,
) -> tuple[list[WorkspaceMemberData], int]:
    workspace = _require_workspace_manager(session, workspace_id, actor_account_id)
    total = (
        session.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace.id)
        )
        or 0
    )
    rows = session.execute(
        select(WorkspaceMember, Account)
        .join(Account, Account.id == WorkspaceMember.account_id)
        .where(WorkspaceMember.workspace_id == workspace.id)
        .order_by(WorkspaceMember.joined_at, WorkspaceMember.account_id)
        .offset((page - 1) * size)
        .limit(size)
    )
    return (
        [
            WorkspaceMemberData(
                account_id=member.account_id,
                email=account.email,
                is_owner=member.account_id == workspace.owner_account_id,
                joined_at=member.joined_at,
            )
            for member, account in rows
        ],
        total,
    )


def list_invitations(
    session: Session,
    actor_account_id: UUID,
    workspace_id: UUID,
    page: int,
    size: int,
) -> tuple[list[WorkspaceInvitationData], int]:
    workspace = _require_workspace_manager(session, workspace_id, actor_account_id)
    total = (
        session.scalar(
            select(func.count())
            .select_from(WorkspaceInvitation)
            .where(WorkspaceInvitation.workspace_id == workspace.id)
        )
        or 0
    )
    rows = session.execute(
        select(WorkspaceInvitation, Account, OneTimeCredential, MailOutbox)
        .join(Account, Account.id == WorkspaceInvitation.account_id)
        .join(
            OneTimeCredential, OneTimeCredential.id == WorkspaceInvitation.credential_id
        )
        .outerjoin(
            MailOutbox, MailOutbox.credential_id == WorkspaceInvitation.credential_id
        )
        .where(WorkspaceInvitation.workspace_id == workspace.id)
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id)
        .offset((page - 1) * size)
        .limit(size)
    )
    now = _now()
    return (
        [
            _invitation_data(invitation, account, credential, outbox, now)
            for invitation, account, credential, outbox in rows
        ],
        total,
    )


def create_invitation(
    session: Session, actor_account_id: UUID, workspace_id: UUID, email: str
) -> WorkspaceInvitationData:
    try:
        workspace = _require_workspace_manager(session, workspace_id, actor_account_id)
        ensure_workspace_writable(session, workspace.id)
        account = identity_service.get_or_create_invitation_account(session, email)
        if account.status == identity_service.DISABLED:
            session.rollback()
            raise InvitationRecipientUnavailable
        member = session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.account_id == account.id,
            )
        )
        if member is not None:
            session.rollback()
            raise WorkspaceMemberExists
        now = _now()
        if (
            _expire_active_invitations(session, workspace.id, account.id, now)
            is not None
        ):
            session.rollback()
            raise WorkspaceInvitationExists
        credential, token = identity_service.issue_workspace_invitation_credential(
            session, account
        )
        invitation = WorkspaceInvitation(
            workspace_id=workspace.id,
            account_id=account.id,
            credential_id=credential.id,
        )
        session.add(invitation)
        session.flush()
        todo_id = None
        if account.status == identity_service.ACTIVE:
            todo, _ = create_todo(
                session,
                recipient_account_id=account.id,
                workspace_id=workspace.id,
                work_id=None,
                kind="workspace_invitation",
                target_kind="workspace_invitation",
                target_id=invitation.id,
                source_key=str(invitation.id),
                summary="加入工作空间邀请",
                context_label=f"工作空间：{workspace.name}",
            )
            todo_id = todo.id
        outbox = enqueue_token_mail(
            session,
            credential.id,
            account.id,
            identity_service.WORKSPACE_INVITATION,
            token,
            workspace_name=workspace.name,
            todo_id=todo_id,
        )
        session.add(
            SecurityAudit(
                action="workspace_invitation_created",
                actor_account_id=actor_account_id,
                target_account_id=account.id,
                scope=str(workspace.id),
            )
        )
        _commit_or_rollback(session)
    except (
        InvitationRecipientUnavailable,
        WorkspaceInvitationExists,
        WorkspaceMemberExists,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return _invitation_data(invitation, account, credential, outbox, now)


def revoke_invitation(
    session: Session, actor_account_id: UUID, workspace_id: UUID, invitation_id: UUID
) -> WorkspaceInvitationData:
    try:
        workspace = _require_workspace_manager(session, workspace_id, actor_account_id)
        ensure_workspace_writable(session, workspace.id)
        candidate = session.execute(
            select(WorkspaceInvitation.id, WorkspaceInvitation.credential_id).where(
                WorkspaceInvitation.workspace_id == workspace.id,
                WorkspaceInvitation.id == invitation_id,
            )
        ).one_or_none()
        if candidate is None:
            session.rollback()
            raise WorkspaceInvitationUnavailable
        outboxes = _lock_outboxes(session, [candidate.credential_id])
        credentials = _lock_credentials(session, [candidate.credential_id])
        invitations = _lock_invitations(session, [candidate.id])
        invitation = invitations.get(candidate.id)
        credential = credentials.get(candidate.credential_id)
        if (
            invitation is None
            or credential is None
            or invitation.workspace_id != workspace.id
            or invitation.credential_id != credential.id
        ):
            session.rollback()
            raise WorkspaceInvitationUnavailable

        now = _now()
        if _invitation_status(invitation, credential, now) == "expired":
            invitation.status = "expired"
            identity_service.revoke_workspace_invitation_credential(
                session, credential, now
            )
            cancel_todo_by_source(
                session,
                invitation.account_id,
                "workspace_invitation",
                str(invitation.id),
            )
            clear_outbox_envelopes(session, [credential.id])
        elif invitation.status == "active":
            invitation.status = "revoked"
            invitation.revoked_at = now
            identity_service.revoke_workspace_invitation_credential(
                session, credential, now
            )
            cancel_todo_by_source(
                session,
                invitation.account_id,
                "workspace_invitation",
                str(invitation.id),
            )
            clear_outbox_envelopes(session, [credential.id])
            session.add(
                SecurityAudit(
                    action="workspace_invitation_revoked",
                    actor_account_id=actor_account_id,
                    target_account_id=invitation.account_id,
                    scope=str(workspace.id),
                )
            )
        account = session.get(Account, invitation.account_id)
        if account is None:
            session.rollback()
            raise WorkspaceInvitationUnavailable
        _commit_or_rollback(session)
    except WorkspaceInvitationUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return _invitation_data(
        invitation, account, credential, outboxes.get(credential.id), now
    )


def remove_member(
    session: Session, actor_account_id: UUID, workspace_id: UUID, account_id: UUID
) -> None:
    try:
        workspace = _require_workspace_manager(session, workspace_id, actor_account_id)
        ensure_workspace_writable(session, workspace.id)
        if account_id == workspace.owner_account_id:
            session.rollback()
            raise WorkspaceOwnerCannotBeRemoved
        member = session.scalar(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.account_id == account_id,
            )
            .with_for_update()
        )
        if member is None:
            session.rollback()
            raise WorkspaceMemberUnavailable

        set_work_access_cleanup_scope(session, workspace.id)
        work_ids = list(
            session.scalars(
                select(WorkAccess.work_id)
                .where(WorkAccess.account_id == account_id)
                .order_by(WorkAccess.work_id)
            )
        )
        for work_id in work_ids:
            accesses = list(
                session.scalars(
                    select(WorkAccess)
                    .where(WorkAccess.work_id == work_id)
                    .order_by(WorkAccess.account_id)
                    .with_for_update()
                )
            )
            target_access = next(
                (access for access in accesses if access.account_id == account_id),
                None,
            )
            if target_access is None:
                continue
            if (
                target_access.role == "maintainer"
                and sum(access.role == "maintainer" for access in accesses) == 1
            ):
                session.rollback()
                raise WorkspaceMemberLastMaintainerRequired
            delete_work_access_todos(session, account_id, work_id)
            session.delete(target_access)
            session.add(
                SecurityAudit(
                    action="work_access_revoked",
                    actor_account_id=actor_account_id,
                    target_account_id=account_id,
                    scope=f"workspace:{workspace.id}/work:{work_id}",
                )
            )

        candidates = list(
            session.execute(
                select(WorkspaceInvitation.id, WorkspaceInvitation.credential_id).where(
                    WorkspaceInvitation.workspace_id == workspace.id,
                    WorkspaceInvitation.account_id == account_id,
                    WorkspaceInvitation.status == "active",
                )
            )
        )
        invitation_ids = [candidate.id for candidate in candidates]
        credential_ids = [candidate.credential_id for candidate in candidates]
        credential_ids_by_invitation = {
            candidate.id: candidate.credential_id for candidate in candidates
        }
        _lock_outboxes(session, credential_ids)
        credentials = _lock_credentials(session, credential_ids)
        invitations = _lock_invitations(session, invitation_ids)

        now = _now()
        revoked_credential_ids: list[UUID] = []
        for invitation_id in invitation_ids:
            invitation = invitations.get(invitation_id)
            if (
                invitation is None
                or invitation.workspace_id != workspace.id
                or invitation.account_id != account_id
                or invitation.credential_id
                != credential_ids_by_invitation[invitation_id]
                or invitation.status != "active"
            ):
                continue
            invitation.status = "revoked"
            invitation.revoked_at = now
            cancel_todo_by_source(
                session,
                invitation.account_id,
                "workspace_invitation",
                str(invitation.id),
            )
            credential = credentials.get(invitation.credential_id)
            if credential is not None:
                identity_service.revoke_workspace_invitation_credential(
                    session, credential, now
                )
                revoked_credential_ids.append(credential.id)
        clear_outbox_envelopes(session, revoked_credential_ids)
        session.delete(member)
        session.add(
            SecurityAudit(
                action="workspace_member_removed",
                actor_account_id=actor_account_id,
                target_account_id=account_id,
                scope=str(workspace.id),
            )
        )
        _commit_or_rollback(session)
    except (
        WorkspaceMemberLastMaintainerRequired,
        WorkspaceMemberUnavailable,
        WorkspaceOwnerCannotBeRemoved,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise


def exchange_current_session_invitation(
    session: Session, actor_account_id: UUID, token: str, operation_key: str
) -> InvitationExchangeResult:
    subject_hash = identity_service.attempt_subject(token)
    try:
        credential, invitation = _lock_invitation_for_exchange(session, token)
        if credential is None or invitation is None:
            session.rollback()
            raise WorkspaceInvitationUnavailable

        retry_after = _attempt_retry_after(session, subject_hash)
        if retry_after is not None:
            _commit_or_rollback(session)
            raise WorkspaceInvitationRateLimited(retry_after)
        now = _now()
        if invitation.account_id != actor_account_id:
            _record_attempt(session, subject_hash)
            _commit_or_rollback(session)
            raise WorkspaceInvitationAccountMismatch
        workspace = session.get(Workspace, invitation.workspace_id)
        if workspace is None:
            _record_unavailable(session, subject_hash)
        ensure_workspace_writable(session, workspace.id)
        if invitation.status == "accepted":
            if (
                invitation.accepted_operation == CURRENT_SESSION_OPERATION
                and invitation.accepted_operation_key == operation_key
            ):
                _clear_attempt(session, subject_hash)
                _commit_or_rollback(session)
                return InvitationExchangeResult(
                    _workspace_data(workspace, actor_account_id)
                )
            _record_unavailable(session, subject_hash)
        if not _is_exchange_available(credential, invitation, now):
            if _invitation_status(invitation, credential, now) == "expired":
                invitation.status = "expired"
            _record_unavailable(session, subject_hash)
        member = session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == invitation.workspace_id,
                WorkspaceMember.account_id == actor_account_id,
            )
        )
        if member is not None:
            _record_unavailable(session, subject_hash)

        session.add(
            WorkspaceMember(
                workspace_id=invitation.workspace_id, account_id=actor_account_id
            )
        )
        session.flush()
        invitation.status = "accepted"
        invitation.accepted_at = now
        invitation.accepted_operation_key = operation_key
        invitation.accepted_operation = CURRENT_SESSION_OPERATION
        if not identity_service.consume_workspace_invitation_credential(
            session, credential, now
        ):
            session.rollback()
            _record_unavailable(session, subject_hash)
        complete_todo_by_source(
            session,
            actor_account_id,
            "workspace_invitation",
            str(invitation.id),
        )
        clear_outbox_envelopes(session, [credential.id])
        _clear_attempt(session, subject_hash)
        session.add(
            SecurityAudit(
                action="workspace_member_granted",
                actor_account_id=actor_account_id,
                target_account_id=actor_account_id,
                scope=str(invitation.workspace_id),
            )
        )
        _commit_or_rollback(session)
    except (
        WorkspaceInvitationAccountMismatch,
        WorkspaceInvitationRateLimited,
        WorkspaceInvitationUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return InvitationExchangeResult(_workspace_data(workspace, actor_account_id))


def exchange_inbox_invitation(
    session: Session, actor_account_id: UUID, invitation_id: UUID, operation_key: str
) -> InvitationExchangeResult:
    try:
        set_actor(session, actor_account_id)
        set_workspace_invitation_inbox_scope(session, invitation_id)
        invitation = session.scalar(
            select(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.id == invitation_id,
                WorkspaceInvitation.account_id == actor_account_id,
            )
            .with_for_update()
        )
        if invitation is None:
            session.rollback()
            raise WorkspaceInvitationUnavailable
        credential = session.scalar(
            select(OneTimeCredential)
            .where(OneTimeCredential.id == invitation.credential_id)
            .with_for_update()
        )
        if credential is None:
            session.rollback()
            raise WorkspaceInvitationUnavailable
        set_invitation_credential(session, credential.id)
        workspace = session.get(Workspace, invitation.workspace_id)
        now = _now()
        if workspace is None:
            session.rollback()
            raise WorkspaceInvitationUnavailable
        ensure_workspace_writable(session, workspace.id)
        if invitation.status == "accepted":
            if (
                invitation.accepted_operation == "inbox"
                and invitation.accepted_operation_key == operation_key
            ):
                _commit_or_rollback(session)
                return InvitationExchangeResult(
                    _workspace_data(workspace, actor_account_id)
                )
            session.rollback()
            raise WorkspaceInvitationUnavailable
        if not _is_exchange_available(credential, invitation, now):
            if _expire_invitation(session, invitation, credential, now):
                _commit_or_rollback(session)
            else:
                session.rollback()
            raise WorkspaceInvitationUnavailable
        member = session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == invitation.workspace_id,
                WorkspaceMember.account_id == actor_account_id,
            )
        )
        if member is not None:
            session.rollback()
            raise WorkspaceInvitationUnavailable
        session.add(
            WorkspaceMember(
                workspace_id=invitation.workspace_id, account_id=actor_account_id
            )
        )
        session.flush()
        invitation.status = "accepted"
        invitation.accepted_at = now
        invitation.accepted_operation_key = operation_key
        invitation.accepted_operation = "inbox"
        if not identity_service.consume_workspace_invitation_credential(
            session, credential, now
        ):
            session.rollback()
            raise WorkspaceInvitationUnavailable
        complete_todo_by_source(
            session,
            actor_account_id,
            "workspace_invitation",
            str(invitation.id),
        )
        clear_outbox_envelopes(session, [credential.id])
        session.add(
            SecurityAudit(
                action="workspace_member_granted",
                actor_account_id=actor_account_id,
                target_account_id=actor_account_id,
                scope=str(invitation.workspace_id),
            )
        )
        _commit_or_rollback(session)
    except WorkspaceInvitationUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return InvitationExchangeResult(_workspace_data(workspace, actor_account_id))


def exchange_account_activation_invitation(
    session: Session, token: str, password: str, operation_key: str
) -> InvitationExchangeResult:
    identity_service.validate_password(password)
    subject_hash = identity_service.attempt_subject(token)
    try:
        credential, invitation = _lock_invitation_for_exchange(session, token)
        if credential is None or invitation is None:
            session.rollback()
            raise WorkspaceInvitationUnavailable

        retry_after = _attempt_retry_after(session, subject_hash)
        if retry_after is not None:
            _commit_or_rollback(session)
            raise WorkspaceInvitationRateLimited(retry_after)
        now = _now()
        account = identity_service.lock_account(session, invitation.account_id)
        workspace = session.get(Workspace, invitation.workspace_id)
        if account is None or workspace is None:
            _record_unavailable(session, subject_hash)
        if invitation.status == "accepted":
            if (
                invitation.accepted_operation == ACCOUNT_ACTIVATION_OPERATION
                and invitation.accepted_operation_key == operation_key
            ):
                _clear_attempt(session, subject_hash)
                _commit_or_rollback(session)
                return InvitationExchangeResult(
                    _workspace_data(workspace, invitation.account_id),
                    login_required=True,
                )
            _record_unavailable(session, subject_hash)
        if not _is_exchange_available(credential, invitation, now):
            if _invitation_status(invitation, credential, now) == "expired":
                invitation.status = "expired"
            _record_unavailable(session, subject_hash)
        if account.status != identity_service.PENDING_ACTIVATION:
            _record_unavailable(session, subject_hash)
        member = session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == invitation.workspace_id,
                WorkspaceMember.account_id == account.id,
            )
        )
        if member is not None:
            _record_unavailable(session, subject_hash)

        session.add(
            WorkspaceMember(workspace_id=invitation.workspace_id, account_id=account.id)
        )
        session.flush()
        session_result = identity_service.activate_invited_account(
            session, account, password
        )
        invitation.status = "accepted"
        invitation.accepted_at = now
        invitation.accepted_operation_key = operation_key
        invitation.accepted_operation = ACCOUNT_ACTIVATION_OPERATION
        if not identity_service.consume_workspace_invitation_credential(
            session, credential, now
        ):
            session.rollback()
            _record_unavailable(session, subject_hash)
        _clear_attempt(session, subject_hash)
        session.add(
            SecurityAudit(
                action="workspace_member_granted",
                actor_account_id=account.id,
                target_account_id=account.id,
                scope=str(invitation.workspace_id),
            )
        )
        _commit_or_rollback(session)
    except (
        WorkspaceInvitationRateLimited,
        WorkspaceInvitationUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return InvitationExchangeResult(
        _workspace_data(workspace, account.id), session_result=session_result
    )


def request_workspace_exit(
    session: Session,
    actor_account_id: UUID,
    workspace_id: UUID,
    password: str,
    expected_revision: int,
    read_until: datetime,
    operation_key: str,
) -> WorkspaceData:
    if (
        expected_revision <= 0
        or read_until.tzinfo is None
        or not operation_key
        or len(operation_key) > 128
    ):
        raise WorkspaceExitInvalid
    try:
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        if workspace is None:
            session.rollback()
            raise WorkspaceUnavailable
        if (
            workspace.owner_account_id != actor_account_id
            or not identity_service.verify_current_password(
                session, actor_account_id, password
            )
        ):
            session.rollback()
            raise WorkspaceExitReauthenticationFailed
        if workspace.exit_requested_at is not None:
            if (
                workspace.exit_requested_by_account_id == actor_account_id
                and workspace.exit_operation_key == operation_key
            ):
                _commit_or_rollback(session)
                return _workspace_data(workspace, actor_account_id)
            session.rollback()
            raise WorkspaceExitInProgress
        workspace = session.scalar(
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if workspace is None:
            workspace = session.scalar(
                select(Workspace).where(Workspace.id == workspace_id)
            )
            if workspace is None:
                session.rollback()
                raise WorkspaceUnavailable
            if (
                workspace.exit_requested_by_account_id == actor_account_id
                and workspace.exit_operation_key == operation_key
            ):
                _commit_or_rollback(session)
                return _workspace_data(workspace, actor_account_id)
            session.rollback()
            raise WorkspaceExitInProgress
        if workspace.exit_requested_at is not None:
            if (
                workspace.exit_requested_by_account_id == actor_account_id
                and workspace.exit_operation_key == operation_key
            ):
                _commit_or_rollback(session)
                return _workspace_data(workspace, actor_account_id)
            session.rollback()
            raise WorkspaceExitInProgress
        if workspace.revision != expected_revision:
            session.rollback()
            raise WorkspaceRevisionConflict
        now = session.scalar(select(func.current_timestamp()))
        if now is None or read_until <= now:
            session.rollback()
            raise WorkspaceExitInvalid
        workspace.exit_requested_at = now
        workspace.exit_read_until = read_until
        workspace.exit_requested_by_account_id = actor_account_id
        workspace.exit_operation_key = operation_key
        workspace.revision += 1
        session.add(
            SecurityAudit(
                action="workspace_exit_requested",
                actor_account_id=actor_account_id,
                scope=f"workspace:{workspace.id};read_until:{read_until.isoformat()}",
            )
        )
        _commit_or_rollback(session)
    except (
        WorkspaceExitInProgress,
        WorkspaceExitInvalid,
        WorkspaceExitReauthenticationFailed,
        WorkspaceRevisionConflict,
        WorkspaceUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return _workspace_data(workspace, actor_account_id)


def process_next_due_workspace_exit(session: Session) -> str | None:
    """收敛一项到期退出；调用方负责轮询，不创建独立任务队列。"""
    try:
        set_workspace_exit_processor(session)
        workspace = session.scalar(
            select(Workspace)
            .where(
                Workspace.exit_requested_at.is_not(None),
                Workspace.exit_completed_at.is_(None),
                Workspace.exit_read_until <= func.current_timestamp(),
            )
            .order_by(Workspace.exit_read_until, Workspace.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if workspace is None:
            session.rollback()
            return None
        set_workspace_exit_maintenance_scope(session, workspace.id)
        set_workspace_management_scope(session, workspace.id)
        set_work_access_cleanup_scope(session, workspace.id)

        member_ids = list(
            session.scalars(
                select(WorkspaceMember.account_id)
                .where(WorkspaceMember.workspace_id == workspace.id)
                .order_by(WorkspaceMember.account_id)
                .with_for_update()
            )
        )
        list(
            session.scalars(
                select(WorkAccess)
                .where(WorkAccess.workspace_id == workspace.id)
                .order_by(WorkAccess.work_id, WorkAccess.account_id)
                .with_for_update()
            )
        )
        invitations = list(
            session.scalars(
                select(WorkspaceInvitation)
                .where(
                    WorkspaceInvitation.workspace_id == workspace.id,
                    WorkspaceInvitation.status == "active",
                )
                .order_by(WorkspaceInvitation.id)
                .with_for_update()
            )
        )
        credential_ids = [invitation.credential_id for invitation in invitations]
        credentials = _lock_credentials(session, credential_ids)
        _lock_outboxes(session, credential_ids)
        now = session.scalar(select(func.current_timestamp()))
        if now is None:
            raise WorkspaceOperationRetryable
        for invitation in invitations:
            invitation.status = "revoked"
            invitation.revoked_at = now
            credential = credentials.get(invitation.credential_id)
            if credential is not None:
                identity_service.revoke_workspace_invitation_credential(
                    session, credential, now
                )
        clear_outbox_envelopes(session, credential_ids)
        cancel_workspace_todos(session, workspace.id)
        session.execute(
            delete(WorkAccess).where(WorkAccess.workspace_id == workspace.id)
        )
        session.execute(
            delete(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace.id)
        )
        identity_service.revoke_sessions_for_accounts(
            session, member_ids, "workspace_exit"
        )
        workspace.exit_completed_at = now
        session.add(
            SecurityAudit(
                action="workspace_exit_completed",
                scope=f"workspace:{workspace.id}",
            )
        )
        _commit_or_rollback(session)
    except SQLAlchemyError as error:
        session.rollback()
        raise WorkspaceOperationRetryable from error
    except Exception:
        session.rollback()
        raise
    return "completed"


def diagnose_workspace(
    session: Session, workspace_id: UUID, operator: str, reason: str
) -> dict[str, object]:
    if not operator.strip() or not reason.strip():
        raise ValueError("受控维护必须提供操作者和理由")
    set_maintenance_workspace_scope(session, workspace_id)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        session.rollback()
        raise WorkspaceUnavailable
    members = list(
        session.scalars(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace.id)
            .order_by(WorkspaceMember.joined_at, WorkspaceMember.account_id)
        )
    )
    invitations = list(
        session.scalars(
            select(WorkspaceInvitation)
            .where(WorkspaceInvitation.workspace_id == workspace.id)
            .order_by(WorkspaceInvitation.created_at, WorkspaceInvitation.id)
        )
    )
    session.add(
        SecurityAudit(
            action="workspace_maintenance_access",
            operator=operator.strip(),
            reason=reason.strip(),
            scope=str(workspace.id),
        )
    )
    _commit_or_rollback(session)
    return {
        "workspaceId": str(workspace.id),
        "memberAccountIds": [str(member.account_id) for member in members],
        "invitations": [
            {
                "id": str(invitation.id),
                "accountId": str(invitation.account_id),
                "status": invitation.status,
            }
            for invitation in invitations
        ],
    }
