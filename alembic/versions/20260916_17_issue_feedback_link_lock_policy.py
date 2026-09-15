"""允许关联守卫取得反馈行锁。"""

from alembic import op
from app.core.config import settings

revision = "20260916_17"
down_revision = "20260916_16"
branch_labels = None
depends_on = None


def _role(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def upgrade() -> None:
    migrator_role = _role(settings.migrator_db_user)
    op.execute(
        "CREATE POLICY issue_evidence_feedback_submission_lock_guard "
        "ON playtest_feedback_submissions FOR UPDATE "
        f"TO {migrator_role} USING (true)"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS issue_evidence_feedback_submission_lock_guard "
        "ON playtest_feedback_submissions"
    )
