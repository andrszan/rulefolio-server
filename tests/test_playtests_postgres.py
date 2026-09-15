import smtplib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update

from app.access.context import set_actor, set_workspace_management_scope
from app.core.config import settings
from app.core.database import SessionLocal
from app.files import service as files_service
from app.identity.models import Account
from app.notifications import dispatcher
from app.notifications.models import MailOutbox
from app.notifications.service import enqueue_business_mail, suppress_business_mails
from app.playtests import service as playtests_service
from app.playtests.models import PlaytestSessionParticipant
from app.works import service as works_service
from app.workspaces import service as workspaces_service
from app.workspaces.models import WorkAccess, WorkspaceMember

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test"
    or settings.s3_bucket_name != settings.s3_test_bucket_name,
    reason="需要 TEST_DB_NAME 和 S3_TEST_BUCKET_NAME",
)


@pytest.fixture
def prepared_playtest() -> tuple[object, ...]:
    suffix = uuid4().hex
    owner = Account(email=f"playtest-owner-{suffix}@example.com", status="active")
    organizer = Account(
        email=f"playtest-organizer-{suffix}@example.com", status="active"
    )
    guest = Account(email=f"playtest-guest-{suffix}@example.com", status="active")
    contender = Account(
        email=f"playtest-contender-{suffix}@example.com", status="active"
    )
    rules = Path(__file__).parents[1] / "src/app/baseline-rules.pdf"
    aid = Path(__file__).parents[1] / "src/app/baseline-player-aid.pdf"

    with SessionLocal() as session:
        session.add_all((owner, organizer, guest, contender))
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"试玩-{suffix[:8]}", None
        )
        set_workspace_management_scope(session, workspace.id)
        session.add(WorkspaceMember(workspace_id=workspace.id, account_id=organizer.id))
        session.commit()
        set_actor(session, owner.id)
        work = works_service.create_work(
            session,
            owner.id,
            workspace.id,
            name="试玩快照作品",
            description="验证场次快照和参与权限",
            creative_stage="原型",
            target_experience="协作探索",
            min_players=2,
            max_players=4,
            estimated_duration_minutes=60,
        )
        set_actor(session, owner.id)
        works_service.set_work_access(
            session, owner.id, workspace.id, work.id, organizer.id, "organizer"
        )
        set_actor(session, owner.id)
        with rules.open("rb") as source:
            first = files_service.upload_material(
                session,
                owner.id,
                workspace.id,
                work.id,
                source,
                "首版规则.pdf",
                "application/pdf",
            )
        set_actor(session, owner.id)
        with aid.open("rb") as source:
            second = files_service.upload_material(
                session,
                owner.id,
                workspace.id,
                work.id,
                source,
                "替换材料.pdf",
                "application/pdf",
            )
        current = works_service.update_rule_materials(
            session,
            owner.id,
            workspace.id,
            work.id,
            rule_name="首版规则",
            rule_description=None,
            rule_content="首版快照正文。",
            material_file_ids=[first.id],
            expected_revision=work.revision,
        )
        plan = playtests_service.create_plan(
            session,
            organizer.id,
            workspace.id,
            work.id,
            "观察参与者是否主动交流。",
            "组织者记录关键决策。",
            (
                playtests_service.SessionDraft(
                    scheduled_at=datetime.now(UTC) + timedelta(days=1),
                    location="试玩桌",
                    capacity=1,
                    material_file_ids=(first.id,),
                    participant_emails=(guest.email, contender.email),
                ),
            ),
        )
        session_id = plan.sessions[0].id
        owner_id, organizer_id = owner.id, organizer.id
        guest_id, contender_id = guest.id, contender.id
        workspace_id = workspace.id
        session.rollback()
    return (
        owner_id,
        organizer_id,
        guest_id,
        contender_id,
        workspace_id,
        work,
        first,
        second,
        current,
        session_id,
        rules,
    )


def test_organizer_snapshot_guest_material_confirmation_and_cancellation(
    prepared_playtest: tuple[object, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        owner,
        organizer,
        guest,
        contender,
        workspace,
        work,
        first,
        second,
        current,
        session_id,
        rules,
    ) = prepared_playtest

    with SessionLocal() as session:
        assert (
            session.scalar(
                select(WorkspaceMember.id).where(WorkspaceMember.account_id == guest)
            )
            is None
        )
        assert (
            session.scalar(
                select(WorkAccess.account_id).where(WorkAccess.account_id == guest)
            )
            is None
        )
        view = playtests_service.read_participant_session(session, guest, session_id)
        assert view.rule_content == "首版快照正文。"
        assert [item.id for item in view.materials] == [first.id]
        stream = playtests_service.open_participant_material(
            session, guest, session_id, first.id
        )
        try:
            assert stream.body.read() == rules.read_bytes()
        finally:
            stream.body.close()

        set_actor(session, owner)
        updated = works_service.update_rule_materials(
            session,
            owner,
            workspace,
            work.id,
            rule_name="更新规则",
            rule_description="新的当前材料",
            rule_content="更新后的作品正文。",
            material_file_ids=[second.id],
            expected_revision=current.revision,
        )
        snapshot = playtests_service.read_managed_session(
            session, organizer, workspace, work.id, session_id
        )
        assert snapshot.rule_content == "首版快照正文。"
        assert [item.id for item in snapshot.materials] == [first.id]

        replaced = playtests_service.replace_materials(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            (second.id,),
            snapshot.revision,
        )
        assert replaced.rule_content == "更新后的作品正文。"
        assert [item.id for item in replaced.materials] == [second.id]
        assert updated.revision > current.revision

    barrier = Barrier(2)

    def confirm(account_id: UUID) -> str:
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            try:
                return playtests_service.confirm_participation(
                    session, account_id, session_id
                ).status
            except playtests_service.PlaytestCapacityExceeded:
                return "full"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            account: executor.submit(confirm, account) for account in (guest, contender)
        }
        outcomes = {
            account: future.result(timeout=20) for account, future in futures.items()
        }
    assert sorted(outcomes.values()) == ["confirmed", "full"]
    winner = next(
        account for account, outcome in outcomes.items() if outcome == "confirmed"
    )

    with SessionLocal() as session:
        repeated = playtests_service.confirm_participation(session, winner, session_id)
        assert repeated.status == "confirmed"
        assert repeated.confirmed_count == 1
        set_actor(session, organizer)
        item = playtests_service.read_managed_session(
            session, organizer, workspace, work.id, session_id
        )
        assert item.confirmed_count == 1

        monkeypatch.setattr(dispatcher, "_send", lambda *_: None)
        assert dispatcher.dispatch_one(session) == "accepted"
        arranged = playtests_service.update_arrangement(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            scheduled_at=datetime.now(UTC) + timedelta(days=2),
            location="更新后的试玩桌",
            capacity=1,
            expected_revision=item.revision,
        )
        cancelled = playtests_service.cancel_session(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            arranged.revision,
        )
        assert cancelled.status == "cancelled"
        statuses = list(
            session.scalars(
                select(MailOutbox.status).where(
                    MailOutbox.business_scope == f"playtest-session:{session_id}"
                )
            )
        )
        assert "suppressed" in statuses
        assert "pending" in statuses
        with pytest.raises(playtests_service.PlaytestSessionStateInvalid):
            playtests_service.confirm_participation(session, winner, session_id)
        with pytest.raises(playtests_service.PlaytestUnavailable):
            playtests_service.open_participant_material(
                session, winner, session_id, second.id
            )
        session.rollback()


def test_business_outbox_records_unknown_and_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(email=f"playtest-mail-{uuid4().hex}@example.com", status="active")
    with SessionLocal() as session:
        session.execute(
            update(MailOutbox)
            .where(MailOutbox.status == "pending")
            .values(status="suppressed")
        )
        session.add(account)
        session.commit()
        unknown = enqueue_business_mail(
            session,
            account.id,
            "playtest_invitation",
            "试玩邀请",
            "请登录查看本场试玩安排。",
            business_scope=f"playtest-session:{uuid4()}",
        )
        session.commit()
        monkeypatch.setattr(
            dispatcher, "_send", lambda *_: (_ for _ in ()).throw(OSError)
        )
        assert dispatcher.dispatch_one(session) == "unknown"
        session.refresh(unknown)
        assert unknown.status == "unknown"

        failed = enqueue_business_mail(
            session,
            account.id,
            "playtest_invitation",
            "试玩邀请",
            "请登录查看本场试玩安排。",
            business_scope=f"playtest-session:{uuid4()}",
        )
        session.commit()
        monkeypatch.setattr(
            dispatcher,
            "_send",
            lambda *_: (_ for _ in ()).throw(smtplib.SMTPException),
        )
        assert dispatcher.dispatch_one(session) in {"pending", "failed"}
        session.refresh(failed)
        assert failed.status in {"pending", "failed"}
        session.rollback()


def test_playtest_rls_constraints_and_app_identity(
    prepared_playtest: tuple[object, ...],
) -> None:
    _, organizer, guest, _, workspace, work, first, _, _, session_id, _ = (
        prepared_playtest
    )
    with SessionLocal() as session:
        set_actor(session, guest)
        assert list(session.scalars(select(PlaytestSessionParticipant))) == []
        with pytest.raises(playtests_service.PlaytestUnavailable):
            playtests_service.open_participant_material(
                session, guest, uuid4(), first.id
            )
        set_actor(session, organizer)
        item = playtests_service.read_managed_session(
            session, organizer, workspace, work.id, session_id
        )
        assert item.id == session_id
        assert session.execute(
            text(
                "SELECT relrowsecurity AND relforcerowsecurity "
                "FROM pg_class WHERE relname = 'playtest_sessions'"
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
                "SELECT NOT EXISTS (SELECT 1 FROM pg_class "
                "WHERE relname = 'playtest_sessions' "
                "AND relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user))"
            )
        ).scalar_one()
        session.rollback()


def test_suppression_stops_claimed_business_mail_before_smtp() -> None:
    account = Account(
        email=f"playtest-suppress-{uuid4().hex}@example.com", status="active"
    )
    with SessionLocal() as session:
        session.execute(
            update(MailOutbox)
            .where(MailOutbox.status == "pending")
            .values(status="suppressed")
        )
        session.add(account)
        session.commit()
        outbox = enqueue_business_mail(
            session,
            account.id,
            "playtest_cancelled",
            "试玩场次已取消",
            "请登录查看本场状态。",
            business_scope=f"playtest-session:{uuid4()}",
        )
        session.commit()
        claim = dispatcher._claim_next(session)
        assert claim is not None
        scope = outbox.business_scope
        assert scope is not None
        session.rollback()

    with SessionLocal() as dispatch_session:
        assert dispatcher._load_claim(dispatch_session, claim) is not None
        with SessionLocal() as cancellation_session:
            suppress_business_mails(cancellation_session, scope)
            cancellation_session.commit()
        assert not dispatcher._start_smtp(dispatch_session, claim)
