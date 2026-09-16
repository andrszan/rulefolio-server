from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app import model_registry  # noqa: F401
from app.access.context import (
    set_actor,
    set_invitation_credential,
    set_notification_todo_source_scope,
    set_workspace_invitation_inbox_scope,
)
from app.core.config import settings
from app.core.database import SessionLocal
from app.identity import service as identity_service
from app.identity.models import Account, OneTimeCredential
from app.notifications import dispatcher
from app.notifications import service as notifications_service
from app.notifications.models import MailOutbox, NotificationTodo
from app.workspaces import service as workspaces_service
from app.workspaces.models import WorkspaceInvitation

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test", reason="需要 DB_NAME=rulefolio_test"
)


def test_invitation_event_todo_retry_idempotency_and_dispatch_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    owner = Account(email=f"notifications-owner-{suffix}@example.com", status="active")
    recipient = Account(
        email=f"notifications-recipient-{suffix}@example.com", status="active"
    )
    other = Account(email=f"notifications-other-{suffix}@example.com", status="active")

    with SessionLocal() as session:
        session.add_all((owner, recipient, other))
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"通知工作空间-{suffix[:8]}", None
        )
        set_actor(session, owner.id)
        invitation = workspaces_service.create_invitation(
            session, owner.id, workspace.id, recipient.email
        )
        assert list(session.scalars(select(NotificationTodo))) == []

        set_actor(session, recipient.id)
        invitation_todos, total = notifications_service.list_todos(
            session, recipient.id, "open", 1, 20
        )
        assert total == 1
        assert invitation_todos[0].kind == "workspace_invitation"
        assert invitation_todos[0].target_id == invitation.id
        joined = workspaces_service.exchange_inbox_invitation(
            session, recipient.id, invitation.id, "inbox-operation"
        )
        replayed = workspaces_service.exchange_inbox_invitation(
            session, recipient.id, invitation.id, "inbox-operation"
        )
        assert joined.workspace.id == replayed.workspace.id == workspace.id
        set_actor(session, recipient.id)
        assert (
            notifications_service.list_todos(session, recipient.id, "completed", 1, 20)[
                1
            ]
            == 1
        )

        todo, created = notifications_service.create_todo(
            session,
            recipient_account_id=recipient.id,
            workspace_id=workspace.id,
            work_id=None,
            kind="issue_opened",
            target_kind="issue",
            target_id=uuid4(),
            source_key="retry-check",
            summary="有新的问题需要处理",
            context_label="问题：重投验证",
        )
        assert created
        todo_id = todo.id
        failed = notifications_service.enqueue_business_mail(
            session,
            recipient.id,
            "issue_opened",
            "有新的问题需要处理",
            "请登录 Rulefolio 查看。",
            business_scope=f"issue:{todo.target_id}",
            todo_id=todo_id,
        )
        failed.status = "failed"
        session.commit()

        set_actor(session, recipient.id)
        first = notifications_service.retry_failed_mail(
            session, recipient.id, todo_id, "retry-operation"
        )
        set_actor(session, recipient.id)
        repeated = notifications_service.retry_failed_mail(
            session, recipient.id, todo_id, "retry-operation"
        )
        assert first.id == repeated.id
        assert (
            session.scalar(
                select(func.count())
                .select_from(MailOutbox)
                .where(MailOutbox.todo_id == todo_id)
            )
            == 2
        )
        set_actor(session, recipient.id)
        with pytest.raises(notifications_service.NotificationTodoConflict) as conflict:
            notifications_service.retry_failed_mail(
                session, recipient.id, todo_id, "different-retry-operation"
            )
        assert conflict.value.reason == "notification_mail_retry_used"

        set_actor(session, other.id)
        assert (
            notifications_service.list_todos(session, other.id, "open", 1, 20)[1] == 0
        )
        with pytest.raises(notifications_service.NotificationTodoUnavailable):
            notifications_service.complete_todo(session, other.id, todo_id)

        set_notification_todo_source_scope(
            session, recipient.id, "issue_opened", "retry-check"
        )
        protected = session.scalar(
            select(NotificationTodo)
            .where(NotificationTodo.id == todo_id)
            .with_for_update()
        )
        assert protected is not None
        protected.status = "cancelled"
        protected.resolved_at = None
        session.execute(
            update(MailOutbox)
            .where(MailOutbox.todo_id == todo_id, MailOutbox.id != failed.id)
            .values(status="pending", smtp_started_at=None, claim_id=None)
        )
        session.commit()
        retry_outbox = session.scalar(
            select(MailOutbox).where(
                MailOutbox.todo_id == todo_id,
                MailOutbox.status == "pending",
            )
        )
        assert retry_outbox is not None
        claim = dispatcher.DispatchClaim(retry_outbox.id, uuid4(), 1)
        retry_outbox.status = "sending"
        retry_outbox.claim_id = claim.claim_id
        session.commit()

        monkeypatch.setattr(dispatcher, "_claim_next", lambda _: claim)
        monkeypatch.setattr(dispatcher, "_send", lambda *_: pytest.fail("不应发送"))
        assert dispatcher.dispatch_one(session) == "cancelled"


def test_expired_inbox_invitation_cancels_its_todo() -> None:
    suffix = uuid4().hex
    owner = Account(email=f"notifications-owner-{suffix}@example.com", status="active")
    recipient = Account(
        email=f"notifications-recipient-{suffix}@example.com", status="active"
    )

    with SessionLocal() as session:
        session.add_all((owner, recipient))
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"通知工作空间-{suffix[:8]}", None
        )
        set_actor(session, owner.id)
        invitation = workspaces_service.create_invitation(
            session, owner.id, workspace.id, recipient.email
        )

        set_actor(session, recipient.id)
        set_workspace_invitation_inbox_scope(session, invitation.id)
        stored_invitation = session.scalar(
            select(WorkspaceInvitation)
            .where(WorkspaceInvitation.id == invitation.id)
            .with_for_update()
        )
        assert stored_invitation is not None
        set_invitation_credential(session, stored_invitation.credential_id)
        credential = session.scalar(
            select(OneTimeCredential)
            .where(OneTimeCredential.id == stored_invitation.credential_id)
            .with_for_update()
        )
        assert credential is not None
        credential.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

        open_todos, open_total = notifications_service.list_todos(
            session, recipient.id, "open", 1, 20
        )
        assert open_todos == []
        assert open_total == 0

        set_actor(session, recipient.id)
        set_workspace_invitation_inbox_scope(session, invitation.id)
        expired_invitation = session.scalar(
            select(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation.id)
        )
        assert expired_invitation is not None
        assert expired_invitation.status == "expired"
        set_invitation_credential(session, expired_invitation.credential_id)
        expired_credential = session.scalar(
            select(OneTimeCredential).where(
                OneTimeCredential.id == expired_invitation.credential_id
            )
        )
        assert expired_credential is not None
        assert expired_credential.status == identity_service.TOKEN_REVOKED
        assert (
            session.scalar(
                select(MailOutbox.status).where(
                    MailOutbox.credential_id == expired_credential.id
                )
            )
            == "cancelled"
        )

        set_actor(session, recipient.id)
        cancelled, total = notifications_service.list_todos(
            session, recipient.id, "cancelled", 1, 20
        )
        assert total == 1
        assert cancelled[0].target_id == invitation.id


def test_expired_inbox_invitation_preserves_started_token_outbox() -> None:
    suffix = uuid4().hex
    owner = Account(email=f"notifications-owner-{suffix}@example.com", status="active")
    recipient = Account(
        email=f"notifications-recipient-{suffix}@example.com", status="active"
    )

    with SessionLocal() as session:
        session.add_all((owner, recipient))
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"通知工作空间-{suffix[:8]}", None
        )
        set_actor(session, owner.id)
        invitation = workspaces_service.create_invitation(
            session, owner.id, workspace.id, recipient.email
        )

        set_actor(session, recipient.id)
        set_workspace_invitation_inbox_scope(session, invitation.id)
        stored_invitation = session.scalar(
            select(WorkspaceInvitation)
            .where(WorkspaceInvitation.id == invitation.id)
            .with_for_update()
        )
        assert stored_invitation is not None
        set_invitation_credential(session, stored_invitation.credential_id)
        credential = session.scalar(
            select(OneTimeCredential)
            .where(OneTimeCredential.id == stored_invitation.credential_id)
            .with_for_update()
        )
        assert credential is not None
        started_outbox = session.scalar(
            select(MailOutbox)
            .where(MailOutbox.credential_id == credential.id)
            .with_for_update()
        )
        assert started_outbox is not None
        credential.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        started_outbox.status = "sending"
        started_outbox.claim_id = uuid4()
        started_outbox.smtp_started_at = datetime.now(UTC)
        session.commit()

        open_todos, open_total = notifications_service.list_todos(
            session, recipient.id, "open", 1, 20
        )
        assert open_todos == []
        assert open_total == 0

        set_actor(session, recipient.id)
        set_invitation_credential(session, credential.id)
        persisted_outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.id == started_outbox.id)
        )
        assert persisted_outbox is not None
        assert persisted_outbox.status == "sending"
        assert persisted_outbox.claim_id is not None
        assert persisted_outbox.token_ciphertext is not None
