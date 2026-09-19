"""增加恢复状态、受控恢复 scope 与核验权限。"""

import sqlalchemy as sa

from alembic import op
from app.core.config import settings

revision = "20260920_31"
down_revision = "20260919_30"
branch_labels = None
depends_on = None

RECOVERY_READ_TABLES = (
    "files",
    "issue_evidence_links",
    "issue_retest_links",
    "issues",
    "notification_todos",
    "playtest_feedback_answers",
    "playtest_feedback_items",
    "playtest_feedback_options",
    "playtest_feedback_submissions",
    "playtest_observations",
    "playtest_plans",
    "playtest_session_actual_materials",
    "playtest_session_actual_participants",
    "playtest_session_materials",
    "playtest_session_participants",
    "playtest_sessions",
    "work_accesses",
    "work_material_files",
    "works",
    "workspace_invitation_attempts",
    "workspace_invitations",
    "workspace_members",
    "workspaces",
)


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    app_role = _app_role()
    op.create_table(
        "recovery_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("recovery_id", sa.String(length=128)),
        sa.Column("manifest_sha256", sa.LargeBinary(length=32)),
        sa.Column("recovery_target_at", sa.DateTime(timezone=True)),
        sa.Column("authorization_mode", sa.String(length=16)),
        sa.Column("restricted_access_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("operator", sa.String(length=128)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_recovery_state_singleton"),
        sa.CheckConstraint(
            "stage IN ('open', 'recovering', 'api_open')",
            name="ck_recovery_state_stage",
        ),
        sa.CheckConstraint(
            "authorization_mode IS NULL OR authorization_mode IN ('fresh', 'restricted')",
            name="ck_recovery_state_authorization_mode",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO recovery_state (id, stage) VALUES (1, 'open')")
    op.execute("ALTER TABLE recovery_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE recovery_state FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE FUNCTION public.recovery_maintenance() RETURNS boolean "
        "LANGUAGE sql STABLE AS $$ "
        "SELECT current_setting('app.recovery_maintenance', true) = 'active' $$"
    )
    op.execute(
        "CREATE FUNCTION public.recovery_schema_current() RETURNS boolean "
        "LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $$ "
        f"SELECT EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num = '{revision}') $$"
    )
    op.execute(f"GRANT SELECT, UPDATE ON TABLE recovery_state TO {app_role}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.recovery_maintenance() TO {app_role}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.recovery_schema_current() TO {app_role}"
    )
    op.execute(
        "CREATE POLICY recovery_state_read ON recovery_state FOR SELECT USING (true)"
    )
    op.execute(
        "CREATE POLICY recovery_state_update ON recovery_state FOR UPDATE "
        "USING (public.recovery_maintenance()) "
        "WITH CHECK (public.recovery_maintenance())"
    )
    for table in RECOVERY_READ_TABLES:
        op.execute(
            f"CREATE POLICY {table}_recovery_read ON {table} FOR SELECT "
            "USING (public.recovery_maintenance())"
        )


def downgrade() -> None:
    for table in RECOVERY_READ_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_recovery_read ON {table}")
    op.execute("DROP POLICY IF EXISTS recovery_state_update ON recovery_state")
    op.execute("DROP POLICY IF EXISTS recovery_state_read ON recovery_state")
    app_role = _app_role()
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.recovery_schema_current() FROM {app_role}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.recovery_maintenance() FROM {app_role}"
    )
    op.execute("DROP FUNCTION public.recovery_schema_current()")
    op.execute("DROP FUNCTION public.recovery_maintenance()")
    op.drop_table("recovery_state")
