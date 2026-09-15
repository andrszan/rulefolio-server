"""增加当前规则与材料。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260915_06"
down_revision = "20260915_05"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    app_role = _app_role()
    op.add_column("works", sa.Column("rule_name", sa.String(length=160)))
    op.add_column("works", sa.Column("rule_description", sa.Text()))
    op.add_column("works", sa.Column("rule_content", sa.Text()))
    op.create_check_constraint(
        "ck_work_current_rule_complete",
        "works",
        "(rule_name IS NULL AND rule_description IS NULL AND rule_content IS NULL) "
        "OR (btrim(rule_name) <> '' AND btrim(rule_content) <> '')",
    )
    op.execute(
        "GRANT UPDATE (rule_name, rule_description, rule_content, revision, updated_at) "
        f"ON TABLE works TO {app_role}"
    )

    op.add_column(
        "files",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'image'"),
        ),
    )
    op.create_check_constraint("ck_file_kind", "files", "kind IN ('image', 'material')")
    op.alter_column("files", "kind", server_default=None)

    op.create_table(
        "work_material_files",
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("work_id", "file_id"),
    )
    op.execute(
        f"GRANT SELECT, INSERT, DELETE ON TABLE work_material_files TO {app_role}"
    )
    op.execute("ALTER TABLE work_material_files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE work_material_files FORCE ROW LEVEL SECURITY")

    op.execute("DROP POLICY file_update ON files")
    op.execute("DROP POLICY file_insert ON files")
    op.execute("DROP POLICY file_read ON files")
    op.execute(
        """
        CREATE POLICY file_read ON files FOR SELECT USING (
            id = public.file_lifecycle_id()
            OR (
                kind = 'image'
                AND status = 'ready'
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
                kind = 'material'
                AND status = 'ready'
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = files.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = files.work_id
                      AND access.account_id = public.workspace_actor_id()
                      AND access.role = 'maintainer'
                )
            )
            OR (
                kind = 'image'
                AND status = 'pending'
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
            OR (
                kind = 'material'
                AND status = 'pending'
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
                      AND access.role = 'maintainer'
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
                  AND (files.kind = 'image' OR access.role = 'maintainer')
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
                          AND (files.kind = 'image' OR access.role = 'maintainer')
                    )
                )
            )
        )
        """
    )

    op.execute(
        """
        CREATE POLICY work_material_file_read ON work_material_files FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM works
                WHERE works.id = work_material_files.work_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_material_file_insert ON work_material_files FOR INSERT WITH CHECK (
            work_id = public.work_management_id()
            AND EXISTS (
                SELECT 1 FROM work_accesses access
                WHERE access.work_id = work_material_files.work_id
                  AND access.account_id = public.workspace_actor_id()
                  AND access.role = 'maintainer'
            )
            AND EXISTS (
                SELECT 1 FROM files
                WHERE files.id = work_material_files.file_id
                  AND files.work_id = work_material_files.work_id
                  AND files.kind = 'material'
                  AND files.status = 'ready'
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_material_file_delete ON work_material_files FOR DELETE USING (
            work_id = public.work_management_id()
            AND EXISTS (
                SELECT 1 FROM work_accesses access
                WHERE access.work_id = work_material_files.work_id
                  AND access.account_id = public.workspace_actor_id()
                  AND access.role = 'maintainer'
            )
        )
        """
    )
    op.execute(
        "CREATE POLICY project_baseline_material_read ON work_material_files FOR SELECT "
        "USING (public.project_baseline_maintenance())"
    )
    op.execute(
        "CREATE POLICY project_baseline_material_delete ON work_material_files FOR DELETE "
        "USING (public.project_baseline_maintenance())"
    )


def downgrade() -> None:
    app_role = _app_role()
    op.execute("DROP POLICY file_update ON files")
    op.execute("DROP POLICY file_insert ON files")
    op.execute("DROP POLICY file_read ON files")
    for policy in (
        "project_baseline_material_delete",
        "project_baseline_material_read",
        "work_material_file_delete",
        "work_material_file_insert",
        "work_material_file_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON work_material_files")
    op.drop_table("work_material_files")

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
    op.drop_constraint("ck_file_kind", "files")
    op.drop_column("files", "kind")
    op.execute(
        f"REVOKE UPDATE (rule_name, rule_description, rule_content) ON TABLE works FROM {app_role}"
    )
    op.drop_constraint("ck_work_current_rule_complete", "works")
    op.drop_column("works", "rule_content")
    op.drop_column("works", "rule_description")
    op.drop_column("works", "rule_name")
