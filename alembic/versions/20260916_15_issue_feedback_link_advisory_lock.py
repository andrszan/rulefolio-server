"""串行化问题关联与反馈状态变更。"""

from alembic import op

revision = "20260916_15"
down_revision = "20260916_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_linked_feedback_submission_draft()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.status = 'draft' THEN
                PERFORM pg_advisory_xact_lock(hashtextextended(NEW.id::text, 0));
                IF EXISTS (
                    SELECT 1
                    FROM public.issue_evidence_links
                    WHERE feedback_submission_id = NEW.id
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_linked_feedback_submission_status';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
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


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_linked_feedback_submission_draft()
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
