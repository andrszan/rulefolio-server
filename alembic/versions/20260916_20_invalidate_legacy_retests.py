"""使无法证明归属当前调整的遗留复测失效。"""

from alembic import op

revision = "20260916_20"
down_revision = "20260916_19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE issues SET adjustment_generation = adjustment_generation + 1 "
        "WHERE adjustment_note IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM issue_retest_links "
        "WHERE issue_retest_links.issue_id = issues.id)"
    )
    op.execute(
        "UPDATE issue_retest_links SET conclusion = NULL, conclusion_reason = NULL, "
        "updated_at = now() WHERE conclusion IS NOT NULL"
    )


def downgrade() -> None:
    pass
