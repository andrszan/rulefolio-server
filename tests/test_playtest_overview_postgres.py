from dataclasses import fields
from uuid import UUID

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from alembic import command
from app.access.context import set_actor, set_project_baseline_scope
from app.core.config import settings
from app.core.database import SessionLocal
from app.identity import service as identity_service
from app.identity.models import Account
from app.issues import service as issues_service
from app.main import app
from app.playtests import service as playtests_service
from app.playtests.models import PlaytestSession
from app.project_baseline import reset, reset_confirmation
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
            reset(session, "pytest", "BR-012 集成验证", reset_confirmation()).status
            == "reset"
        )


def _baseline_ids() -> tuple[UUID, UUID, UUID, UUID, UUID]:
    with SessionLocal() as session:
        set_project_baseline_scope(session)
        owner = session.scalar(
            select(Account).where(Account.email == "studio-owner@example.com")
        )
        collaborator = session.scalar(
            select(Account).where(Account.email == "rules-collaborator@example.com")
        )
        guest = session.scalar(
            select(Account).where(Account.email == "playtest-guest@example.com")
        )
        workspace = session.scalar(select(Workspace))
        work = session.scalar(select(Work))
        assert all((owner, collaborator, guest, workspace, work))
        result = owner.id, collaborator.id, guest.id, workspace.id, work.id
        session.rollback()
    return result


@pytest.mark.skipif(
    settings.migrator_db_name != settings.db_name,
    reason="迁移验证需要 MIGRATOR_DB_NAME 指向 TEST_DB_NAME",
)
def test_v22_upgrade_keeps_legacy_play_modes_unrecorded() -> None:
    _reset()
    owner_id, _, _, workspace_id, work_id = _baseline_ids()
    config = Config("alembic.ini")

    command.downgrade(config, "20260916_21")
    try:
        command.upgrade(config, "head")
        with SessionLocal() as session:
            set_project_baseline_scope(session)
            sessions = list(session.scalars(select(PlaytestSession)))
            assert len(sessions) == 2
            assert all(item.actual_play_mode is None for item in sessions)
            session.rollback()

            overview = playtests_service.read_overview(
                session,
                owner_id,
                workspace_id,
                work_id,
                playtests_service.OverviewFilters(),
            )
            assert overview.missing_actual_play_mode_count == 2
    finally:
        command.upgrade(config, "head")


def test_overview_filters_aggregates_pages_issues_and_rls() -> None:
    _reset()
    owner_id, collaborator_id, guest_id, workspace_id, work_id = _baseline_ids()
    with SessionLocal() as session:
        token = identity_service.login(
            session,
            "studio-owner@example.com",
            settings.baseline_password.get_secret_value(),
        ).token
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/works/{work_id}/playtest-overview",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json()["data"]["playModes"] == ["实体桌游", "规则补充复测"]

    with SessionLocal() as session:
        overview = playtests_service.read_overview(
            session,
            owner_id,
            workspace_id,
            work_id,
            playtests_service.OverviewFilters(),
        )
        assert overview.included_session_count == 2
        assert overview.missing_actual_headcount_count == 0
        assert overview.missing_actual_duration_count == 0
        assert overview.missing_completion_status_count == 0
        assert overview.missing_actual_play_mode_count == 0
        assert overview.temporary_variant_count == 1
        assert overview.play_modes == ("实体桌游", "规则补充复测")
        assert overview.headcount_coverage == (
            playtests_service.HeadcountCoverageData(1, 2, 0, 0, 1),
        )

        sessions, total = playtests_service.list_overview_sessions(
            session,
            owner_id,
            workspace_id,
            work_id,
            playtests_service.OverviewFilters(),
            1,
            1,
        )
        assert total == 2
        assert len(sessions) == 1
        second_page, second_total = playtests_service.list_overview_sessions(
            session,
            owner_id,
            workspace_id,
            work_id,
            playtests_service.OverviewFilters(),
            2,
            1,
        )
        assert second_total == total
        assert len(second_page) == 1
        assert sessions[0].id != second_page[0].id
        assert sessions[0].scheduled_at > second_page[0].scheduled_at
        assert {item.has_temporary_variant for item in sessions + second_page} == {
            False,
            True,
        }

        date_filtered = playtests_service.read_overview(
            session,
            owner_id,
            workspace_id,
            work_id,
            playtests_service.OverviewFilters(
                scheduled_from=second_page[0].scheduled_at,
                scheduled_before=sessions[0].scheduled_at,
            ),
        )
        assert date_filtered.included_session_count == 1
        filtered = playtests_service.read_overview(
            session,
            owner_id,
            workspace_id,
            work_id,
            playtests_service.OverviewFilters(
                actual_headcount_min=0,
                actual_headcount_max=1,
                actual_play_mode="实体桌游",
            ),
        )
        assert filtered.included_session_count == 1
        assert filtered.filters.actual_headcount_min == 0
        assert filtered.filters.actual_play_mode == "实体桌游"

        retest_result = playtests_service.read_result(
            session, owner_id, workspace_id, work_id, sessions[0].id
        )
        assert retest_result.actual_material is not None
        playtests_service.save_result(
            session,
            owner_id,
            workspace_id,
            work_id,
            sessions[0].id,
            retest_result.session.revision,
            playtests_service.ResultDraft(
                actual_headcount=0,
                actual_duration_minutes=None,
                completion_status="interrupted",
                actual_play_mode=None,
                actual_material=playtests_service.ActualMaterialDraft(
                    rule_name=retest_result.actual_material.rule_name,
                    rule_description=retest_result.actual_material.rule_description,
                    rule_content=retest_result.actual_material.rule_content,
                    material_file_ids=tuple(
                        material.id
                        for material in retest_result.actual_material.materials
                    ),
                    change_reason=retest_result.actual_material.change_reason,
                ),
                actual_participants=(),
            ),
        )
        changed = playtests_service.read_overview(
            session,
            owner_id,
            workspace_id,
            work_id,
            playtests_service.OverviewFilters(),
        )
        assert changed.missing_actual_duration_count == 1
        assert changed.missing_actual_play_mode_count == 1
        assert changed.headcount_coverage == (
            playtests_service.HeadcountCoverageData(0, 0, 1, 0, 0),
            playtests_service.HeadcountCoverageData(1, 1, 0, 0, 1),
        )

        pending_retest, pending_total = playtests_service.list_overview_issues(
            session,
            owner_id,
            workspace_id,
            work_id,
            "pending-retest",
            1,
            20,
        )
        needs_action, action_total = playtests_service.list_overview_issues(
            session,
            owner_id,
            workspace_id,
            work_id,
            "needs-action",
            1,
            20,
        )
        assert pending_total == action_total == 1
        assert {item.id for item in pending_retest}.isdisjoint(
            item.id for item in needs_action
        )
        assert {field.name for field in fields(pending_retest[0])} == {
            "id",
            "description",
            "decision",
            "status",
            "verification_status",
            "current_conclusion_type",
            "current_conclusion_session_id",
        }
        assert pending_retest[0].verification_status == "pending"
        assert needs_action[0].verification_status == "not_recorded"
        issue_id = pending_retest[0].id

    with SessionLocal() as session:
        collaborator_overview = playtests_service.read_overview(
            session,
            collaborator_id,
            workspace_id,
            work_id,
            playtests_service.OverviewFilters(),
        )
        assert collaborator_overview.included_session_count == 2
        with pytest.raises(playtests_service.PlaytestManagementForbidden):
            playtests_service.read_result(
                session, collaborator_id, workspace_id, work_id, sessions[0].id
            )
        session.rollback()
        with pytest.raises(issues_service.IssueManagementForbidden):
            issues_service.read_issue(
                session,
                collaborator_id,
                workspace_id,
                work_id,
                issue_id,
            )
        session.rollback()

        set_actor(session, collaborator_id)
        assert list(session.scalars(select(PlaytestSession.id))) == []
        assert list(session.scalars(select(PlaytestSession.actual_rule_content))) == []
        assert (
            list(
                session.execute(
                    text("SELECT actual_material_change_reason FROM playtest_sessions")
                )
            )
            == []
        )
        assert (
            list(session.execute(text("SELECT content FROM playtest_observations")))
            == []
        )
        assert (
            list(session.execute(text("SELECT reason, adjustment_note FROM issues")))
            == []
        )
        assert (
            list(
                session.execute(
                    text("SELECT conclusion_reason FROM issue_retest_links")
                )
            )
            == []
        )
        session.rollback()

    with (
        SessionLocal() as session,
        pytest.raises(playtests_service.PlaytestOverviewUnavailable),
    ):
        playtests_service.read_overview(
            session,
            guest_id,
            workspace_id,
            work_id,
            playtests_service.OverviewFilters(),
        )
