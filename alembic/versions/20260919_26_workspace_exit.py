"""增加工作空间退出状态和访问门禁。"""

import sqlalchemy as sa

from alembic import op
from app.core.config import settings

revision = "20260919_26"
down_revision = "20260916_25"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def _exit_read(workspace_id: str) -> str:
    return f"""
        EXISTS (
            SELECT 1 FROM workspaces workspace
            WHERE workspace.id = {workspace_id}
              AND (
                  workspace.exit_read_until IS NULL
                  OR workspace.exit_read_until > CURRENT_TIMESTAMP
                  OR (
                      workspace.id = public.workspace_exit_maintenance_id()
                      AND public.workspace_exit_processor() = 'active'
                  )
              )
        )
    """


def _exit_write(workspace_id: str) -> str:
    return f"""
        EXISTS (
            SELECT 1 FROM workspaces workspace
            WHERE workspace.id = {workspace_id}
              AND (
                  workspace.exit_requested_at IS NULL
                  OR (
                      workspace.id = public.workspace_exit_maintenance_id()
                      AND public.workspace_exit_processor() = 'active'
                  )
              )
        )
    """


def _add_exit_policies(table: str, workspace_id: str) -> None:
    read = _exit_read(workspace_id)
    write = _exit_write(workspace_id)
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_read ON {table} AS RESTRICTIVE "
        f"FOR SELECT USING ({read})"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_insert ON {table} AS RESTRICTIVE "
        f"FOR INSERT WITH CHECK ({write})"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_update ON {table} AS RESTRICTIVE "
        f"FOR UPDATE USING ({write}) WITH CHECK ({write})"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_delete ON {table} AS RESTRICTIVE "
        f"FOR DELETE USING ({write})"
    )


def _drop_exit_policies(table: str) -> None:
    for action in ("delete", "update", "insert", "read"):
        op.execute(f"DROP POLICY IF EXISTS {table}_workspace_exit_{action} ON {table}")


def upgrade() -> None:
    app_role = _app_role()
    op.add_column(
        "workspaces",
        sa.Column(
            "revision", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.add_column(
        "workspaces", sa.Column("exit_requested_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "workspaces", sa.Column("exit_read_until", sa.DateTime(timezone=True))
    )
    op.add_column(
        "workspaces",
        sa.Column("exit_requested_by_account_id", sa.Uuid(), nullable=True),
    )
    op.add_column("workspaces", sa.Column("exit_operation_key", sa.String(length=128)))
    op.add_column(
        "workspaces", sa.Column("exit_completed_at", sa.DateTime(timezone=True))
    )
    op.create_foreign_key(
        "fk_workspaces_exit_requested_by_account",
        "workspaces",
        "identity_accounts",
        ["exit_requested_by_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_workspace_revision_positive", "workspaces", "revision > 0"
    )
    op.create_check_constraint(
        "ck_workspace_exit_request_shape",
        "workspaces",
        "(exit_requested_at IS NULL AND exit_read_until IS NULL "
        "AND exit_requested_by_account_id IS NULL AND exit_operation_key IS NULL) OR "
        "(exit_requested_at IS NOT NULL AND exit_read_until IS NOT NULL "
        "AND exit_requested_by_account_id IS NOT NULL "
        "AND btrim(exit_operation_key) <> '')",
    )
    op.create_check_constraint(
        "ck_workspace_exit_completion_requires_request",
        "workspaces",
        "exit_completed_at IS NULL OR exit_requested_at IS NOT NULL",
    )
    op.create_index(
        "ix_workspaces_exit_due",
        "workspaces",
        ["exit_read_until", "id"],
        postgresql_where=sa.text(
            "exit_requested_at IS NOT NULL AND exit_completed_at IS NULL"
        ),
    )

    op.execute(
        "CREATE FUNCTION public.workspace_exit_maintenance_id() RETURNS uuid "
        "LANGUAGE sql STABLE AS $$ "
        "SELECT NULLIF(current_setting('app.workspace_exit_maintenance_id', true), '')::uuid "
        "$$"
    )
    op.execute(
        "CREATE FUNCTION public.workspace_exit_processor() RETURNS text "
        "LANGUAGE sql STABLE AS $$ "
        "SELECT NULLIF(current_setting('app.workspace_exit_processor', true), '') "
        "$$"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.workspace_exit_maintenance_id() TO {app_role}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.workspace_exit_processor() TO {app_role}"
    )

    op.execute(
        "CREATE POLICY workspace_exit_read ON workspaces AS RESTRICTIVE FOR SELECT USING ("
        "exit_read_until IS NULL OR exit_read_until > CURRENT_TIMESTAMP OR "
        "public.workspace_exit_processor() = 'active')"
    )
    op.execute(
        "CREATE POLICY workspace_exit_processor_read ON workspaces FOR SELECT USING ("
        "public.workspace_exit_processor() = 'active' AND exit_requested_at IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY workspace_exit_request_update ON workspaces FOR UPDATE USING ("
        "owner_account_id = public.workspace_actor_id() AND exit_requested_at IS NULL) "
        "WITH CHECK (owner_account_id = public.workspace_actor_id())"
    )
    op.execute(
        "CREATE POLICY workspace_exit_processor_update ON workspaces FOR UPDATE USING ("
        "id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active') WITH CHECK ("
        "id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active')"
    )

    _add_exit_policies("workspace_members", "workspace_members.workspace_id")
    _add_exit_policies("workspace_invitations", "workspace_invitations.workspace_id")
    _add_exit_policies("work_accesses", "work_accesses.workspace_id")
    _add_exit_policies("works", "works.workspace_id")
    _add_exit_policies("files", "files.workspace_id")
    _add_exit_policies("playtest_plans", "playtest_plans.workspace_id")
    _add_exit_policies("playtest_sessions", "playtest_sessions.workspace_id")
    _add_exit_policies("issues", "issues.workspace_id")
    _add_exit_policies("notification_todos", "notification_todos.workspace_id")

    op.execute(
        "CREATE POLICY notification_todo_workspace_exit_update ON notification_todos "
        "FOR UPDATE USING (workspace_id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active') WITH CHECK ("
        "workspace_id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active')"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS notification_todo_workspace_exit_update ON notification_todos"
    )
    for table in (
        "notification_todos",
        "issues",
        "playtest_sessions",
        "playtest_plans",
        "files",
        "works",
        "work_accesses",
        "workspace_invitations",
        "workspace_members",
    ):
        _drop_exit_policies(table)
    op.execute("DROP POLICY IF EXISTS workspace_exit_processor_update ON workspaces")
    op.execute("DROP POLICY IF EXISTS workspace_exit_request_update ON workspaces")
    op.execute("DROP POLICY IF EXISTS workspace_exit_processor_read ON workspaces")
    op.execute("DROP POLICY IF EXISTS workspace_exit_read ON workspaces")
    op.execute("DROP FUNCTION public.workspace_exit_processor()")
    op.execute("DROP FUNCTION public.workspace_exit_maintenance_id()")
    op.drop_index("ix_workspaces_exit_due", table_name="workspaces")
    op.drop_constraint(
        "ck_workspace_exit_completion_requires_request", "workspaces", type_="check"
    )
    op.drop_constraint("ck_workspace_exit_request_shape", "workspaces", type_="check")
    op.drop_constraint("ck_workspace_revision_positive", "workspaces", type_="check")
    op.drop_constraint(
        "fk_workspaces_exit_requested_by_account", "workspaces", type_="foreignkey"
    )
    op.drop_column("workspaces", "exit_completed_at")
    op.drop_column("workspaces", "exit_operation_key")
    op.drop_column("workspaces", "exit_requested_by_account_id")
    op.drop_column("workspaces", "exit_read_until")
    op.drop_column("workspaces", "exit_requested_at")
    op.drop_column("workspaces", "revision")
