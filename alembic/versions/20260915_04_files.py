"""创建私有作品图片与对象生命周期。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260915_04"
down_revision = "20260915_03"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uploader_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("declared_content_type", sa.String(length=127), nullable=False),
        sa.Column("detected_content_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_file_status"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_file_size_nonnegative"),
        sa.ForeignKeyConstraint(
            ["uploader_account_id"], ["identity_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["work_id", "workspace_id"],
            ["works.id", "works.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_file_object_key"),
    )
    op.create_index(
        "ix_files_work_status_created",
        "files",
        ["work_id", "status", "created_at", "id"],
    )

    app_role = _app_role()
    op.execute(f"GRANT SELECT, INSERT ON TABLE files TO {app_role}")
    op.execute(f"GRANT UPDATE (status, completed_at) ON TABLE files TO {app_role}")
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    op.execute(
        """
        CREATE FUNCTION public.file_lifecycle_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.file_lifecycle_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.file_recovery_work_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.file_recovery_work_id', true), '')::uuid
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION public.file_lifecycle_id() TO {app_role}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.file_recovery_work_id() TO {app_role}"
    )
    op.execute("ALTER TABLE files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE files FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY file_read ON files FOR SELECT USING (
            id = public.file_lifecycle_id()
            OR (
                status = 'ready'
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                )
            )
            OR (
                status = 'pending'
                AND work_id = public.file_recovery_work_id()
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY file_insert ON files FOR INSERT WITH CHECK (
            id = public.file_lifecycle_id()
            AND status = 'pending'
            AND uploader_account_id = public.workspace_actor_id()
            AND EXISTS (
                SELECT 1 FROM workspace_members member
                WHERE member.workspace_id = files.workspace_id
                  AND member.account_id = public.workspace_actor_id()
            )
            AND EXISTS (
                SELECT 1 FROM work_accesses access
                WHERE access.work_id = files.work_id
                  AND access.account_id = public.workspace_actor_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY file_update ON files FOR UPDATE
        USING (
            id = public.file_lifecycle_id()
            AND status = 'pending'
        )
        WITH CHECK (
            id = public.file_lifecycle_id()
            AND completed_at IS NOT NULL
            AND (
                status = 'failed'
                OR (
                    status = 'ready'
                    AND EXISTS (
                        SELECT 1 FROM workspace_members member
                        WHERE member.workspace_id = files.workspace_id
                          AND member.account_id = public.workspace_actor_id()
                    )
                    AND EXISTS (
                        SELECT 1 FROM work_accesses access
                        WHERE access.work_id = files.work_id
                          AND access.account_id = public.workspace_actor_id()
                    )
                )
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS file_update ON files")
    op.execute("DROP POLICY IF EXISTS file_insert ON files")
    op.execute("DROP POLICY IF EXISTS file_read ON files")
    op.drop_index("ix_files_work_status_created", table_name="files")
    op.drop_table("files")
    op.execute("DROP FUNCTION public.file_recovery_work_id()")
    op.execute("DROP FUNCTION public.file_lifecycle_id()")
