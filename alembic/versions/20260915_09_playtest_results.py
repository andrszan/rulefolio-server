"""增加场次结果、实际参与与现场观察。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260915_09"
down_revision = "20260915_08"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def _create_file_read_policy() -> None:
    op.execute(
        """
        CREATE POLICY file_read ON files FOR SELECT USING (
            id = public.file_lifecycle_id()
            OR (
                kind = 'image'
                AND status = 'ready'
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                )
            )
            OR (
                kind = 'material'
                AND status = 'ready'
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                      AND access.role = 'maintainer'
                )
            )
            OR (
                kind = 'material'
                AND status = 'ready'
                AND work_id = public.playtest_result_material_work_id()
            )
            OR (
                kind = 'image'
                AND status = 'pending'
                AND work_id = public.file_recovery_work_id()
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                )
            )
            OR (
                kind = 'material'
                AND status = 'pending'
                AND work_id = public.file_recovery_work_id()
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                      AND access.role = 'maintainer'
                )
            )
        )
        """
    )


def _create_previous_file_read_policy() -> None:
    op.execute(
        """
        CREATE POLICY file_read ON files FOR SELECT USING (
            id = public.file_lifecycle_id()
            OR (
                kind = 'image'
                AND status = 'ready'
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                )
            )
            OR (
                kind = 'material'
                AND status = 'ready'
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                      AND access.role = 'maintainer'
                )
            )
            OR (
                kind = 'image'
                AND status = 'pending'
                AND work_id = public.file_recovery_work_id()
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                )
            )
            OR (
                kind = 'material'
                AND status = 'pending'
                AND work_id = public.file_recovery_work_id()
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                      AND access.role = 'maintainer'
                )
            )
        )
        """
    )


def upgrade() -> None:
    app_role = _app_role()
    op.add_column("playtest_sessions", sa.Column("actual_headcount", sa.Integer()))
    op.add_column(
        "playtest_sessions",
        sa.Column(
            "actual_duration_minutes",
            sa.Integer(),
        ),
    )
    op.add_column("playtest_sessions", sa.Column("completion_status", sa.String(16)))
    op.add_column(
        "playtest_sessions",
        sa.Column(
            "actual_material_recorded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("playtest_sessions", sa.Column("actual_rule_name", sa.String(160)))
    op.add_column("playtest_sessions", sa.Column("actual_rule_description", sa.Text()))
    op.add_column("playtest_sessions", sa.Column("actual_rule_content", sa.Text()))
    op.add_column(
        "playtest_sessions", sa.Column("actual_material_change_reason", sa.Text())
    )
    op.alter_column(
        "playtest_sessions", "actual_material_recorded", server_default=None
    )
    op.create_check_constraint(
        "ck_playtest_session_actual_headcount_nonnegative",
        "playtest_sessions",
        "actual_headcount IS NULL OR actual_headcount >= 0",
    )
    op.create_check_constraint(
        "ck_playtest_session_actual_duration_nonnegative",
        "playtest_sessions",
        "actual_duration_minutes IS NULL OR actual_duration_minutes >= 0",
    )
    op.create_check_constraint(
        "ck_playtest_session_completion_status",
        "playtest_sessions",
        "completion_status IS NULL OR completion_status IN ('completed', 'interrupted')",
    )
    op.create_check_constraint(
        "ck_playtest_session_actual_material_shape",
        "playtest_sessions",
        "(NOT actual_material_recorded "
        "AND actual_rule_name IS NULL "
        "AND actual_rule_description IS NULL "
        "AND actual_rule_content IS NULL "
        "AND actual_material_change_reason IS NULL) "
        "OR (actual_material_recorded "
        "AND btrim(actual_rule_name) <> '' "
        "AND btrim(actual_rule_content) <> '')",
    )

    op.create_table(
        "playtest_session_actual_materials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.LargeBinary(length=32), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["playtest_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "file_id", name="uq_playtest_session_actual_material"
        ),
    )
    op.create_table(
        "playtest_session_actual_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planned_account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("temporary_code", sa.String(length=160)),
        sa.Column("seat_or_faction", sa.String(length=160)),
        sa.Column("score_or_outcome", sa.String(length=160)),
        sa.CheckConstraint(
            "(planned_account_id IS NOT NULL AND temporary_code IS NULL) "
            "OR (planned_account_id IS NULL AND btrim(temporary_code) <> '')",
            name="ck_playtest_actual_participant_identity",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["playtest_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "planned_account_id"],
            [
                "playtest_session_participants.session_id",
                "playtest_session_participants.account_id",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "planned_account_id",
            name="uq_playtest_actual_participant_account",
        ),
        sa.UniqueConstraint(
            "session_id",
            "temporary_code",
            name="uq_playtest_actual_participant_temporary_code",
        ),
    )
    op.create_index(
        "ix_playtest_actual_participants_session",
        "playtest_session_actual_participants",
        ["session_id", "id"],
    )
    op.create_table(
        "playtest_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "recorded_by_account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('fact', 'organizer_interpretation', 'temporary_variant')",
            name="ck_playtest_observation_kind",
        ),
        sa.CheckConstraint(
            "btrim(content) <> ''", name="ck_playtest_observation_content"
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_account_id"], ["identity_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["playtest_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_playtest_observations_session_kind_recorded",
        "playtest_observations",
        ["session_id", "kind", "recorded_at", "id"],
    )

    op.execute(
        "GRANT SELECT, INSERT, DELETE ON TABLE playtest_session_actual_materials "
        f"TO {app_role}"
    )
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON TABLE playtest_session_actual_participants "
        f"TO {app_role}"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE playtest_observations "
        f"TO {app_role}"
    )

    for table in (
        "playtest_session_actual_materials",
        "playtest_session_actual_participants",
        "playtest_observations",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    for table, prefix in (
        ("playtest_session_actual_materials", "playtest_actual_material"),
        ("playtest_session_actual_participants", "playtest_actual_participant"),
        ("playtest_observations", "playtest_observation"),
    ):
        op.execute(
            f"CREATE POLICY {prefix}_read ON {table} FOR SELECT "
            "USING (session_id = public.playtest_session_id())"
        )
        op.execute(
            f"CREATE POLICY {prefix}_insert ON {table} FOR INSERT "
            "WITH CHECK (session_id = public.playtest_session_id())"
        )
        op.execute(
            f"CREATE POLICY {prefix}_delete ON {table} FOR DELETE "
            "USING (session_id = public.playtest_session_id())"
        )

    op.execute(
        "CREATE POLICY playtest_observation_update ON playtest_observations FOR UPDATE "
        "USING (session_id = public.playtest_session_id()) "
        "WITH CHECK (session_id = public.playtest_session_id())"
    )

    for policy, table in (
        (
            "project_baseline_playtest_actual_material_read",
            "playtest_session_actual_materials",
        ),
        (
            "project_baseline_playtest_actual_material_delete",
            "playtest_session_actual_materials",
        ),
        (
            "project_baseline_playtest_actual_participant_read",
            "playtest_session_actual_participants",
        ),
        (
            "project_baseline_playtest_actual_participant_delete",
            "playtest_session_actual_participants",
        ),
        ("project_baseline_playtest_observation_read", "playtest_observations"),
        ("project_baseline_playtest_observation_delete", "playtest_observations"),
    ):
        command = "SELECT" if policy.endswith("_read") else "DELETE"
        op.execute(
            f"CREATE POLICY {policy} ON {table} FOR {command} "
            "USING (public.project_baseline_maintenance())"
        )

    op.execute(
        """
        CREATE FUNCTION public.playtest_result_material_work_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(
                current_setting('app.playtest_result_material_work_id', true), ''
            )::uuid
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.playtest_result_material_work_id() "
        f"TO {app_role}"
    )
    op.execute("DROP POLICY file_read ON files")
    _create_file_read_policy()


def downgrade() -> None:
    op.execute("DROP POLICY file_read ON files")
    _create_previous_file_read_policy()
    op.execute("DROP FUNCTION public.playtest_result_material_work_id()")

    for policy, table in (
        ("project_baseline_playtest_observation_delete", "playtest_observations"),
        ("project_baseline_playtest_observation_read", "playtest_observations"),
        (
            "project_baseline_playtest_actual_participant_delete",
            "playtest_session_actual_participants",
        ),
        (
            "project_baseline_playtest_actual_participant_read",
            "playtest_session_actual_participants",
        ),
        (
            "project_baseline_playtest_actual_material_delete",
            "playtest_session_actual_materials",
        ),
        (
            "project_baseline_playtest_actual_material_read",
            "playtest_session_actual_materials",
        ),
        ("playtest_observation_update", "playtest_observations"),
        ("playtest_observation_delete", "playtest_observations"),
        ("playtest_observation_insert", "playtest_observations"),
        ("playtest_observation_read", "playtest_observations"),
        ("playtest_actual_participant_delete", "playtest_session_actual_participants"),
        ("playtest_actual_participant_insert", "playtest_session_actual_participants"),
        ("playtest_actual_participant_read", "playtest_session_actual_participants"),
        ("playtest_actual_material_delete", "playtest_session_actual_materials"),
        ("playtest_actual_material_insert", "playtest_session_actual_materials"),
        ("playtest_actual_material_read", "playtest_session_actual_materials"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.drop_index(
        "ix_playtest_observations_session_kind_recorded",
        table_name="playtest_observations",
    )
    op.drop_table("playtest_observations")
    op.drop_index(
        "ix_playtest_actual_participants_session",
        table_name="playtest_session_actual_participants",
    )
    op.drop_table("playtest_session_actual_participants")
    op.drop_table("playtest_session_actual_materials")
    op.drop_constraint(
        "ck_playtest_session_actual_material_shape",
        "playtest_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_playtest_session_completion_status", "playtest_sessions", type_="check"
    )
    op.drop_constraint(
        "ck_playtest_session_actual_duration_nonnegative",
        "playtest_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_playtest_session_actual_headcount_nonnegative",
        "playtest_sessions",
        type_="check",
    )
    op.drop_column("playtest_sessions", "actual_material_change_reason")
    op.drop_column("playtest_sessions", "actual_rule_content")
    op.drop_column("playtest_sessions", "actual_rule_description")
    op.drop_column("playtest_sessions", "actual_rule_name")
    op.drop_column("playtest_sessions", "actual_material_recorded")
    op.drop_column("playtest_sessions", "completion_status")
    op.drop_column("playtest_sessions", "actual_duration_minutes")
    op.drop_column("playtest_sessions", "actual_headcount")
