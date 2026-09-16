"""补充待办安全上下文标签。"""

import sqlalchemy as sa

from alembic import op

revision = "20260916_25"
down_revision = "20260916_24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_todos", sa.Column("context_label", sa.String(length=200))
    )
    op.execute("ALTER TABLE notification_todos NO FORCE ROW LEVEL SECURITY")
    try:
        op.execute("UPDATE notification_todos SET context_label = summary")
    finally:
        op.execute("ALTER TABLE notification_todos FORCE ROW LEVEL SECURITY")
    op.alter_column("notification_todos", "context_label", nullable=False)
    op.create_check_constraint(
        "ck_notification_todo_context_label",
        "notification_todos",
        "btrim(context_label) <> ''",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_notification_todo_context_label",
        "notification_todos",
        type_="check",
    )
    op.drop_column("notification_todos", "context_label")
