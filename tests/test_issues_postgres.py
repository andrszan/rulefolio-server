from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from alembic import command
from app.access.context import (
    set_actor,
    set_project_baseline_scope,
    set_work_management_scope,
)
from app.core.config import settings
from app.core.database import SessionLocal
from app.evidence import service as evidence_service
from app.files.models import StoredFile
from app.identity.models import Account
from app.issues import service as issues_service
from app.issues.models import Issue, IssueEvidenceLink, IssueRetestLink
from app.playtests import service as playtests_service
from app.project_baseline import reset, reset_confirmation
from app.works import service as works_service
from app.works.models import Work
from app.workspaces.models import Workspace

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test"
    or settings.s3_bucket_name != settings.s3_test_bucket_name,
    reason="需要 TEST_DB_NAME 和 S3_TEST_BUCKET_NAME",
)


def _reset() -> None:
    with SessionLocal() as session:
        assert (
            reset(session, "pytest", "BR-010 集成验证", reset_confirmation()).status
            == "reset"
        )


def _baseline_ids() -> tuple[UUID, UUID, UUID, UUID, UUID, UUID]:
    with SessionLocal() as session:
        set_project_baseline_scope(session)
        owner = session.scalar(
            select(Account).where(Account.email == "studio-owner@example.com")
        )
        organizer = session.scalar(
            select(Account).where(Account.email == "session-organizer@example.com")
        )
        workspace = session.scalar(select(Workspace))
        work = session.scalar(select(Work))
        modify_issue = session.scalar(select(Issue).where(Issue.decision == "modify"))
        assert all((owner, organizer, workspace, work, modify_issue))
        links = list(
            session.scalars(
                select(IssueEvidenceLink).where(
                    IssueEvidenceLink.issue_id == modify_issue.id
                )
            )
        )
        observation_id = next(
            link.observation_id for link in links if link.observation_id
        )
        feedback_id = next(
            link.feedback_submission_id for link in links if link.feedback_submission_id
        )
        assert observation_id is not None and feedback_id is not None
        result = (
            owner.id,
            organizer.id,
            workspace.id,
            work.id,
            modify_issue.id,
            observation_id,
        )
        session.rollback()
    return result


def test_issues_keep_current_sources_enforce_relations_and_hide_cross_work() -> None:
    _reset()
    owner_id, organizer_id, workspace_id, work_id, issue_id, observation_id = (
        _baseline_ids()
    )

    with SessionLocal() as session:
        issues, total = issues_service.list_issues(
            session, owner_id, workspace_id, work_id, 1, 20
        )
        assert total == 4
        assert {issue.decision for issue in issues} == {"modify", "observe", "reject"}
        assert {issue.status for issue in issues} == {"open", "closed"}

        issue = issues_service.read_issue(
            session, owner_id, workspace_id, work_id, issue_id
        )
        evidence, evidence_total = issues_service.list_issue_evidence(
            session, owner_id, workspace_id, work_id, issue_id, 1, 20
        )
        assert evidence_total == issue.source_count == 3
        assert {item.source.source_type for item in evidence} == {
            "observation",
            "feedback_submission",
        }
        feedback = next(
            item.source
            for item in evidence
            if item.source.source_type == "feedback_submission"
        )
        assert feedback.location == "工作室试玩桌 A"
        assert any(
            answer.question == "哪一段规则最需要进一步说明？"
            and answer.text_value is not None
            for answer in feedback.answers
        )

        updated = issues_service.update_issue(
            session,
            owner_id,
            workspace_id,
            work_id,
            issue_id,
            description="终局结算的触发顺序仍需要示例。",
            decision="modify",
            reason="当前来源仍指向理解障碍。",
            adjustment_note=None,
            status="closed",
            expected_revision=issue.revision,
        )
        assert updated.revision == issue.revision + 1
        assert updated.status == "closed"
        with pytest.raises(issues_service.IssueRevisionConflict):
            issues_service.update_issue(
                session,
                owner_id,
                workspace_id,
                work_id,
                issue_id,
                description="过期更新。",
                decision="reject",
                reason="不应覆盖。",
                adjustment_note=None,
                status="open",
                expected_revision=issue.revision,
            )

    with SessionLocal() as session:
        with pytest.raises(issues_service.IssueManagementForbidden):
            issues_service.list_issues(
                session, organizer_id, workspace_id, work_id, 1, 20
            )

        set_actor(session, owner_id)
        other_work = works_service.create_work(
            session,
            owner_id,
            workspace_id,
            name="无关来源作品",
            description="验证跨作品来源不可用。",
            creative_stage="原型",
            target_experience="隔离来源",
            min_players=2,
            max_players=4,
            estimated_duration_minutes=60,
        )
        with pytest.raises(issues_service.IssueUnavailable):
            issues_service.create_issue(
                session,
                owner_id,
                workspace_id,
                other_work.id,
                "cross-work-source",
                description="不应建立的问题。",
                decision="observe",
                reason="来源不属于当前作品。",
                status="open",
                references=(
                    evidence_service.IssueEvidenceReference(
                        source_type="observation", source_id=observation_id
                    ),
                ),
            )

    with SessionLocal() as session:
        issue = issues_service.read_issue(
            session, owner_id, workspace_id, work_id, issue_id
        )
        evidence, _ = issues_service.list_issue_evidence(
            session, owner_id, workspace_id, work_id, issue_id, 1, 20
        )
        observation_link = next(
            item for item in evidence if item.source.source_type == "observation"
        )
        removed = issues_service.remove_issue_evidence(
            session,
            owner_id,
            workspace_id,
            work_id,
            issue_id,
            observation_link.link_id,
            issue.revision,
        )
        assert removed.source_count == 2
        next_link = issues_service.list_issue_evidence(
            session, owner_id, workspace_id, work_id, issue_id, 1, 20
        )[0][0]
        one_remaining = issues_service.remove_issue_evidence(
            session,
            owner_id,
            workspace_id,
            work_id,
            issue_id,
            next_link.link_id,
            removed.revision,
        )
        assert one_remaining.source_count == 1
        only_link = issues_service.list_issue_evidence(
            session, owner_id, workspace_id, work_id, issue_id, 1, 20
        )[0][0]
        with pytest.raises(issues_service.IssueLastEvidenceRequired):
            issues_service.remove_issue_evidence(
                session,
                owner_id,
                workspace_id,
                work_id,
                issue_id,
                only_link.link_id,
                one_remaining.revision,
            )


def test_issue_creation_is_idempotent_and_revision_writes_lock_the_issue() -> None:
    _reset()
    owner_id, _, workspace_id, work_id, issue_id, observation_id = _baseline_ids()
    reference = evidence_service.IssueEvidenceReference(
        source_type="observation", source_id=observation_id
    )

    with SessionLocal() as session:
        created = issues_service.create_issue(
            session,
            owner_id,
            workspace_id,
            work_id,
            "issue-idempotency",
            description="重复来源作为独立判断保留。",
            decision="observe",
            reason="维护者需要继续观察该记录。",
            status="open",
            references=(reference,),
        )
        retried = issues_service.create_issue(
            session,
            owner_id,
            workspace_id,
            work_id,
            "issue-idempotency",
            description="重复来源作为独立判断保留。",
            decision="observe",
            reason="维护者需要继续观察该记录。",
            status="open",
            references=(reference,),
        )
        assert retried.id == created.id
        with pytest.raises(issues_service.IssueOperationConflict):
            issues_service.create_issue(
                session,
                owner_id,
                workspace_id,
                work_id,
                "issue-idempotency",
                description="另一创建意图。",
                decision="reject",
                reason="同一 key 不应覆盖。",
                status="open",
                references=(reference,),
            )

    with SessionLocal() as session:
        current = issues_service.read_issue(
            session, owner_id, workspace_id, work_id, issue_id
        )

    start = Barrier(2)

    def update(description: str) -> str:
        with SessionLocal() as session:
            start.wait(timeout=10)
            try:
                issues_service.update_issue(
                    session,
                    owner_id,
                    workspace_id,
                    work_id,
                    issue_id,
                    description=description,
                    decision="modify",
                    reason="并发更新验证。",
                    adjustment_note=None,
                    status="open",
                    expected_revision=current.revision,
                )
                return "updated"
            except issues_service.IssueRevisionConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            executor.submit(update, description)
            for description in ("并发判断 A。", "并发判断 B。")
        ]
        assert sorted(future.result(timeout=20) for future in outcomes) == [
            "conflict",
            "updated",
        ]

    with SessionLocal() as session:
        set_project_baseline_scope(session)
        assert session.execute(
            text(
                "SELECT relrowsecurity AND relforcerowsecurity "
                "FROM pg_class WHERE relname IN ('issues', 'issue_evidence_links', "
                "'issue_retest_links') "
                "ORDER BY relname"
            )
        ).scalars().all() == [True, True, True]
        assert session.scalar(select(IssueEvidenceLink)) is not None
        assert session.scalar(select(IssueRetestLink)) is not None
        session.rollback()


def test_issue_retest_links_require_work_management_scope() -> None:
    _reset()
    owner_id, _, workspace_id, work_id, _, _ = _baseline_ids()

    with SessionLocal() as session:
        set_actor(session, owner_id)
        assert session.scalar(select(IssueRetestLink)) is None
        session.rollback()

    with SessionLocal() as session:
        set_actor(session, owner_id)
        set_work_management_scope(session, work_id, workspace_id)
        assert session.scalar(select(IssueRetestLink)) is not None
        session.rollback()


def test_adjustment_retest_and_current_conclusion_follow_explicit_evidence() -> None:
    _reset()
    owner_id, _, workspace_id, work_id, issue_id, _ = _baseline_ids()

    with SessionLocal() as session:
        issue = issues_service.read_issue(
            session, owner_id, workspace_id, work_id, issue_id
        )
        assert issue.verification_status == "verified"
        assert issue.current_conclusion is not None
        updated = issues_service.update_issue(
            session,
            owner_id,
            workspace_id,
            work_id,
            issue.id,
            description=issue.description,
            decision=issue.decision,
            reason=issue.reason,
            adjustment_note="补充了另一种终局结算示例。",
            status="open",
            expected_revision=issue.revision,
        )
        assert updated.verification_status == "pending"
        assert updated.current_conclusion is None
        retests, total = issues_service.list_retests(
            session, owner_id, workspace_id, work_id, issue.id, 1, 20
        )
        assert total == 1
        assert retests[0].conclusion is None
        assert not retests[0].current_adjustment
        with pytest.raises(issues_service.IssueInvalid):
            issues_service.save_retest_conclusion(
                session,
                owner_id,
                workspace_id,
                work_id,
                issue.id,
                retests[0].id,
                conclusion="insufficient_evidence",
                reason="此前调整的复测不能用于当前结论。",
                status="open",
                expected_revision=updated.revision,
            )

        set_project_baseline_scope(session)
        material_ids = tuple(
            session.scalars(
                select(StoredFile.id)
                .where(StoredFile.kind == "material")
                .order_by(StoredFile.id)
            )
        )
        retest_plan = playtests_service.create_plan(
            session,
            owner_id,
            workspace_id,
            work_id,
            "验证新增终局示例是否足够清晰。",
            "记录玩家完成结算时的提问。",
            (
                playtests_service.SessionDraft(
                    scheduled_at=datetime.now(UTC) + timedelta(days=7),
                    location="复测桌 C",
                    capacity=2,
                    material_file_ids=material_ids,
                    participant_emails=(),
                ),
            ),
            retest_issue_id=updated.id,
            retest_issue_expected_revision=updated.revision,
        )
        started = playtests_service.start_session(
            session,
            owner_id,
            workspace_id,
            work_id,
            retest_plan.sessions[0].id,
            retest_plan.sessions[0].revision,
        )
        playtests_service.save_result(
            session,
            owner_id,
            workspace_id,
            work_id,
            started.id,
            started.revision,
            playtests_service.ResultDraft(
                actual_headcount=0,
                actual_duration_minutes=20,
                completion_status="interrupted",
                actual_material=playtests_service.ActualMaterialDraft(
                    rule_name=started.rule_name,
                    rule_description=started.rule_description,
                    rule_content=started.rule_content,
                    material_file_ids=tuple(
                        material.id for material in started.materials
                    ),
                    change_reason=None,
                ),
                actual_participants=(),
            ),
        )
        current = issues_service.read_issue(
            session, owner_id, workspace_id, work_id, issue.id
        )
        retests, _ = issues_service.list_retests(
            session, owner_id, workspace_id, work_id, issue.id, 1, 20
        )
        target = next(retest for retest in retests if retest.plan_id == retest_plan.id)
        assert target.current_adjustment
        with pytest.raises(issues_service.IssueRetestEvidenceRequired):
            issues_service.save_retest_conclusion(
                session,
                owner_id,
                workspace_id,
                work_id,
                issue.id,
                target.id,
                conclusion="verified",
                reason="缺少本场来源时不应保存。",
                status="closed",
                expected_revision=current.revision,
            )

        current = issues_service.read_issue(
            session, owner_id, workspace_id, work_id, issue.id
        )
        retests, _ = issues_service.list_retests(
            session, owner_id, workspace_id, work_id, issue.id, 1, 20
        )
        target = next(retest for retest in retests if retest.plan_id == retest_plan.id)
        concluded = issues_service.save_retest_conclusion(
            session,
            owner_id,
            workspace_id,
            work_id,
            issue.id,
            target.id,
            conclusion="insufficient_evidence",
            reason="本场尚未关联观察或已提交反馈。",
            status="open",
            expected_revision=current.revision,
        )
        assert concluded.status == "open"
        assert concluded.verification_status == "insufficient_evidence"
        assert concluded.current_conclusion is not None
        assert concluded.current_conclusion.retest_id == target.id


def test_v18_retest_is_not_reused_for_a_later_adjustment_after_upgrade() -> None:
    _reset()
    owner_id, _, workspace_id, work_id, _, _ = _baseline_ids()
    with SessionLocal() as session:
        set_project_baseline_scope(session)
        issue_id = session.scalar(select(IssueRetestLink.issue_id))
        assert issue_id is not None
        session.rollback()
    config = Config("alembic.ini")
    engine = create_engine(settings.migrator_database_url)

    command.downgrade(config, "20260916_18")
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE issues NO FORCE ROW LEVEL SECURITY"))
            connection.execute(
                text("ALTER TABLE issue_retest_links NO FORCE ROW LEVEL SECURITY")
            )
            try:
                assert (
                    connection.scalar(
                        text(
                            "SELECT conclusion FROM issue_retest_links WHERE issue_id = :issue_id"
                        ),
                        {"issue_id": issue_id},
                    )
                    == "verified"
                )
                adjustment = connection.execute(
                    text(
                        "UPDATE issues SET adjustment_note = :adjustment_note WHERE id = :issue_id"
                    ),
                    {
                        "adjustment_note": "补充了新的终局结算示例。",
                        "issue_id": issue_id,
                    },
                )
                conclusion = connection.execute(
                    text(
                        "UPDATE issue_retest_links SET conclusion = NULL, conclusion_reason = NULL "
                        "WHERE issue_id = :issue_id"
                    ),
                    {"issue_id": issue_id},
                )
                assert adjustment.rowcount == 1
                assert conclusion.rowcount == 1
                assert (
                    connection.scalar(
                        text("SELECT adjustment_note FROM issues WHERE id = :issue_id"),
                        {"issue_id": issue_id},
                    )
                    == "补充了新的终局结算示例。"
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT conclusion FROM issue_retest_links WHERE issue_id = :issue_id"
                        ),
                        {"issue_id": issue_id},
                    )
                    is None
                )
            finally:
                connection.execute(
                    text("ALTER TABLE issue_retest_links FORCE ROW LEVEL SECURITY")
                )
                connection.execute(text("ALTER TABLE issues FORCE ROW LEVEL SECURITY"))

        command.upgrade(config, "head")

        with SessionLocal() as session:
            issue = issues_service.read_issue(
                session, owner_id, workspace_id, work_id, issue_id
            )
            assert issue.verification_status == "pending"
            assert issue.current_conclusion is None
            retests, total = issues_service.list_retests(
                session, owner_id, workspace_id, work_id, issue_id, 1, 20
            )
            assert total == 1
            assert not retests[0].current_adjustment
            assert retests[0].conclusion is None
            with pytest.raises(issues_service.IssueInvalid):
                issues_service.save_retest_conclusion(
                    session,
                    owner_id,
                    workspace_id,
                    work_id,
                    issue_id,
                    retests[0].id,
                    conclusion="insufficient_evidence",
                    reason="无法证明此前复测属于当前调整。",
                    status="open",
                    expected_revision=issue.revision,
                )
    finally:
        engine.dispose()
        command.upgrade(config, "head")
        _reset()
