"""以安全函数解除工作空间退出策略的 RLS 循环。"""

from alembic import op
from app.core.config import settings

revision = "20260919_27"
down_revision = "20260919_26"
branch_labels = None
depends_on = None

_TABLES = (
    ("workspace_members", "workspace_members.workspace_id"),
    ("workspace_invitations", "workspace_invitations.workspace_id"),
    ("work_accesses", "work_accesses.workspace_id"),
    ("works", "works.workspace_id"),
    ("files", "files.workspace_id"),
    ("playtest_plans", "playtest_plans.workspace_id"),
    ("playtest_sessions", "playtest_sessions.workspace_id"),
    ("issues", "issues.workspace_id"),
    ("notification_todos", "notification_todos.workspace_id"),
)


def _role(name: str | None) -> str:
    if name is None:
        raise ValueError("迁移必须配置对应数据库身份")
    return op.get_bind().dialect.identifier_preparer.quote(name)


def _app_role() -> str:
    return _role(settings.db_user)


def _migrator_role() -> str:
    return _role(settings.migrator_db_user)


def _drop_exit_policies(table: str) -> None:
    for action in ("delete", "update", "insert", "read"):
        op.execute(f"DROP POLICY {table}_workspace_exit_{action} ON {table}")


def _create_function_guarded_policies(table: str, workspace_id: str) -> None:
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_read ON {table} AS RESTRICTIVE "
        f"FOR SELECT USING (public.workspace_exit_allows_read({workspace_id}))"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_insert ON {table} AS RESTRICTIVE "
        f"FOR INSERT WITH CHECK (public.workspace_exit_allows_write({workspace_id}))"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_update ON {table} AS RESTRICTIVE "
        f"FOR UPDATE USING (public.workspace_exit_allows_write({workspace_id})) "
        f"WITH CHECK (public.workspace_exit_allows_write({workspace_id}))"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_delete ON {table} AS RESTRICTIVE "
        f"FOR DELETE USING (public.workspace_exit_allows_write({workspace_id}))"
    )


def _create_direct_policies(table: str, workspace_id: str) -> None:
    read = f"""
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
    write = f"""
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


def upgrade() -> None:
    app_role = _app_role()
    migrator_role = _migrator_role()
    for table, _ in _TABLES:
        _drop_exit_policies(table)
    op.execute(f"ALTER POLICY workspace_read ON workspaces TO {app_role}")
    op.execute(
        f"CREATE POLICY workspace_exit_guard_read ON workspaces FOR SELECT TO {migrator_role} "
        "USING (true)"
    )
    op.execute(
        """
        CREATE FUNCTION public.workspace_exit_allows_read(p_workspace_id uuid)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT EXISTS (
                SELECT 1
                FROM public.workspaces workspace
                WHERE workspace.id = p_workspace_id
                  AND (
                      workspace.exit_read_until IS NULL
                      OR workspace.exit_read_until > CURRENT_TIMESTAMP
                      OR (
                          workspace.id = public.workspace_exit_maintenance_id()
                          AND public.workspace_exit_processor() = 'active'
                      )
                  )
            )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.workspace_exit_allows_write(p_workspace_id uuid)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT EXISTS (
                SELECT 1
                FROM public.workspaces workspace
                WHERE workspace.id = p_workspace_id
                  AND (
                      workspace.exit_requested_at IS NULL
                      OR (
                          workspace.id = public.workspace_exit_maintenance_id()
                          AND public.workspace_exit_processor() = 'active'
                      )
                  )
            )
        $$
        """
    )
    for signature in (
        "workspace_exit_allows_read(uuid)",
        "workspace_exit_allows_write(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {app_role}")
    for table, workspace_id in _TABLES:
        _create_function_guarded_policies(table, workspace_id)


def downgrade() -> None:
    for table, _ in _TABLES:
        _drop_exit_policies(table)
    for signature in (
        "workspace_exit_allows_write(uuid)",
        "workspace_exit_allows_read(uuid)",
    ):
        op.execute(f"DROP FUNCTION public.{signature}")
    op.execute("DROP POLICY workspace_exit_guard_read ON workspaces")
    op.execute("ALTER POLICY workspace_read ON workspaces TO PUBLIC")
    for table, workspace_id in _TABLES:
        _create_direct_policies(table, workspace_id)
