"""允许管理者识别已锁定反馈题。"""

from alembic import op

revision = "20260916_11"
down_revision = "20260916_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE POLICY playtest_feedback_item_update_lookup ON playtest_feedback_items FOR UPDATE "
        "USING (session_id = public.feedback_management_session_id()) "
        "WITH CHECK (session_id = public.feedback_management_session_id() AND NOT is_locked)"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY playtest_feedback_item_update_lookup ON playtest_feedback_items"
    )
