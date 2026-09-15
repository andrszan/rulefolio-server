"""补齐已发布场次迁移的行锁 RLS policy。"""

from alembic import op

revision = "20260915_08"
down_revision = "20260915_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename = 'works'
                  AND policyname = 'work_playtest_lock'
            ) THEN
                CREATE POLICY work_playtest_lock ON works FOR UPDATE
                USING (
                    works.id = public.playtest_management_work_id()
                    AND EXISTS (
                        SELECT 1 FROM work_accesses access
                        WHERE access.work_id = works.id
                          AND access.account_id = public.workspace_actor_id()
                          AND access.role IN ('maintainer', 'organizer')
                    )
                )
                WITH CHECK (false);
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename = 'playtest_sessions'
                  AND policyname = 'playtest_session_confirmation_lock'
            ) THEN
                CREATE POLICY playtest_session_confirmation_lock
                ON playtest_sessions FOR UPDATE
                USING (id = public.playtest_session_id())
                WITH CHECK (false);
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    # 20260915_07 的完整新建路径已经包含这些 policy。
    pass
