from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.access.context import (
    set_actor,
    set_project_baseline_scope,
    set_work_access_cleanup_scope,
    set_workspace_exit_maintenance_scope,
    set_workspace_exit_processor,
    set_workspace_management_scope,
)
from app.audit.models import SecurityAudit
from app.core.config import settings
from app.core.database import SessionLocal
from app.identity import service as identity_service
from app.identity.models import Account, OneTimeCredential, SessionRecord
from app.notifications.models import MailOutbox, NotificationTodo
from app.works import service as works_service
from app.works.models import Work
from app.workspaces import service as workspaces_service
from app.workspaces.models import (
    WorkAccess,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test", reason="需要 DB_NAME=rulefolio_test"
)


def _create_work(session, owner_id, workspace_id):
    return works_service.create_work(
        session,
        owner_id,
        workspace_id,
        name="退出验证作品",
        description="用于验证退出后的访问收敛。",
        creative_stage="原型",
        target_experience="协作",
        min_players=2,
        max_players=4,
        estimated_duration_minutes=60,
    )


def _set_exit_maintenance_scopes(session, workspace_id):
    set_workspace_exit_processor(session)
    set_workspace_exit_maintenance_scope(session, workspace_id)
    set_workspace_management_scope(session, workspace_id)
    set_work_access_cleanup_scope(session, workspace_id)


def test_concurrent_exit_retries_with_same_key_return_same_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    read_until = datetime.now(UTC) + timedelta(hours=1)
    with SessionLocal() as session:
        owner = identity_service.create_active_baseline_account(
            session, f"br014-concurrent-{suffix}@example.com", "unused-password"
        )
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"并发退出-{suffix[:8]}", None
        )

    barrier = Barrier(2)
    monkeypatch.setattr(
        identity_service,
        "verify_current_password",
        lambda *_: (barrier.wait(timeout=10), True)[1],
    )

    def request_exit() -> workspaces_service.WorkspaceData:
        with SessionLocal() as session:
            set_actor(session, owner.id)
            return workspaces_service.request_workspace_exit(
                session,
                owner.id,
                workspace.id,
                "unused-password",
                workspace.revision,
                read_until,
                "same-operation",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request_exit) for _ in range(2)]
        first, second = (future.result(timeout=20) for future in futures)

    assert first == second
    with SessionLocal() as session:
        set_actor(session, owner.id)
        assert (
            session.scalar(
                select(func.count())
                .select_from(SecurityAudit)
                .where(
                    SecurityAudit.action == "workspace_exit_requested",
                    SecurityAudit.scope.like(f"workspace:{workspace.id};%"),
                )
            )
            == 1
        )


def test_workspace_exit_blocks_writes_then_revokes_related_access() -> None:
    suffix = uuid4().hex
    password = "current-password-for-exit"
    with SessionLocal() as session:
        owner = identity_service.create_active_baseline_account(
            session, f"br014-owner-{suffix}@example.com", password
        )
        member = Account(email=f"br014-member-{suffix}@example.com", status="active")
        invitee = Account(email=f"br014-invitee-{suffix}@example.com", status="active")
        session.add_all((member, invitee))
        session.flush()
        session.add_all(
            (
                SessionRecord(
                    account_id=owner.id,
                    token_hash=uuid4().bytes + uuid4().bytes,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                SessionRecord(
                    account_id=member.id,
                    token_hash=uuid4().bytes + uuid4().bytes,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
            )
        )
        session.commit()

        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"退出验证-{suffix[:8]}", None
        )
        set_actor(session, owner.id)
        set_workspace_management_scope(session, workspace.id)
        session.add(WorkspaceMember(workspace_id=workspace.id, account_id=member.id))
        session.commit()

        set_actor(session, owner.id)
        work = _create_work(session, owner.id, workspace.id)
        set_actor(session, owner.id)
        works_service.set_work_access(
            session, owner.id, workspace.id, work.id, member.id, "collaborator"
        )
        set_actor(session, owner.id)
        invitation = workspaces_service.create_invitation(
            session, owner.id, workspace.id, invitee.email
        )
        outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.workspace_name == workspace.name)
        )
        assert outbox is not None

        set_actor(session, owner.id)
        with pytest.raises(workspaces_service.WorkspaceExitReauthenticationFailed):
            workspaces_service.request_workspace_exit(
                session,
                owner.id,
                workspace.id,
                "incorrect-password",
                workspace.revision,
                datetime.now(UTC) + timedelta(hours=1),
                "wrong-password",
            )

        set_actor(session, owner.id)
        exiting = workspaces_service.request_workspace_exit(
            session,
            owner.id,
            workspace.id,
            password,
            workspace.revision,
            datetime.now(UTC) + timedelta(hours=1),
            "exit-operation",
        )
        assert exiting.access_state == "exiting"
        assert exiting.revision == workspace.revision + 1
        assert exiting.read_until is not None

        set_actor(session, owner.id)
        retry = workspaces_service.request_workspace_exit(
            session,
            owner.id,
            workspace.id,
            password,
            workspace.revision,
            datetime.now(UTC) + timedelta(hours=1),
            "exit-operation",
        )
        assert retry == exiting
        assert (
            session.scalar(
                select(func.count())
                .select_from(SecurityAudit)
                .where(
                    SecurityAudit.action == "workspace_exit_requested",
                    SecurityAudit.scope.like(f"workspace:{workspace.id};%"),
                )
            )
            == 1
        )

        set_actor(session, owner.id)
        with pytest.raises(workspaces_service.WorkspaceExitInProgress):
            workspaces_service.request_workspace_exit(
                session,
                owner.id,
                workspace.id,
                password,
                workspace.revision,
                datetime.now(UTC) + timedelta(hours=1),
                "different-operation",
            )

        set_actor(session, member.id)
        assert (
            works_service.read_work(session, member.id, workspace.id, work.id).id
            == work.id
        )
        set_actor(session, owner.id)
        with pytest.raises(workspaces_service.WorkspaceExitInProgress):
            works_service.update_work(
                session,
                owner.id,
                workspace.id,
                work.id,
                name="不应保存的修改",
                description=work.description,
                creative_stage=work.creative_stage,
                target_experience=work.target_experience,
                min_players=work.min_players,
                max_players=work.max_players,
                estimated_duration_minutes=work.estimated_duration_minutes,
                expected_revision=work.revision,
            )

        _set_exit_maintenance_scopes(session, workspace.id)
        due_workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace.id).with_for_update()
        )
        assert due_workspace is not None
        due_workspace.exit_read_until = session.scalar(
            select(func.current_timestamp())
        ) - timedelta(days=1)
        session.commit()

        set_actor(session, member.id)
        with pytest.raises(works_service.WorkUnavailable):
            works_service.read_work(session, member.id, workspace.id, work.id)

        assert (
            workspaces_service.process_next_due_workspace_exit(session) == "completed"
        )

        _set_exit_maintenance_scopes(session, workspace.id)
        completed_workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace.id)
        )
        assert completed_workspace is not None
        assert completed_workspace.exit_completed_at is not None
        assert not list(
            session.scalars(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace.id
                )
            )
        )
        assert not list(
            session.scalars(
                select(WorkAccess).where(WorkAccess.workspace_id == workspace.id)
            )
        )
        cancelled_invitation = session.scalar(
            select(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation.id)
        )
        assert cancelled_invitation is not None
        assert cancelled_invitation.status == "revoked"
        credential = session.scalar(
            select(OneTimeCredential).where(
                OneTimeCredential.id == cancelled_invitation.credential_id
            )
        )
        assert credential is not None
        assert credential.status == identity_service.TOKEN_REVOKED
        cancelled_outbox = session.scalar(
            select(MailOutbox).where(MailOutbox.id == outbox.id)
        )
        assert cancelled_outbox is not None
        assert cancelled_outbox.status == "cancelled"
        assert cancelled_outbox.token_ciphertext is None
        set_actor(session, invitee.id)
        todo = session.scalar(
            select(NotificationTodo).where(
                NotificationTodo.workspace_id == workspace.id
            )
        )
        assert todo is not None
        assert todo.status == "cancelled"
        revoked_sessions = list(
            session.scalars(
                select(SessionRecord).where(
                    SessionRecord.account_id.in_((owner.id, member.id)),
                    SessionRecord.revoke_reason == "workspace_exit",
                )
            )
        )
        assert len(revoked_sessions) == 2
        set_project_baseline_scope(session)
        assert session.scalar(select(Work.id).where(Work.id == work.id)) == work.id
        assert (
            session.scalar(
                select(func.count())
                .select_from(SecurityAudit)
                .where(
                    SecurityAudit.action == "workspace_exit_completed",
                    SecurityAudit.scope == f"workspace:{workspace.id}",
                )
            )
            == 1
        )
