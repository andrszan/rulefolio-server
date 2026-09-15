"""保持关联问题的反馈提交状态。"""

from alembic import op
from app.core.config import settings

revision = "20260916_13"
down_revision = "20260916_12"
branch_labels = None
depends_on = None


def _role(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def upgrade() -> None:
    app_role = _role(settings.db_user)
    migrator_role = _role(settings.migrator_db_user)
    op.execute(
        f"CREATE POLICY issue_evidence_link_submission_guard ON issue_evidence_links "
        f"FOR SELECT TO {migrator_role} USING (true)"
    )
    op.execute(
        """
        CREATE FUNCTION public.prevent_linked_feedback_submission_draft()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.status = 'draft' AND EXISTS (
                SELECT 1
                FROM public.issue_evidence_links
                WHERE feedback_submission_id = NEW.id
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'ck_linked_feedback_submission_status';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.prevent_linked_feedback_submission_draft() "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.prevent_linked_feedback_submission_draft() "
        f"TO {app_role}"
    )
    op.execute(
        "CREATE TRIGGER prevent_linked_feedback_submission_draft "
        "BEFORE UPDATE OF status ON playtest_feedback_submissions "
        "FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status) "
        "EXECUTE FUNCTION public.prevent_linked_feedback_submission_draft()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS prevent_linked_feedback_submission_draft "
        "ON playtest_feedback_submissions"
    )
    op.execute("DROP FUNCTION public.prevent_linked_feedback_submission_draft()")
    op.execute(
        "DROP POLICY IF EXISTS issue_evidence_link_submission_guard "
        "ON issue_evidence_links"
    )
