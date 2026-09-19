"""允许退出处理器读取待取消的工作空间待办。"""

from alembic import op

revision = "20260919_30"
down_revision = "20260919_29"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE POLICY notification_todo_workspace_exit_read ON notification_todos "
        "FOR SELECT USING (workspace_id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active')"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY notification_todo_workspace_exit_read ON notification_todos"
    )
