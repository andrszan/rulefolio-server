from threading import Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access.context import set_actor
from app.core.config import settings
from app.core.database import SessionLocal
from app.identity import service as identity_service
from app.identity.models import Account, OneTimeCredential
from app.notifications import dispatcher
from app.notifications.models import MailOutbox
from app.notifications.service import decrypt_token
from app.workspaces import service
from app.workspaces.models import Workspace, WorkspaceInvitationAttempt

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test", reason="需要 DB_NAME=rulefolio_test"
)


def test_workspace_invitation_uses_per_workspace_active_constraint_and_rls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    owner = Account(email=f"br002-owner-{suffix}@example.com", status="active")
    recipient = Account(email=f"br002-member-{suffix}@example.com", status="active")

    with SessionLocal() as session:
        session.add_all((owner, recipient))
        session.commit()

        set_actor(session, owner.id)
        first_workspace = service.create_workspace(
            session, owner.id, f"第一个工作空间-{suffix[:8]}", None
        )
        set_actor(session, owner.id)
        second_workspace = service.create_workspace(
            session, owner.id, f"第二个工作空间-{suffix[:8]}", None
        )
        diagnostic = service.diagnose_workspace(
            session, first_workspace.id, "pytest", "BR-002 集成验证"
        )
        assert diagnostic["workspaceId"] == str(first_workspace.id)
        assert diagnostic["memberAccountIds"] == [str(owner.id)]

        set_actor(session, owner.id)
        first_invitation = service.create_invitation(
            session, owner.id, first_workspace.id, recipient.email
        )
        set_actor(session, owner.id)
        second_invitation = service.create_invitation(
            session, owner.id, second_workspace.id, recipient.email
        )
        assert first_invitation.status == second_invitation.status == "active"

        credentials = list(
            session.scalars(
                select(OneTimeCredential).where(
                    OneTimeCredential.account_id == recipient.id,
                    OneTimeCredential.purpose == "workspace_invitation",
                )
            )
        )
        assert len(credentials) == 2
        assert all(credential.status == "active" for credential in credentials)

        first_outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.workspace_name == first_workspace.name)
        )
        assert first_outbox is not None
        invitation_token = decrypt_token(first_outbox)
        assert invitation_token.encode() not in first_outbox.token_ciphertext

        claim = dispatcher.DispatchClaim(first_outbox.id, uuid4(), 1)
        first_outbox.status = "sending"
        first_outbox.claim_id = claim.claim_id
        session.commit()
        sent: list[tuple[str, str, str]] = []
        monkeypatch.setattr(dispatcher, "_claim_next", lambda _: claim)
        monkeypatch.setattr(
            dispatcher,
            "_send",
            lambda address, subject, body: sent.append((address, subject, body)),
        )
        assert dispatcher.dispatch_one(session) == "accepted"
        assert len(sent) == 1
        address, subject, body = sent[0]
        assert address == recipient.email
        assert subject == "加入好玩实验室工作空间"
        assert first_workspace.name in body
        assert f"/auth/invitation#token={invitation_token}" in body

        set_actor(session, recipient.id)
        exchange = service.exchange_current_session_invitation(
            session, recipient.id, invitation_token, "br002-exchange-1"
        )
        assert exchange.workspace.id == first_workspace.id
        with pytest.raises(service.WorkspaceInvitationUnavailable):
            service.exchange_account_activation_invitation(
                session, invitation_token, "valid-password", "br002-exchange-1"
            )

        assert not list(session.scalars(select(Workspace)))

        set_actor(session, owner.id)
        revoked = service.revoke_invitation(
            session, owner.id, second_workspace.id, second_invitation.id
        )
        assert revoked.status == "revoked"
        second_outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.workspace_name == second_workspace.name)
        )
        assert second_outbox is not None
        assert second_outbox.status == "cancelled"
        assert second_outbox.token_ciphertext is None

        set_actor(session, owner.id)
        service.remove_member(session, owner.id, exchange.workspace.id, recipient.id)
        set_actor(session, recipient.id)
        with pytest.raises(service.WorkspaceUnavailable):
            service.read_workspace(session, recipient.id, exchange.workspace.id)


def test_unknown_invitation_tokens_do_not_create_attempt_records() -> None:
    unknown_token = f"unknown-invitation-{uuid4().hex}"
    with SessionLocal() as session:
        initial_count = session.scalar(
            select(func.count()).select_from(WorkspaceInvitationAttempt)
        )
        with pytest.raises(service.WorkspaceInvitationUnavailable):
            service.exchange_current_session_invitation(
                session, uuid4(), unknown_token, "unknown-current-session"
            )
        with pytest.raises(service.WorkspaceInvitationUnavailable):
            service.exchange_account_activation_invitation(
                session, unknown_token, "valid-password", "unknown-account-activation"
            )
        assert (
            session.scalar(select(func.count()).select_from(WorkspaceInvitationAttempt))
            == initial_count
        )

        suffix = uuid4().hex
        orphan = Account(email=f"br002-orphan-{suffix}@example.com", status="active")
        session.add(orphan)
        session.commit()
        _, orphan_token = identity_service.issue_workspace_invitation_credential(
            session, orphan
        )
        session.commit()
        with pytest.raises(service.WorkspaceInvitationUnavailable):
            service.exchange_current_session_invitation(
                session, orphan.id, orphan_token, "orphan-invitation"
            )
        assert (
            session.scalar(select(func.count()).select_from(WorkspaceInvitationAttempt))
            == initial_count
        )

        owner = Account(email=f"br002-owner-{suffix}@example.com", status="active")
        recipient = Account(
            email=f"br002-recipient-{suffix}@example.com", status="active"
        )
        session.add_all((owner, recipient))
        session.commit()
        set_actor(session, owner.id)
        workspace_name = f"尝试记录验证-{suffix}"
        workspace = service.create_workspace(session, owner.id, workspace_name, None)
        set_actor(session, owner.id)
        invitation = service.create_invitation(
            session, owner.id, workspace.id, recipient.email
        )
        outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.workspace_name == workspace_name)
        )
        assert outbox is not None
        token = decrypt_token(outbox)
        set_actor(session, owner.id)
        service.revoke_invitation(session, owner.id, workspace.id, invitation.id)

        subject_hash = identity_service.attempt_subject(token)
        with pytest.raises(service.WorkspaceInvitationUnavailable):
            service.exchange_current_session_invitation(
                session, recipient.id, token, "revoked-invitation"
            )
        first_attempt = session.scalar(
            select(WorkspaceInvitationAttempt).where(
                WorkspaceInvitationAttempt.purpose
                == service.INVITATION_ATTEMPT_PURPOSE,
                WorkspaceInvitationAttempt.subject_hash == subject_hash,
            )
        )
        assert first_attempt is not None
        assert first_attempt.count == 1
        assert first_attempt.subject_hash != token.encode()

        with pytest.raises(service.WorkspaceInvitationUnavailable):
            service.exchange_current_session_invitation(
                session, recipient.id, token, "revoked-invitation-retry"
            )
        second_attempt = session.scalar(
            select(WorkspaceInvitationAttempt).where(
                WorkspaceInvitationAttempt.purpose
                == service.INVITATION_ATTEMPT_PURPOSE,
                WorkspaceInvitationAttempt.subject_hash == subject_hash,
            )
        )
        assert second_attempt is not None
        assert second_attempt.id == first_attempt.id
        assert second_attempt.count == 2


def test_invitation_revocation_before_smtp_boundary_suppresses_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    owner = Account(email=f"br002-owner-{suffix}@example.com", status="active")
    recipient = Account(email=f"br002-recipient-{suffix}@example.com", status="active")

    with SessionLocal() as session:
        session.add_all((owner, recipient))
        session.commit()
        set_actor(session, owner.id)
        workspace_name = f"并发撤销验证-{suffix}"
        workspace = service.create_workspace(session, owner.id, workspace_name, None)
        set_actor(session, owner.id)
        invitation = service.create_invitation(
            session, owner.id, workspace.id, recipient.email
        )
        outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.workspace_name == workspace_name)
        )
        assert outbox is not None
        claim = dispatcher.DispatchClaim(outbox.id, uuid4(), 1)
        outbox.status = "sending"
        outbox.claim_id = claim.claim_id
        session.commit()

    original_start_smtp = dispatcher._start_smtp
    dispatch_ready = Event()
    revocation_complete = Event()
    errors: list[Exception] = []
    results: dict[str, object] = {}
    sent: list[tuple[object, ...]] = []

    def start_smtp(session: Session, dispatch_claim: dispatcher.DispatchClaim) -> bool:
        dispatch_ready.set()
        assert revocation_complete.wait(timeout=5)
        return original_start_smtp(session, dispatch_claim)

    monkeypatch.setattr(dispatcher, "_claim_next", lambda _: claim)
    monkeypatch.setattr(dispatcher, "_start_smtp", start_smtp)
    monkeypatch.setattr(dispatcher, "_send", lambda *args: sent.append(args))

    def dispatch() -> None:
        try:
            with SessionLocal() as session:
                results["dispatch"] = dispatcher.dispatch_one(session)
        except Exception as error:
            errors.append(error)

    def revoke() -> None:
        try:
            with SessionLocal() as session:
                set_actor(session, owner.id)
                results["revoke"] = service.revoke_invitation(
                    session, owner.id, workspace.id, invitation.id
                )
        except Exception as error:
            errors.append(error)
        finally:
            revocation_complete.set()

    dispatch_thread = Thread(target=dispatch)
    revoke_thread = Thread(target=revoke)
    dispatch_thread.start()
    assert dispatch_ready.wait(timeout=5)
    revoke_thread.start()
    revoke_thread.join(timeout=5)
    dispatch_thread.join(timeout=5)

    assert not dispatch_thread.is_alive()
    assert not revoke_thread.is_alive()
    assert errors == []
    assert results["dispatch"] == "superseded"
    assert sent == []
    revoked = results["revoke"]
    assert isinstance(revoked, service.WorkspaceInvitationData)
    assert revoked.status == "revoked"

    with SessionLocal() as session:
        cancelled_outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.id == outbox.id)
        )
        assert cancelled_outbox is not None
        assert cancelled_outbox.status == "cancelled"
        assert cancelled_outbox.smtp_started_at is None
