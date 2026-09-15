"""增加项目基线重置的最小权限与 RLS scope。"""

from alembic import op
from app.core.config import settings

revision = "20260915_05"
down_revision = "20260915_04"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    app_role = _app_role()
    op.execute(f"GRANT DELETE ON TABLE works, files TO {app_role}")
    op.execute(
        """
        CREATE FUNCTION public.project_baseline_maintenance() RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT current_setting('app.project_baseline_maintenance', true) = 'active'
        $$
        """
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.project_baseline_maintenance() TO {app_role}"
    )

    for policy, table in (
        ("project_baseline_workspace_read", "workspaces"),
        ("project_baseline_workspace_delete", "workspaces"),
        ("project_baseline_member_read", "workspace_members"),
        ("project_baseline_member_delete", "workspace_members"),
        ("project_baseline_invitation_read", "workspace_invitations"),
        ("project_baseline_invitation_delete", "workspace_invitations"),
        ("project_baseline_work_read", "works"),
        ("project_baseline_work_delete", "works"),
        ("project_baseline_access_read", "work_accesses"),
        ("project_baseline_access_delete", "work_accesses"),
        ("project_baseline_file_read", "files"),
        ("project_baseline_file_delete", "files"),
    ):
        command = "SELECT" if policy.endswith("_read") else "DELETE"
        op.execute(
            f"CREATE POLICY {policy} ON {table} FOR {command} "
            "USING (public.project_baseline_maintenance())"
        )


def downgrade() -> None:
    for policy, table in (
        ("project_baseline_workspace_read", "workspaces"),
        ("project_baseline_workspace_delete", "workspaces"),
        ("project_baseline_member_read", "workspace_members"),
        ("project_baseline_member_delete", "workspace_members"),
        ("project_baseline_invitation_read", "workspace_invitations"),
        ("project_baseline_invitation_delete", "workspace_invitations"),
        ("project_baseline_work_read", "works"),
        ("project_baseline_work_delete", "works"),
        ("project_baseline_access_read", "work_accesses"),
        ("project_baseline_access_delete", "work_accesses"),
        ("project_baseline_file_read", "files"),
        ("project_baseline_file_delete", "files"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute("DROP FUNCTION public.project_baseline_maintenance()")
    app_role = _app_role()
    op.execute(f"REVOKE DELETE ON TABLE works, files FROM {app_role}")
