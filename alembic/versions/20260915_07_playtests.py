"""增加测试计划、场次快照与业务邮件。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260915_07"
down_revision = "20260915_06"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    app_role = _app_role()

    op.drop_constraint("mail_outbox_credential_id_key", "mail_outbox", type_="unique")
    op.alter_column(
        "mail_outbox",
        "credential_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column("mail_outbox", sa.Column("business_scope", sa.String(length=128)))
    op.add_column("mail_outbox", sa.Column("frozen_subject", sa.String(length=200)))
    op.add_column("mail_outbox", sa.Column("frozen_body", sa.String(length=4000)))
    op.create_index(
        "uq_mail_outbox_credential",
        "mail_outbox",
        ["credential_id"],
        unique=True,
        postgresql_where=sa.text("credential_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_mail_outbox_delivery_shape",
        "mail_outbox",
        "(credential_id IS NOT NULL AND business_scope IS NULL "
        "AND frozen_subject IS NULL AND frozen_body IS NULL) OR "
        "(credential_id IS NULL AND btrim(business_scope) <> '' "
        "AND btrim(frozen_subject) <> '' AND btrim(frozen_body) <> '')",
    )

    op.create_table(
        "playtest_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observation_goals", sa.Text(), nullable=False),
        sa.Column("recording_method", sa.Text(), nullable=False),
        sa.Column("creator_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "btrim(observation_goals) <> ''", name="ck_playtest_plan_observation_goals"
        ),
        sa.CheckConstraint(
            "btrim(recording_method) <> ''", name="ck_playtest_plan_recording_method"
        ),
        sa.ForeignKeyConstraint(
            ["creator_account_id"], ["identity_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["work_id", "workspace_id"],
            ["works.id", "works.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", "work_id", name="uq_playtest_plan_scope"
        ),
    )
    op.create_index(
        "ix_playtest_plans_work_created",
        "playtest_plans",
        ["workspace_id", "work_id", sa.text("created_at DESC"), "id"],
    )

    op.create_table(
        "playtest_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.String(length=240), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'scheduled'"),
        ),
        sa.Column(
            "revision", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("observation_goals", sa.Text(), nullable=False),
        sa.Column("recording_method", sa.Text(), nullable=False),
        sa.Column("work_name", sa.String(length=160), nullable=False),
        sa.Column("rule_name", sa.String(length=160), nullable=False),
        sa.Column("rule_description", sa.Text()),
        sa.Column("rule_content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "capacity > 0", name="ck_playtest_session_capacity_positive"
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'started', 'cancelled')",
            name="ck_playtest_session_status",
        ),
        sa.CheckConstraint(
            "revision > 0", name="ck_playtest_session_revision_positive"
        ),
        sa.CheckConstraint(
            "(status = 'started' AND started_at IS NOT NULL) "
            "OR (status IN ('scheduled', 'cancelled') AND started_at IS NULL)",
            name="ck_playtest_session_started_at_state",
        ),
        sa.CheckConstraint(
            "btrim(location) <> ''", name="ck_playtest_session_location"
        ),
        sa.CheckConstraint(
            "btrim(observation_goals) <> ''",
            name="ck_playtest_session_observation_goals",
        ),
        sa.CheckConstraint(
            "btrim(recording_method) <> ''", name="ck_playtest_session_recording_method"
        ),
        sa.CheckConstraint(
            "btrim(work_name) <> ''", name="ck_playtest_session_work_name"
        ),
        sa.CheckConstraint(
            "btrim(rule_name) <> ''", name="ck_playtest_session_rule_name"
        ),
        sa.CheckConstraint(
            "btrim(rule_content) <> ''", name="ck_playtest_session_rule_content"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "workspace_id", "work_id"],
            [
                "playtest_plans.id",
                "playtest_plans.workspace_id",
                "playtest_plans.work_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_id", "workspace_id"],
            ["works.id", "works.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_playtest_sessions_plan_scheduled",
        "playtest_sessions",
        ["plan_id", "scheduled_at", "id"],
    )
    op.create_index(
        "ix_playtest_sessions_work_scheduled",
        "playtest_sessions",
        ["workspace_id", "work_id", "scheduled_at", "id"],
    )

    op.create_table(
        "playtest_session_materials",
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
            "session_id", "file_id", name="uq_playtest_session_material"
        ),
    )

    op.create_table(
        "playtest_session_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'invited'"),
        ),
        sa.Column("latest_outbox_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('invited', 'confirmed')",
            name="ck_playtest_participant_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["latest_outbox_id"], ["mail_outbox.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["playtest_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "account_id", name="uq_playtest_session_participant"
        ),
    )
    op.create_index(
        "ix_playtest_participants_session_status",
        "playtest_session_participants",
        ["session_id", "status", "account_id"],
    )

    op.execute(f"GRANT SELECT, INSERT, DELETE ON TABLE playtest_plans TO {app_role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE playtest_sessions TO {app_role}"
    )
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON TABLE playtest_session_materials "
        f"TO {app_role}"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE playtest_session_participants "
        f"TO {app_role}"
    )

    op.execute(
        """
        CREATE FUNCTION public.playtest_management_workspace_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.playtest_management_workspace_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.playtest_management_work_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.playtest_management_work_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.playtest_session_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.playtest_session_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.playtest_participant_lookup_session_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.playtest_participant_lookup_session_id', true), '')::uuid
        $$
        """
    )
    for function in (
        "playtest_management_workspace_id",
        "playtest_management_work_id",
        "playtest_session_id",
        "playtest_participant_lookup_session_id",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function}() TO {app_role}")

    op.execute(
        """
        CREATE POLICY work_playtest_lock ON works FOR UPDATE
        USING (
            works.id = public.playtest_management_work_id()
            AND EXISTS (
                SELECT 1 FROM work_accesses access
                WHERE access.work_id = works.id
                  AND access.account_id = public.workspace_actor_id()
                  AND access.role IN ('maintainer', 'organizer')
            )
        )
        WITH CHECK (false)
        """
    )

    for table in (
        "playtest_plans",
        "playtest_sessions",
        "playtest_session_materials",
        "playtest_session_participants",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY playtest_plan_read ON playtest_plans FOR SELECT USING (
            workspace_id = public.playtest_management_workspace_id()
            AND work_id = public.playtest_management_work_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_plan_insert ON playtest_plans FOR INSERT WITH CHECK (
            workspace_id = public.playtest_management_workspace_id()
            AND work_id = public.playtest_management_work_id()
            AND creator_account_id = public.workspace_actor_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_plan_delete ON playtest_plans FOR DELETE USING (
            workspace_id = public.playtest_management_workspace_id()
            AND work_id = public.playtest_management_work_id()
        )
        """
    )

    op.execute(
        """
        CREATE POLICY playtest_session_read ON playtest_sessions FOR SELECT USING (
            (workspace_id = public.playtest_management_workspace_id()
             AND work_id = public.playtest_management_work_id())
            OR id = public.playtest_session_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_session_insert ON playtest_sessions FOR INSERT WITH CHECK (
            workspace_id = public.playtest_management_workspace_id()
            AND work_id = public.playtest_management_work_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_session_update ON playtest_sessions FOR UPDATE
        USING (
            workspace_id = public.playtest_management_workspace_id()
            AND work_id = public.playtest_management_work_id()
        )
        WITH CHECK (
            workspace_id = public.playtest_management_workspace_id()
            AND work_id = public.playtest_management_work_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_session_confirmation_lock ON playtest_sessions FOR UPDATE
        USING (id = public.playtest_session_id())
        WITH CHECK (false)
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_session_delete ON playtest_sessions FOR DELETE USING (
            workspace_id = public.playtest_management_workspace_id()
            AND work_id = public.playtest_management_work_id()
        )
        """
    )

    op.execute(
        """
        CREATE POLICY playtest_material_read ON playtest_session_materials FOR SELECT USING (
            session_id = public.playtest_session_id()
        )
        """
    )
    for action in ("INSERT", "DELETE"):
        command = "WITH CHECK" if action == "INSERT" else "USING"
        op.execute(
            f"""
            CREATE POLICY playtest_material_{action.lower()}
            ON playtest_session_materials FOR {action} {command} (
                EXISTS (
                    SELECT 1 FROM playtest_sessions session
                    WHERE session.id = playtest_session_materials.session_id
                      AND session.workspace_id = public.playtest_management_workspace_id()
                      AND session.work_id = public.playtest_management_work_id()
                )
            )
            """
        )

    op.execute(
        """
        CREATE POLICY playtest_participant_self_read
        ON playtest_session_participants FOR SELECT USING (
            account_id = public.workspace_actor_id()
            AND session_id = public.playtest_participant_lookup_session_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_participant_session_read
        ON playtest_session_participants FOR SELECT USING (
            session_id = public.playtest_session_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_participant_insert
        ON playtest_session_participants FOR INSERT WITH CHECK (
            EXISTS (
                SELECT 1 FROM playtest_sessions session
                WHERE session.id = playtest_session_participants.session_id
                  AND session.workspace_id = public.playtest_management_workspace_id()
                  AND session.work_id = public.playtest_management_work_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_participant_update
        ON playtest_session_participants FOR UPDATE
        USING (
            (session_id = public.playtest_session_id()
             AND account_id = public.workspace_actor_id())
            OR EXISTS (
                SELECT 1 FROM playtest_sessions session
                WHERE session.id = playtest_session_participants.session_id
                  AND session.workspace_id = public.playtest_management_workspace_id()
                  AND session.work_id = public.playtest_management_work_id()
            )
        )
        WITH CHECK (
            (session_id = public.playtest_session_id()
             AND account_id = public.workspace_actor_id())
            OR EXISTS (
                SELECT 1 FROM playtest_sessions session
                WHERE session.id = playtest_session_participants.session_id
                  AND session.workspace_id = public.playtest_management_workspace_id()
                  AND session.work_id = public.playtest_management_work_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY playtest_participant_delete
        ON playtest_session_participants FOR DELETE USING (
            EXISTS (
                SELECT 1 FROM playtest_sessions session
                WHERE session.id = playtest_session_participants.session_id
                  AND session.workspace_id = public.playtest_management_workspace_id()
                  AND session.work_id = public.playtest_management_work_id()
            )
        )
        """
    )

    for policy, table in (
        ("project_baseline_playtest_plan_read", "playtest_plans"),
        ("project_baseline_playtest_plan_delete", "playtest_plans"),
        ("project_baseline_playtest_session_read", "playtest_sessions"),
        ("project_baseline_playtest_session_delete", "playtest_sessions"),
        ("project_baseline_playtest_material_read", "playtest_session_materials"),
        ("project_baseline_playtest_material_delete", "playtest_session_materials"),
        ("project_baseline_playtest_participant_read", "playtest_session_participants"),
        (
            "project_baseline_playtest_participant_delete",
            "playtest_session_participants",
        ),
    ):
        command = "SELECT" if policy.endswith("_read") else "DELETE"
        op.execute(
            f"CREATE POLICY {policy} ON {table} FOR {command} "
            "USING (public.project_baseline_maintenance())"
        )


def downgrade() -> None:
    for policy, table in (
        (
            "project_baseline_playtest_participant_delete",
            "playtest_session_participants",
        ),
        ("project_baseline_playtest_participant_read", "playtest_session_participants"),
        ("project_baseline_playtest_material_delete", "playtest_session_materials"),
        ("project_baseline_playtest_material_read", "playtest_session_materials"),
        ("project_baseline_playtest_session_delete", "playtest_sessions"),
        ("project_baseline_playtest_session_read", "playtest_sessions"),
        ("project_baseline_playtest_plan_delete", "playtest_plans"),
        ("project_baseline_playtest_plan_read", "playtest_plans"),
        ("playtest_participant_delete", "playtest_session_participants"),
        ("playtest_participant_update", "playtest_session_participants"),
        ("playtest_participant_insert", "playtest_session_participants"),
        ("playtest_participant_session_read", "playtest_session_participants"),
        ("playtest_participant_self_read", "playtest_session_participants"),
        ("playtest_material_delete", "playtest_session_materials"),
        ("playtest_material_insert", "playtest_session_materials"),
        ("playtest_material_read", "playtest_session_materials"),
        ("playtest_session_delete", "playtest_sessions"),
        ("playtest_session_confirmation_lock", "playtest_sessions"),
        ("playtest_session_update", "playtest_sessions"),
        ("playtest_session_insert", "playtest_sessions"),
        ("playtest_session_read", "playtest_sessions"),
        ("playtest_plan_delete", "playtest_plans"),
        ("playtest_plan_insert", "playtest_plans"),
        ("playtest_plan_read", "playtest_plans"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.drop_index(
        "ix_playtest_participants_session_status",
        table_name="playtest_session_participants",
    )
    op.drop_table("playtest_session_participants")
    op.drop_table("playtest_session_materials")
    op.drop_index("ix_playtest_sessions_work_scheduled", table_name="playtest_sessions")
    op.drop_index("ix_playtest_sessions_plan_scheduled", table_name="playtest_sessions")
    op.drop_table("playtest_sessions")
    op.drop_index("ix_playtest_plans_work_created", table_name="playtest_plans")
    op.drop_table("playtest_plans")
    op.execute("DROP POLICY IF EXISTS work_playtest_lock ON works")
    for function in (
        "playtest_participant_lookup_session_id",
        "playtest_session_id",
        "playtest_management_work_id",
        "playtest_management_workspace_id",
    ):
        op.execute(f"DROP FUNCTION public.{function}()")
    op.drop_constraint("ck_mail_outbox_delivery_shape", "mail_outbox", type_="check")
    op.drop_index("uq_mail_outbox_credential", table_name="mail_outbox")
    op.drop_column("mail_outbox", "frozen_body")
    op.drop_column("mail_outbox", "frozen_subject")
    op.drop_column("mail_outbox", "business_scope")
    op.execute("DELETE FROM mail_outbox WHERE credential_id IS NULL")
    op.alter_column(
        "mail_outbox",
        "credential_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_unique_constraint(
        "mail_outbox_credential_id_key", "mail_outbox", ["credential_id"]
    )
