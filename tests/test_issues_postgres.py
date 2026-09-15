from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import select, text

from app.access.context import set_actor, set_project_baseline_scope
from app.core.config import settings
from app.core.database import SessionLocal
from app.evidence import service as evidence_service
from app.identity.models import Account
from app.issues import service as issues_service
from app.issues.models import Issue, IssueEvidenceLink
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
        assert total == 3
        assert {issue.decision for issue in issues} == {"modify", "observe", "reject"}
        assert {issue.status for issue in issues} == {"open", "closed"}

        issue = issues_service.read_issue(
            session, owner_id, workspace_id, work_id, issue_id
        )
        evidence, evidence_total = issues_service.list_issue_evidence(
            session, owner_id, workspace_id, work_id, issue_id, 1, 20
        )
        assert evidence_total == issue.source_count == 2
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
        assert removed.source_count == 1
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
                removed.revision,
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
                    status="open",
                    expected_revision=1,
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
                "FROM pg_class WHERE relname IN ('issues', 'issue_evidence_links') "
                "ORDER BY relname"
            )
        ).scalars().all() == [True, True]
        assert session.scalar(select(IssueEvidenceLink)) is not None
        session.rollback()
