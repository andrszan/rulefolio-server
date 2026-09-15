"""防止问题关联草稿反馈。"""

from alembic import op
from app.core.config import settings

revision = "20260916_14"
down_revision = "20260916_13"
branch_labels = None
depends_on = None


def _role(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def upgrade() -> None:
    app_role = _role(settings.db_user)
    migrator_role = _role(settings.migrator_db_user)
    op.execute(
        "CREATE POLICY issue_evidence_feedback_submission_status_guard "
        "ON playtest_feedback_submissions FOR SELECT "
        f"TO {migrator_role} USING (true)"
    )
    op.execute(
        """
        CREATE FUNCTION public.prevent_issue_link_to_draft_feedback()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            submission_status text;
        BEGIN
            IF NEW.feedback_submission_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT status INTO submission_status
            FROM public.playtest_feedback_submissions
            WHERE id = NEW.feedback_submission_id
            FOR SHARE;
            IF submission_status IS DISTINCT FROM 'submitted' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'ck_issue_evidence_link_feedback_submission_status';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.prevent_issue_link_to_draft_feedback() "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.prevent_issue_link_to_draft_feedback() "
        f"TO {app_role}"
    )
    op.execute(
        "CREATE TRIGGER prevent_issue_link_to_draft_feedback "
        "BEFORE INSERT OR UPDATE OF feedback_submission_id ON issue_evidence_links "
        "FOR EACH ROW EXECUTE FUNCTION public.prevent_issue_link_to_draft_feedback()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS prevent_issue_link_to_draft_feedback "
        "ON issue_evidence_links"
    )
    op.execute("DROP FUNCTION public.prevent_issue_link_to_draft_feedback()")
    op.execute(
        "DROP POLICY IF EXISTS issue_evidence_feedback_submission_status_guard "
        "ON playtest_feedback_submissions"
    )
