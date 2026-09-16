import smtplib
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update

from app.access.context import (
    set_actor,
    set_work_management_scope,
    set_workspace_management_scope,
)
from app.core.config import settings
from app.core.database import SessionLocal
from app.evidence import service as evidence_service
from app.evidence.models import PlaytestObservation
from app.files import service as files_service
from app.identity.models import Account
from app.issues import service as issues_service
from app.issues.models import IssueEvidenceLink
from app.notifications import dispatcher
from app.notifications.models import MailOutbox
from app.notifications.service import enqueue_business_mail, suppress_business_mails
from app.playtests import service as playtests_service
from app.playtests.models import (
    PlaytestSessionActualMaterial,
    PlaytestSessionActualParticipant,
    PlaytestSessionParticipant,
)
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
    collaborator = Account(
        email=f"playtest-collaborator-{suffix}@example.com", status="active"
    )
    rules = Path(__file__).parents[1] / "src/app/baseline-rules.pdf"
    aid = Path(__file__).parents[1] / "src/app/baseline-player-aid.pdf"

    with SessionLocal() as session:
        session.add_all((owner, organizer, guest, contender, collaborator))
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"试玩-{suffix[:8]}", None
        )
        set_workspace_management_scope(session, workspace.id)
        session.add_all(
            (
                WorkspaceMember(workspace_id=workspace.id, account_id=organizer.id),
                WorkspaceMember(workspace_id=workspace.id, account_id=collaborator.id),
            )
        )
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
        works_service.set_work_access(
            session, owner.id, workspace.id, work.id, collaborator.id, "collaborator"
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
        guest_id, contender_id, collaborator_id = (
            guest.id,
            contender.id,
            collaborator.id,
        )
        workspace_id = workspace.id
        session.rollback()
    return (
        owner_id,
        organizer_id,
        guest_id,
        contender_id,
        collaborator_id,
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
        _,
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


def test_result_actual_participation_materials_and_observations(
    prepared_playtest: tuple[object, ...],
) -> None:
    (
        _,
        organizer,
        guest,
        _,
        collaborator,
        workspace,
        work,
        first,
        second,
        _,
        session_id,
        _,
    ) = prepared_playtest

    with SessionLocal() as session:
        arranged = playtests_service.read_managed_session(
            session, organizer, workspace, work.id, session_id
        )
        started = playtests_service.start_session(
            session, organizer, workspace, work.id, session_id, arranged.revision
        )
        saved = playtests_service.save_result(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            started.revision,
            playtests_service.ResultDraft(
                actual_headcount=2,
                actual_duration_minutes=0,
                completion_status="completed",
                actual_play_mode="  实体桌游  ",
                actual_material=playtests_service.ActualMaterialDraft(
                    rule_name=started.rule_name,
                    rule_description=started.rule_description,
                    rule_content=started.rule_content,
                    material_file_ids=(second.id,),
                    change_reason="现场改用辅助页说明规则。",
                ),
                actual_participants=(
                    playtests_service.ActualParticipantDraft(
                        planned_account_id=guest,
                        temporary_code=None,
                        seat_or_faction="先手",
                        score_or_outcome="获胜",
                    ),
                    playtests_service.ActualParticipantDraft(
                        planned_account_id=None,
                        temporary_code="临场观察者",
                        seat_or_faction=None,
                        score_or_outcome=None,
                    ),
                ),
            ),
        )
        assert saved.session.revision > started.revision
        fact = playtests_service.create_observation(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            saved.session.revision,
            "fact",
            "玩家在第一轮主动解释了路径选择。",
        )
        corrected = playtests_service.update_observation(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            fact.observation.id,
            fact.revision,
            "organizer_interpretation",
            "提示语已经能帮助玩家快速分工。",
        )
        result = playtests_service.read_result(
            session, organizer, workspace, work.id, session_id
        )
        assert result.session.status == "started"
        assert result.session.revision == corrected.revision
        assert result.actual_headcount == 2
        assert result.actual_duration_minutes == 0
        assert result.completion_status == "completed"
        assert result.actual_play_mode == "实体桌游"
        assert result.actual_material is not None
        assert result.actual_material.change_reason == "现场改用辅助页说明规则。"
        assert [material.id for material in result.actual_material.materials] == [
            second.id
        ]
        assert {material.id for material in result.material_candidates} == {
            first.id,
            second.id,
        }
        assert {item.planned_account_id for item in result.actual_participants} == {
            guest,
            None,
        }
        assert result.observations[0].id == fact.observation.id
        assert result.observations[0].kind == "organizer_interpretation"

        cleared = playtests_service.save_result(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            result.session.revision,
            playtests_service.ResultDraft(
                actual_headcount=2,
                actual_duration_minutes=0,
                completion_status="completed",
                actual_play_mode="   ",
                actual_material=playtests_service.ActualMaterialDraft(
                    rule_name=started.rule_name,
                    rule_description=started.rule_description,
                    rule_content=started.rule_content,
                    material_file_ids=(second.id,),
                    change_reason="现场改用辅助页说明规则。",
                ),
                actual_participants=(
                    playtests_service.ActualParticipantDraft(
                        planned_account_id=guest,
                        temporary_code=None,
                        seat_or_faction="先手",
                        score_or_outcome="获胜",
                    ),
                    playtests_service.ActualParticipantDraft(
                        planned_account_id=None,
                        temporary_code="临场观察者",
                        seat_or_faction=None,
                        score_or_outcome=None,
                    ),
                ),
            ),
        )
        assert cleared.actual_play_mode is None
        with pytest.raises(playtests_service.PlaytestResultInvalid):
            playtests_service.save_result(
                session,
                organizer,
                workspace,
                work.id,
                session_id,
                cleared.session.revision,
                playtests_service.ResultDraft(
                    actual_headcount=2,
                    actual_duration_minutes=0,
                    completion_status="completed",
                    actual_play_mode="x" * 161,
                    actual_material=None,
                    actual_participants=(),
                ),
            )

        session.rollback()
        set_actor(session, guest)
        assert list(session.scalars(select(PlaytestSessionActualMaterial))) == []
        assert list(session.scalars(select(PlaytestSessionActualParticipant))) == []
        assert list(session.scalars(select(PlaytestObservation))) == []
        with pytest.raises(playtests_service.PlaytestUnavailable):
            playtests_service.read_result(
                session, guest, workspace, work.id, session_id
            )
        with pytest.raises(playtests_service.PlaytestUnavailable):
            playtests_service.create_observation(
                session,
                guest,
                workspace,
                work.id,
                session_id,
                corrected.revision,
                "fact",
                "无权写入的观察。",
            )

        set_actor(session, collaborator)
        assert list(session.scalars(select(PlaytestSessionActualMaterial))) == []
        assert list(session.scalars(select(PlaytestSessionActualParticipant))) == []
        assert list(session.scalars(select(PlaytestObservation))) == []
        with pytest.raises(playtests_service.PlaytestManagementForbidden):
            playtests_service.read_result(
                session, collaborator, workspace, work.id, session_id
            )
        session.rollback()
        with pytest.raises(playtests_service.PlaytestManagementForbidden):
            playtests_service.create_observation(
                session,
                collaborator,
                workspace,
                work.id,
                session_id,
                corrected.revision,
                "fact",
                "无权写入的观察。",
            )
        session.rollback()

        with pytest.raises(playtests_service.PlaytestSessionRevisionConflict):
            playtests_service.save_result(
                session,
                organizer,
                workspace,
                work.id,
                session_id,
                saved.session.revision,
                playtests_service.ResultDraft(
                    actual_headcount=None,
                    actual_duration_minutes=None,
                    completion_status=None,
                    actual_material=None,
                    actual_participants=(),
                ),
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
    _, organizer, guest, _, _, workspace, work, first, _, _, session_id, _ = (
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


def test_feedback_keeps_drafts_private_locks_items_and_updates_current_answers(
    prepared_playtest: tuple[object, ...],
) -> None:
    (
        owner,
        organizer,
        guest,
        contender,
        _,
        workspace,
        work,
        _,
        _,
        _,
        session_id,
        _,
    ) = prepared_playtest

    with SessionLocal() as session:
        short_text = playtests_service.create_feedback_item(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            "feedback-short-text",
            evidence_service.FeedbackItemDraft(
                kind="short_text", question="哪条规则还需要说明？", options=()
            ),
        )
        choice = playtests_service.create_feedback_item(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            "feedback-choice",
            evidence_service.FeedbackItemDraft(
                kind="single_choice",
                question="协作节奏如何？",
                options=("过慢", "合适", "过快"),
            ),
        )
        started = playtests_service.start_session(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            playtests_service.read_managed_session(
                session, organizer, workspace, work.id, session_id
            ).revision,
        )
        draft = playtests_service.save_participant_feedback(
            session,
            guest,
            session_id,
            "feedback-direct-create",
            None,
            evidence_service.FeedbackSubmissionDraft(
                source="direct",
                temporary_alias=None,
                status="draft",
                answers=(
                    evidence_service.FeedbackAnswerDraft(
                        item_id=short_text.id, text_value="终局结算希望有示例。"
                    ),
                ),
            ),
        )
        retry = playtests_service.save_participant_feedback(
            session,
            guest,
            session_id,
            "feedback-direct-create",
            None,
            evidence_service.FeedbackSubmissionDraft(
                source="direct",
                temporary_alias=None,
                status="draft",
                answers=(
                    evidence_service.FeedbackAnswerDraft(
                        item_id=short_text.id, text_value="终局结算希望有示例。"
                    ),
                ),
            ),
        )
        assert retry.id == draft.id
        with pytest.raises(evidence_service.FeedbackItemLocked):
            playtests_service.update_feedback_item(
                session,
                organizer,
                workspace,
                work.id,
                session_id,
                short_text.id,
                short_text.revision,
                evidence_service.FeedbackItemDraft(
                    kind="short_text", question="已改变的问题", options=()
                ),
            )
        session.rollback()

        manager_view = playtests_service.read_feedback(
            session, organizer, workspace, work.id, session_id
        )
        assert manager_view.feedback.submissions == ()
        participant_view = playtests_service.read_participant_session(
            session, guest, session_id
        )
        assert participant_view.own_feedback is not None
        assert participant_view.own_feedback.status == "draft"
        other_participant = playtests_service.read_participant_session(
            session, contender, session_id
        )
        assert other_participant.own_feedback is None

        submitted = playtests_service.save_participant_feedback(
            session,
            guest,
            session_id,
            None,
            draft.revision,
            evidence_service.FeedbackSubmissionDraft(
                source="direct",
                temporary_alias=None,
                status="submitted",
                answers=(
                    evidence_service.FeedbackAnswerDraft(
                        item_id=choice.id, option_id=choice.options[1].id
                    ),
                ),
            ),
        )
        assert submitted.id == draft.id
        assert submitted.revision == draft.revision + 1
        assert submitted.answers[0].item_id == choice.id
        with pytest.raises(evidence_service.FeedbackSubmissionRevisionConflict):
            playtests_service.save_participant_feedback(
                session,
                guest,
                session_id,
                None,
                draft.revision,
                evidence_service.FeedbackSubmissionDraft(
                    source="direct",
                    temporary_alias=None,
                    status="submitted",
                    answers=(
                        evidence_service.FeedbackAnswerDraft(
                            item_id=choice.id, option_id=choice.options[0].id
                        ),
                    ),
                ),
            )
        session.rollback()

        manager_view = playtests_service.read_feedback(
            session, organizer, workspace, work.id, session_id
        )
        assert [submission.id for submission in manager_view.feedback.submissions] == [
            draft.id
        ]
        organizer_submission = playtests_service.create_feedback_submission(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            "feedback-organizer-create",
            evidence_service.FeedbackSubmissionDraft(
                source="temporary_alias",
                temporary_alias="现场观察者 A",
                status="submitted",
                answers=(
                    evidence_service.FeedbackAnswerDraft(
                        item_id=short_text.id, text_value="口头复盘提到结算例子不足。"
                    ),
                ),
            ),
        )
        with pytest.raises(evidence_service.FeedbackSubmissionForbidden):
            playtests_service.update_feedback_submission(
                session,
                owner,
                workspace,
                work.id,
                session_id,
                organizer_submission.id,
                organizer_submission.revision,
                evidence_service.FeedbackSubmissionDraft(
                    source="oral_discussion",
                    temporary_alias=None,
                    status="submitted",
                    answers=(
                        evidence_service.FeedbackAnswerDraft(
                            item_id=short_text.id, text_value="无权更正。"
                        ),
                    ),
                ),
            )
        session.rollback()
        assert started.status == "started"


def test_issue_linked_direct_feedback_cannot_return_to_draft(
    prepared_playtest: tuple[object, ...],
) -> None:
    (
        owner,
        organizer,
        guest,
        _,
        _,
        workspace,
        work,
        first,
        _,
        _,
        session_id,
        _,
    ) = prepared_playtest

    with SessionLocal() as session:
        item = playtests_service.create_feedback_item(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            "linked-feedback-item",
            evidence_service.FeedbackItemDraft(
                kind="short_text", question="路线提示是否清晰？", options=()
            ),
        )
        started = playtests_service.start_session(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            playtests_service.read_managed_session(
                session, organizer, workspace, work.id, session_id
            ).revision,
        )
        submitted = playtests_service.save_participant_feedback(
            session,
            guest,
            session_id,
            "linked-feedback-create",
            None,
            evidence_service.FeedbackSubmissionDraft(
                source="direct",
                temporary_alias=None,
                status="submitted",
                answers=(
                    evidence_service.FeedbackAnswerDraft(
                        item_id=item.id, text_value="路线提示需要更多示例。"
                    ),
                ),
            ),
        )
        issue = issues_service.create_issue(
            session,
            owner,
            workspace,
            work.id,
            "linked-feedback-issue",
            description="路线提示的说明仍需调整。",
            decision="modify",
            reason="已提交反馈指出理解障碍。",
            status="open",
            references=(
                evidence_service.IssueEvidenceReference(
                    source_type="feedback_submission", source_id=submitted.id
                ),
            ),
        )

        with pytest.raises(evidence_service.FeedbackSubmissionLinked):
            playtests_service.save_participant_feedback(
                session,
                guest,
                session_id,
                None,
                submitted.revision,
                evidence_service.FeedbackSubmissionDraft(
                    source="direct",
                    temporary_alias=None,
                    status="draft",
                    answers=(
                        evidence_service.FeedbackAnswerDraft(
                            item_id=item.id, text_value="路线提示需要更多示例。"
                        ),
                    ),
                ),
            )

        current = playtests_service.read_participant_session(session, guest, session_id)
        assert current.own_feedback is not None
        assert current.own_feedback.status == "submitted"
        evidence, total = issues_service.list_issue_evidence(
            session, owner, workspace, work.id, issue.id, 1, 20
        )
        assert started.status == "started"
        assert total == 1
        assert evidence[0].source.source_id == submitted.id


def test_feedback_link_and_draft_race_preserves_submitted(
    prepared_playtest: tuple[object, ...],
) -> None:
    (
        owner,
        organizer,
        guest,
        _,
        _,
        workspace,
        work,
        _,
        _,
        _,
        session_id,
        _,
    ) = prepared_playtest

    with SessionLocal() as session:
        item = playtests_service.create_feedback_item(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            "linked-feedback-race-item",
            evidence_service.FeedbackItemDraft(
                kind="short_text", question="路线提示是否清晰？", options=()
            ),
        )
        started = playtests_service.start_session(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            playtests_service.read_managed_session(
                session, organizer, workspace, work.id, session_id
            ).revision,
        )
        observation = playtests_service.create_observation(
            session,
            organizer,
            workspace,
            work.id,
            session_id,
            started.revision,
            "fact",
            "参与者在路线选择时停顿并请求说明。",
        )
        submitted = playtests_service.save_participant_feedback(
            session,
            guest,
            session_id,
            "linked-feedback-race-create",
            None,
            evidence_service.FeedbackSubmissionDraft(
                source="direct",
                temporary_alias=None,
                status="submitted",
                answers=(
                    evidence_service.FeedbackAnswerDraft(
                        item_id=item.id, text_value="路线提示需要更多示例。"
                    ),
                ),
            ),
        )
        issue = issues_service.create_issue(
            session,
            owner,
            workspace,
            work.id,
            "linked-feedback-race-issue",
            description="路线提示的说明仍需调整。",
            decision="modify",
            reason="现场观察指出理解障碍。",
            status="open",
            references=(
                evidence_service.IssueEvidenceReference(
                    source_type="observation", source_id=observation.observation.id
                ),
            ),
        )

    link_flushed = Event()
    allow_link_commit = Event()
    draft_started = Event()

    def link_feedback() -> str:
        with SessionLocal() as session:
            set_actor(session, owner)
            set_work_management_scope(session, work.id, workspace)
            session.add(
                IssueEvidenceLink(
                    issue_id=issue.id, feedback_submission_id=submitted.id
                )
            )
            session.flush()
            link_flushed.set()
            assert allow_link_commit.wait(timeout=5)
            session.commit()
        return "linked"

    def draft_feedback() -> str:
        assert link_flushed.wait(timeout=5)
        with SessionLocal() as session:
            draft_started.set()
            try:
                playtests_service.save_participant_feedback(
                    session,
                    guest,
                    session_id,
                    None,
                    submitted.revision,
                    evidence_service.FeedbackSubmissionDraft(
                        source="direct",
                        temporary_alias=None,
                        status="draft",
                        answers=(
                            evidence_service.FeedbackAnswerDraft(
                                item_id=item.id, text_value="路线提示需要更多示例。"
                            ),
                        ),
                    ),
                )
            except evidence_service.FeedbackSubmissionLinked:
                return "linked"
        return "drafted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        link = executor.submit(link_feedback)
        assert link_flushed.wait(timeout=5)
        draft = executor.submit(draft_feedback)
        assert draft_started.wait(timeout=5)
        try:
            with pytest.raises(FutureTimeoutError):
                draft.result(timeout=1)
        finally:
            allow_link_commit.set()
        assert link.result(timeout=5) == "linked"
        assert draft.result(timeout=5) == "linked"

    with SessionLocal() as session:
        current = playtests_service.read_participant_session(session, guest, session_id)
        assert current.own_feedback is not None
        assert current.own_feedback.status == "submitted"
        evidence, total = issues_service.list_issue_evidence(
            session, owner, workspace, work.id, issue.id, 1, 20
        )
        assert total == 2
        assert {item.source.source_id for item in evidence} == {
            observation.observation.id,
            submitted.id,
        }
