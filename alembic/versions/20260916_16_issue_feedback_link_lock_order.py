"""统一问题关联与反馈保存的锁顺序。"""

from alembic import op

revision = "20260916_16"
down_revision = "20260916_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_issue_link_to_draft_feedback()
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
            FOR KEY SHARE;
            PERFORM pg_advisory_xact_lock(
                hashtextextended(NEW.feedback_submission_id::text, 0)
            );
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


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_issue_link_to_draft_feedback()
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
            PERFORM pg_advisory_xact_lock(
                hashtextextended(NEW.feedback_submission_id::text, 0)
            );
            SELECT status INTO submission_status
            FROM public.playtest_feedback_submissions
            WHERE id = NEW.feedback_submission_id;
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
