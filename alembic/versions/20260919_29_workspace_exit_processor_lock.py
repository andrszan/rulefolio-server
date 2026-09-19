"""允许退出处理器领取到期工作空间。"""

from alembic import op

revision = "20260919_29"
down_revision = "20260919_28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP POLICY workspace_exit_processor_update ON workspaces")
    op.execute(
        "CREATE POLICY workspace_exit_processor_update ON workspaces FOR UPDATE USING ("
        "public.workspace_exit_processor() = 'active') WITH CHECK ("
        "id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active')"
    )


def downgrade() -> None:
    op.execute("DROP POLICY workspace_exit_processor_update ON workspaces")
    op.execute(
        "CREATE POLICY workspace_exit_processor_update ON workspaces FOR UPDATE USING ("
        "id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active') WITH CHECK ("
        "id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active')"
    )
