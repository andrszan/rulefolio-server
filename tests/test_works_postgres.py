from threading import Barrier, Thread
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access.context import (
    set_actor,
    set_work_management_scope,
    set_workspace_management_scope,
)
from app.audit.models import SecurityAudit
from app.core.config import settings
from app.core.database import SessionLocal
from app.identity.models import Account
from app.works import service as works_service
from app.works.models import Work
from app.workspaces import service as workspaces_service
from app.workspaces.models import WorkAccess, WorkspaceMember

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test", reason="需要 DB_NAME=rulefolio_test"
)


def _create_work(
    session: Session, actor_id: UUID, workspace_id: UUID, name: str
) -> works_service.WorkData:
    return works_service.create_work(
        session,
        actor_id,
        workspace_id,
        name=name,
        description="私有作品简介",
        creative_stage="原型",
        target_experience="共同探索",
        min_players=2,
        max_players=4,
        estimated_duration_minutes=60,
    )


def test_private_work_access_rls_revision_member_removal_and_audit() -> None:
    suffix = uuid4().hex
    owner = Account(email=f"br003-owner-{suffix}@example.com", status="active")
    organizer = Account(email=f"br003-organizer-{suffix}@example.com", status="active")
    collaborator = Account(
        email=f"br003-collaborator-{suffix}@example.com", status="active"
    )
    creator = Account(email=f"br003-creator-{suffix}@example.com", status="active")

    with SessionLocal() as session:
        session.add_all((owner, organizer, collaborator, creator))
        session.commit()

        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"BR-003-{suffix[:8]}", None
        )
        set_workspace_management_scope(session, workspace.id)
        session.add_all(
            (
                WorkspaceMember(workspace_id=workspace.id, account_id=organizer.id),
                WorkspaceMember(workspace_id=workspace.id, account_id=collaborator.id),
                WorkspaceMember(workspace_id=workspace.id, account_id=creator.id),
            )
        )
        session.commit()

        set_actor(session, creator.id)
        hidden_work = _create_work(session, creator.id, workspace.id, "成员私有作品")
        set_actor(session, owner.id)
        owner_works, owner_total = works_service.list_works(
            session, owner.id, workspace.id, 1, 20
        )
        assert owner_works == []
        assert owner_total == 0
        with pytest.raises(works_service.WorkUnavailable):
            works_service.read_work(session, owner.id, workspace.id, hidden_work.id)

        set_actor(session, owner.id)
        first_work = _create_work(session, owner.id, workspace.id, "第一款私有作品")
        set_actor(session, owner.id)
        second_work = _create_work(session, owner.id, workspace.id, "第二款私有作品")
        set_actor(session, owner.id)
        works_service.set_work_access(
            session,
            owner.id,
            workspace.id,
            first_work.id,
            organizer.id,
            "organizer",
        )
        set_actor(session, owner.id)
        works_service.set_work_access(
            session,
            owner.id,
            workspace.id,
            second_work.id,
            collaborator.id,
            "collaborator",
        )

        set_actor(session, owner.id)
        set_work_management_scope(session, first_work.id, workspace.id)
        session.add(
            WorkAccess(
                work_id=first_work.id,
                workspace_id=workspace.id,
                account_id=collaborator.id,
                role="invalid",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        set_actor(session, organizer.id)
        visible_works, total = works_service.list_works(
            session, organizer.id, workspace.id, 1, 20
        )
        assert total == 1
        assert [work.id for work in visible_works] == [first_work.id]
        assert [
            work.id
            for work in session.scalars(
                select(Work).where(Work.workspace_id == workspace.id)
            )
        ] == [first_work.id]
        with pytest.raises(works_service.WorkUnavailable):
            works_service.read_work(session, organizer.id, workspace.id, second_work.id)

        set_actor(session, organizer.id)
        with pytest.raises(works_service.WorkManagementForbidden):
            works_service.list_access_members(
                session, organizer.id, workspace.id, first_work.id, 1, 20
            )

        set_actor(session, owner.id)
        works_service.set_work_access(
            session,
            owner.id,
            workspace.id,
            first_work.id,
            organizer.id,
            "maintainer",
        )
        set_actor(session, organizer.id)
        works_service.set_work_access(
            session,
            organizer.id,
            workspace.id,
            first_work.id,
            collaborator.id,
            "collaborator",
        )
        set_actor(session, owner.id)
        updated = works_service.update_work(
            session,
            owner.id,
            workspace.id,
            first_work.id,
            name="第一款私有作品（更新）",
            description="更新后的私有作品简介",
            creative_stage="测试",
            target_experience="共同复盘",
            min_players=3,
            max_players=5,
            estimated_duration_minutes=90,
            expected_revision=1,
        )
        assert updated.revision == 2
        set_actor(session, owner.id)
        with pytest.raises(works_service.WorkRevisionConflict):
            works_service.update_work(
                session,
                owner.id,
                workspace.id,
                first_work.id,
                name="过期更新",
                description="过期简介",
                creative_stage="测试",
                target_experience="共同复盘",
                min_players=3,
                max_players=5,
                estimated_duration_minutes=90,
                expected_revision=1,
            )

        set_actor(session, owner.id)
        with pytest.raises(works_service.WorkLastMaintainerRequired):
            works_service.revoke_work_access(
                session, owner.id, workspace.id, second_work.id, owner.id
            )

        set_actor(session, owner.id)
        workspaces_service.remove_member(
            session, owner.id, workspace.id, collaborator.id
        )
        revoked_scopes = list(
            session.scalars(
                select(SecurityAudit.scope)
                .where(
                    SecurityAudit.action == "work_access_revoked",
                    SecurityAudit.actor_account_id == owner.id,
                    SecurityAudit.target_account_id == collaborator.id,
                )
                .order_by(SecurityAudit.scope)
            )
        )
        assert revoked_scopes == sorted(
            (
                f"workspace:{workspace.id}/work:{first_work.id}",
                f"workspace:{workspace.id}/work:{second_work.id}",
            )
        )

        set_actor(session, owner.id)
        set_workspace_management_scope(session, workspace.id)
        session.add(
            WorkspaceMember(workspace_id=workspace.id, account_id=collaborator.id)
        )
        session.commit()
        set_actor(session, collaborator.id)
        restored_works, restored_total = works_service.list_works(
            session, collaborator.id, workspace.id, 1, 20
        )
        assert restored_works == []
        assert restored_total == 0
        with pytest.raises(works_service.WorkUnavailable):
            works_service.read_work(
                session, collaborator.id, workspace.id, first_work.id
            )

        assert session.execute(
            text(
                "SELECT relrowsecurity AND relforcerowsecurity "
                "FROM pg_class WHERE relname = 'works'"
            )
        ).scalar_one()
        assert session.execute(
            text(
                "SELECT relrowsecurity AND relforcerowsecurity "
                "FROM pg_class WHERE relname = 'work_accesses'"
            )
        ).scalar_one()
        assert not session.execute(
            text(
                "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
            )
        ).scalar_one()
        assert not session.execute(
            text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        ).scalar_one()
        assert not session.execute(
            text(
                "SELECT has_table_privilege(current_user, 'alembic_version', 'SELECT')"
            )
        ).scalar_one()
        assert not session.execute(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).scalar_one()
        assert session.execute(
            text(
                "SELECT NOT EXISTS ("
                "SELECT 1 FROM pg_class "
                "WHERE relkind = 'r' "
                "AND relname IN ('works', 'work_accesses') "
                "AND relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
                ")"
            )
        ).scalar_one()


def test_concurrent_reciprocal_maintainer_revocations_keep_one_maintainer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    first = Account(email=f"br003-first-{suffix}@example.com", status="active")
    second = Account(email=f"br003-second-{suffix}@example.com", status="active")

    with SessionLocal() as session:
        session.add_all((first, second))
        session.commit()
        set_actor(session, first.id)
        workspace = workspaces_service.create_workspace(
            session, first.id, f"互撤维护者-{suffix[:8]}", None
        )
        set_workspace_management_scope(session, workspace.id)
        session.add(WorkspaceMember(workspace_id=workspace.id, account_id=second.id))
        session.commit()
        set_actor(session, first.id)
        work = _create_work(session, first.id, workspace.id, "维护者并发撤销")
        set_actor(session, first.id)
        works_service.set_work_access(
            session, first.id, workspace.id, work.id, second.id, "maintainer"
        )

    start = Barrier(2)
    member_lock = Barrier(2)
    original_lock_work_members = workspaces_service.lock_work_members
    outcomes: dict[UUID, str] = {}
    errors: list[Exception] = []

    def lock_work_members(
        session: Session, workspace_id: UUID, account_ids: set[UUID]
    ) -> frozenset[UUID]:
        member_lock.wait(timeout=5)
        return original_lock_work_members(session, workspace_id, account_ids)

    monkeypatch.setattr(workspaces_service, "lock_work_members", lock_work_members)

    def revoke(actor_id: UUID, target_account_id: UUID) -> None:
        try:
            with SessionLocal() as session:
                set_actor(session, actor_id)
                start.wait(timeout=5)
                works_service.revoke_work_access(
                    session, actor_id, workspace.id, work.id, target_account_id
                )
                outcomes[actor_id] = "revoked"
        except works_service.WorkManagementForbidden:
            outcomes[actor_id] = "rejected"
        except Exception as error:
            errors.append(error)

    first_thread = Thread(target=revoke, args=(first.id, second.id))
    second_thread = Thread(target=revoke, args=(second.id, first.id))
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert sorted(outcomes.values()) == ["rejected", "revoked"]

    with SessionLocal() as session:
        set_actor(session, first.id)
        set_work_management_scope(session, work.id, workspace.id)
        maintainers = list(
            session.scalars(
                select(WorkAccess.account_id).where(
                    WorkAccess.work_id == work.id, WorkAccess.role == "maintainer"
                )
            )
        )
    assert len(maintainers) == 1
    assert maintainers[0] in {first.id, second.id}


def test_concurrent_member_removal_and_work_access_grant_leave_no_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    owner = Account(email=f"br003-owner-{suffix}@example.com", status="active")
    member = Account(email=f"br003-member-{suffix}@example.com", status="active")

    with SessionLocal() as session:
        session.add_all((owner, member))
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"移除成员并发授权-{suffix[:8]}", None
        )
        set_workspace_management_scope(session, workspace.id)
        session.add(WorkspaceMember(workspace_id=workspace.id, account_id=member.id))
        session.commit()
        set_actor(session, owner.id)
        work = _create_work(session, owner.id, workspace.id, "成员并发授权")

    start = Barrier(2)
    race = Barrier(2)
    original_lock_work_members = workspaces_service.lock_work_members
    original_set_work_access_cleanup_scope = (
        workspaces_service.set_work_access_cleanup_scope
    )
    outcomes: dict[str, str] = {}
    errors: list[Exception] = []

    def lock_work_members(
        session: Session, workspace_id: UUID, account_ids: set[UUID]
    ) -> frozenset[UUID]:
        race.wait(timeout=5)
        return original_lock_work_members(session, workspace_id, account_ids)

    def set_work_access_cleanup_scope(session: Session, workspace_id: UUID) -> None:
        original_set_work_access_cleanup_scope(session, workspace_id)
        race.wait(timeout=5)

    monkeypatch.setattr(workspaces_service, "lock_work_members", lock_work_members)
    monkeypatch.setattr(
        workspaces_service,
        "set_work_access_cleanup_scope",
        set_work_access_cleanup_scope,
    )

    def remove() -> None:
        try:
            with SessionLocal() as session:
                set_actor(session, owner.id)
                start.wait(timeout=5)
                workspaces_service.remove_member(
                    session, owner.id, workspace.id, member.id
                )
                outcomes["remove"] = "removed"
        except Exception as error:
            errors.append(error)

    def grant() -> None:
        try:
            with SessionLocal() as session:
                set_actor(session, owner.id)
                start.wait(timeout=5)
                works_service.set_work_access(
                    session, owner.id, workspace.id, work.id, member.id, "collaborator"
                )
                outcomes["grant"] = "granted"
        except works_service.WorkAccessMemberUnavailable:
            outcomes["grant"] = "member_unavailable"
        except Exception as error:
            errors.append(error)

    remove_thread = Thread(target=remove)
    grant_thread = Thread(target=grant)
    remove_thread.start()
    grant_thread.start()
    remove_thread.join(timeout=5)
    grant_thread.join(timeout=5)

    assert not remove_thread.is_alive()
    assert not grant_thread.is_alive()
    assert errors == []
    assert outcomes == {"remove": "removed", "grant": "member_unavailable"}

    with SessionLocal() as session:
        set_actor(session, owner.id)
        set_workspace_management_scope(session, workspace.id)
        assert (
            session.scalar(
                select(WorkspaceMember.id).where(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.account_id == member.id,
                )
            )
            is None
        )
        set_work_management_scope(session, work.id, workspace.id)
        assert (
            session.scalar(
                select(WorkAccess).where(
                    WorkAccess.work_id == work.id, WorkAccess.account_id == member.id
                )
            )
            is None
        )


def test_work_database_constraints_reject_invalid_values_and_duplicate_access() -> None:
    suffix = uuid4().hex
    owner = Account(email=f"br003-constraints-{suffix}@example.com", status="active")

    with SessionLocal() as session:
        session.add(owner)
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"作品约束-{suffix[:8]}", None
        )

        for min_players, max_players, duration in ((0, 2, 60), (3, 2, 60), (2, 4, 0)):
            work_id = uuid4()
            set_actor(session, owner.id)
            set_work_management_scope(session, work_id, workspace.id)
            session.add(
                Work(
                    id=work_id,
                    workspace_id=workspace.id,
                    name="无效作品",
                    description="无效约束验证",
                    creative_stage="原型",
                    target_experience="验证",
                    min_players=min_players,
                    max_players=max_players,
                    estimated_duration_minutes=duration,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        set_actor(session, owner.id)
        work = _create_work(session, owner.id, workspace.id, "唯一访问验证")
        set_actor(session, owner.id)
        set_work_management_scope(session, work.id, workspace.id)
        session.add(
            WorkAccess(
                work_id=work.id,
                workspace_id=workspace.id,
                account_id=owner.id,
                role="maintainer",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
