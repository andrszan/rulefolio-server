import os
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access.context import (
    set_actor,
    set_notification_todo_source_scope,
    set_notification_todo_target_scope,
    set_notification_todo_work_cleanup_scope,
)
from app.core.config import settings
from app.notifications.models import MailOutbox, NotificationTodo

TODO_MAINTENANCE_KINDS = frozenset(
    {
        "feedback_submitted",
        "issue_opened",
        "retest_arrangement_needed",
        "retest_arranged",
    }
)


class NotificationTodoUnavailable(Exception):
    pass


@dataclass(frozen=True)
class NotificationTodoConflict(Exception):
    reason: str


@dataclass(frozen=True)
class NotificationTodoData:
    id: UUID
    kind: str
    summary: str
    context_label: str
    workspace_id: UUID
    work_id: UUID | None
    target_kind: str
    target_id: UUID
    status: str
    created_at: datetime
    resolved_at: datetime | None
    mail_status: str | None
    last_attempt_at: datetime | None
    can_retry_mail: bool


def _associated_data(credential_id: UUID, purpose: str) -> bytes:
    return f"{credential_id}:{purpose}:{settings.token_encryption_key_version}".encode()


def _now() -> datetime:
    return datetime.now(UTC)


def _mail_status(outbox: MailOutbox | None) -> str | None:
    if outbox is None:
        return None
    if outbox.status in {"pending", "sending"}:
        return "pending"
    if outbox.status in {"cancelled", "suppressed"}:
        return "cancelled"
    return outbox.status


def _latest_mail(
    session: Session, todo_id: UUID, *, lock: bool = False
) -> MailOutbox | None:
    statement = (
        select(MailOutbox)
        .where(MailOutbox.todo_id == todo_id)
        .order_by(MailOutbox.created_at.desc(), MailOutbox.id.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def todo_data(session: Session, todo: NotificationTodo) -> NotificationTodoData:
    outbox = _latest_mail(session, todo.id)
    return NotificationTodoData(
        id=todo.id,
        kind=todo.kind,
        summary=todo.summary,
        context_label=todo.context_label,
        workspace_id=todo.workspace_id,
        work_id=todo.work_id,
        target_kind=todo.target_kind,
        target_id=todo.target_id,
        status=todo.status,
        created_at=todo.created_at,
        resolved_at=todo.resolved_at,
        mail_status=_mail_status(outbox),
        last_attempt_at=(
            outbox.smtp_started_at or outbox.dispatch_started_at
            if outbox is not None
            else None
        ),
        can_retry_mail=(
            todo.status == "open"
            and not todo.retry_used
            and outbox is not None
            and outbox.status == "failed"
            and outbox.credential_id is None
        ),
    )


def business_todo_eligible(session: Session, todo: NotificationTodo) -> bool:
    """将当前资格复核委托给持有对应业务事实的所有者。"""
    if todo.work_id is None:
        return True
    if todo.kind in {
        "playtest_invitation",
        "playtest_arrangement_updated",
        "playtest_material_updated",
        "playtest_cancelled",
    }:
        from app.playtests import service as playtests_service

        return playtests_service.notification_todo_eligible(session, todo)
    from app.issues import service as issues_service

    return issues_service.notification_todo_eligible(session, todo)


def create_todo(
    session: Session,
    *,
    recipient_account_id: UUID,
    workspace_id: UUID,
    work_id: UUID | None,
    kind: str,
    target_kind: str,
    target_id: UUID,
    source_key: str,
    summary: str,
    context_label: str,
) -> tuple[NotificationTodo, bool]:
    """在业务所有者已经核验目标和收件资格的事务中创建幂等待办。"""
    set_notification_todo_source_scope(session, recipient_account_id, kind, source_key)
    existing = session.scalar(
        select(NotificationTodo)
        .where(
            NotificationTodo.recipient_account_id == recipient_account_id,
            NotificationTodo.kind == kind,
            NotificationTodo.source_key == source_key,
        )
        .with_for_update()
    )
    if existing is not None:
        return existing, False
    todo = NotificationTodo(
        recipient_account_id=recipient_account_id,
        workspace_id=workspace_id,
        work_id=work_id,
        kind=kind,
        target_kind=target_kind,
        target_id=target_id,
        source_key=source_key,
        summary=summary,
        context_label=context_label,
    )
    session.add(todo)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(NotificationTodo)
            .where(
                NotificationTodo.recipient_account_id == recipient_account_id,
                NotificationTodo.kind == kind,
                NotificationTodo.source_key == source_key,
            )
            .with_for_update()
        )
        if existing is None:
            raise
        return existing, False
    return todo, True


def enqueue_token_mail(
    session: Session,
    credential_id: UUID,
    account_id: UUID,
    purpose: str,
    token: str,
    workspace_name: str | None = None,
    *,
    todo_id: UUID | None = None,
) -> MailOutbox:
    nonce = os.urandom(12)
    ciphertext = AESGCM(settings.token_encryption_key_bytes).encrypt(
        nonce,
        token.encode(),
        _associated_data(credential_id, purpose),
    )
    outbox = MailOutbox(
        credential_id=credential_id,
        todo_id=todo_id,
        recipient_account_id=account_id,
        purpose=purpose,
        workspace_name=workspace_name,
        token_ciphertext=ciphertext,
        token_nonce=nonce,
        key_version=settings.token_encryption_key_version,
    )
    session.add(outbox)
    return outbox


def enqueue_business_mail(
    session: Session,
    account_id: UUID,
    purpose: str,
    subject: str,
    body: str,
    *,
    business_scope: str,
    todo_id: UUID | None = None,
    retry_operation_key: str | None = None,
) -> MailOutbox:
    """写入已冻结正文的业务邮件，不附带一次性凭据。"""
    outbox = MailOutbox(
        todo_id=todo_id,
        retry_operation_key=retry_operation_key,
        recipient_account_id=account_id,
        purpose=purpose,
        business_scope=business_scope,
        frozen_subject=subject,
        frozen_body=body,
    )
    session.add(outbox)
    session.flush()
    return outbox


def _stop_todo_mail(session: Session, todo_id: UUID, status: str = "cancelled") -> None:
    session.execute(
        update(MailOutbox)
        .where(
            MailOutbox.todo_id == todo_id,
            or_(
                MailOutbox.status == "pending",
                and_(
                    MailOutbox.status == "sending",
                    MailOutbox.smtp_started_at.is_(None),
                ),
            ),
        )
        .values(status=status, claim_id=None)
    )


def complete_todo_by_source(
    session: Session, recipient_id: UUID, kind: str, source_key: str
) -> None:
    set_notification_todo_source_scope(session, recipient_id, kind, source_key)
    todo = session.scalar(
        select(NotificationTodo)
        .where(
            NotificationTodo.recipient_account_id == recipient_id,
            NotificationTodo.kind == kind,
            NotificationTodo.source_key == source_key,
        )
        .with_for_update()
    )
    if todo is not None and todo.status == "open":
        todo.status = "completed"
        todo.resolved_at = _now()
        _stop_todo_mail(session, todo.id)


def cancel_todo_by_source(
    session: Session, recipient_id: UUID, kind: str, source_key: str
) -> None:
    set_notification_todo_source_scope(session, recipient_id, kind, source_key)
    todo = session.scalar(
        select(NotificationTodo)
        .where(
            NotificationTodo.recipient_account_id == recipient_id,
            NotificationTodo.kind == kind,
            NotificationTodo.source_key == source_key,
        )
        .with_for_update()
    )
    if todo is not None and todo.status == "open":
        todo.status = "cancelled"
        todo.resolved_at = _now()
        _stop_todo_mail(session, todo.id)


def cancel_todos_for_target(
    session: Session,
    target_kind: str,
    target_id: UUID,
    *,
    kinds: set[str] | None = None,
) -> None:
    set_notification_todo_target_scope(session, target_kind, target_id)
    statement = select(NotificationTodo).where(
        NotificationTodo.target_kind == target_kind,
        NotificationTodo.target_id == target_id,
        NotificationTodo.status == "open",
    )
    if kinds is not None:
        statement = statement.where(NotificationTodo.kind.in_(kinds))
    for todo in session.scalars(statement.with_for_update()):
        todo.status = "cancelled"
        todo.resolved_at = _now()
        _stop_todo_mail(session, todo.id)


def complete_todos_for_target(
    session: Session,
    target_kind: str,
    target_id: UUID,
    *,
    kinds: set[str] | None = None,
) -> None:
    set_notification_todo_target_scope(session, target_kind, target_id)
    statement = select(NotificationTodo).where(
        NotificationTodo.target_kind == target_kind,
        NotificationTodo.target_id == target_id,
        NotificationTodo.status == "open",
    )
    if kinds is not None:
        statement = statement.where(NotificationTodo.kind.in_(kinds))
    for todo in session.scalars(statement.with_for_update()):
        todo.status = "completed"
        todo.resolved_at = _now()
        _stop_todo_mail(session, todo.id)


def delete_work_access_todos(
    session: Session, recipient_id: UUID, work_id: UUID
) -> None:
    """撤销维护资格时删除会泄露作品上下文的待办。"""
    set_notification_todo_work_cleanup_scope(session, recipient_id, work_id)
    todos = list(
        session.scalars(
            select(NotificationTodo)
            .where(
                NotificationTodo.recipient_account_id == recipient_id,
                NotificationTodo.work_id == work_id,
                NotificationTodo.kind.in_(TODO_MAINTENANCE_KINDS),
            )
            .with_for_update()
        )
    )
    for todo in todos:
        _stop_todo_mail(session, todo.id)
        session.delete(todo)


def complete_todo(
    session: Session, recipient_id: UUID, todo_id: UUID
) -> NotificationTodoData:
    todo = session.scalar(
        select(NotificationTodo)
        .where(
            NotificationTodo.id == todo_id,
            NotificationTodo.recipient_account_id == recipient_id,
        )
        .with_for_update()
    )
    if todo is None:
        session.rollback()
        raise NotificationTodoUnavailable
    if todo.status == "cancelled":
        session.rollback()
        raise NotificationTodoConflict("notification_todo_not_open")
    if todo.status == "open":
        todo.status = "completed"
        todo.resolved_at = _now()
        _stop_todo_mail(session, todo.id)
    session.commit()
    return todo_data(session, todo)


def retry_failed_mail(
    session: Session, recipient_id: UUID, todo_id: UUID, operation_key: str
) -> NotificationTodoData:
    todo = session.scalar(
        select(NotificationTodo)
        .where(
            NotificationTodo.id == todo_id,
            NotificationTodo.recipient_account_id == recipient_id,
        )
        .with_for_update()
    )
    if todo is None:
        session.rollback()
        raise NotificationTodoUnavailable
    existing_retry = session.scalar(
        select(MailOutbox)
        .where(
            MailOutbox.todo_id == todo.id,
            MailOutbox.retry_operation_key == operation_key,
        )
        .with_for_update()
    )
    if existing_retry is not None:
        session.commit()
        return todo_data(session, todo)
    if todo.status != "open":
        session.rollback()
        raise NotificationTodoConflict("notification_todo_not_open")
    if todo.retry_used:
        session.rollback()
        raise NotificationTodoConflict("notification_mail_retry_used")
    if not business_todo_eligible(session, todo):
        session.rollback()
        raise NotificationTodoConflict("notification_mail_retry_unavailable")
    failed = _latest_mail(session, todo.id, lock=True)
    if (
        failed is None
        or failed.status != "failed"
        or failed.credential_id is not None
        or failed.frozen_subject is None
        or failed.frozen_body is None
        or failed.business_scope is None
    ):
        session.rollback()
        raise NotificationTodoConflict("notification_mail_retry_unavailable")
    todo.retry_used = True
    enqueue_business_mail(
        session,
        todo.recipient_account_id,
        failed.purpose,
        failed.frozen_subject,
        failed.frozen_body,
        business_scope=failed.business_scope,
        todo_id=todo.id,
        retry_operation_key=operation_key,
    )
    session.commit()
    return todo_data(session, todo)


def _expire_open_workspace_invitation_todos(
    session: Session, recipient_id: UUID
) -> None:
    invitation_ids = list(
        session.scalars(
            select(NotificationTodo.target_id).where(
                NotificationTodo.recipient_account_id == recipient_id,
                NotificationTodo.kind == "workspace_invitation",
                NotificationTodo.status == "open",
            )
        )
    )
    if not invitation_ids:
        return
    from app.workspaces import service as workspaces_service

    expired = False
    for invitation_id in invitation_ids:
        expired = (
            workspaces_service.expire_inbox_invitation(
                session, recipient_id, invitation_id, _now()
            )
            or expired
        )
    if expired:
        session.commit()
    set_actor(session, recipient_id)


def list_todos(
    session: Session, recipient_id: UUID, status: str, page: int, size: int
) -> tuple[list[NotificationTodoData], int]:
    set_actor(session, recipient_id)
    if status == "open":
        _expire_open_workspace_invitation_todos(session, recipient_id)
    total = (
        session.scalar(
            select(func.count())
            .select_from(NotificationTodo)
            .where(
                NotificationTodo.recipient_account_id == recipient_id,
                NotificationTodo.status == status,
            )
        )
        or 0
    )
    todos = list(
        session.scalars(
            select(NotificationTodo)
            .where(
                NotificationTodo.recipient_account_id == recipient_id,
                NotificationTodo.status == status,
            )
            .order_by(NotificationTodo.created_at.desc(), NotificationTodo.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    )
    return [todo_data(session, todo) for todo in todos], total


def suppress_business_mails(session: Session, business_scope: str) -> None:
    """取消场次时只抑制尚未被 SMTP 领取的本场业务邮件。"""
    session.execute(
        update(MailOutbox)
        .where(
            MailOutbox.business_scope == business_scope,
            MailOutbox.credential_id.is_(None),
            or_(
                MailOutbox.status == "pending",
                and_(
                    MailOutbox.status == "sending",
                    MailOutbox.smtp_started_at.is_(None),
                ),
            ),
        )
        .values(status="suppressed", claim_id=None)
    )


def clear_outbox_envelopes(
    session: Session, credential_ids: list[UUID], status: str = "cancelled"
) -> None:
    if not credential_ids:
        return
    session.execute(
        update(MailOutbox)
        .where(
            MailOutbox.credential_id.in_(credential_ids),
            MailOutbox.status.in_(("pending", "sending", "cancelled")),
            MailOutbox.smtp_started_at.is_(None),
        )
        .values(
            status=status,
            claim_id=None,
            token_ciphertext=None,
            token_nonce=None,
            key_version=None,
        )
    )


def decrypt_token(outbox: MailOutbox) -> str:
    if outbox.token_ciphertext is None or outbox.token_nonce is None:
        raise ValueError("邮件凭据已清除")
    return (
        AESGCM(settings.token_encryption_key_bytes)
        .decrypt(
            outbox.token_nonce,
            outbox.token_ciphertext,
            _associated_data(outbox.credential_id, outbox.purpose),
        )
        .decode()
    )
