"""创建私有作品与作品级访问边界。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260915_03"
down_revision = "20260915_02"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    op.create_table(
        "works",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("creative_stage", sa.String(length=160), nullable=False),
        sa.Column("target_experience", sa.Text(), nullable=False),
        sa.Column("min_players", sa.Integer(), nullable=False),
        sa.Column("max_players", sa.Integer(), nullable=False),
        sa.Column("estimated_duration_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "revision", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("min_players > 0", name="ck_work_min_players_positive"),
        sa.CheckConstraint("max_players >= min_players", name="ck_work_player_range"),
        sa.CheckConstraint(
            "estimated_duration_minutes > 0",
            name="ck_work_estimated_duration_positive",
        ),
        sa.CheckConstraint("revision > 0", name="ck_work_revision_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_work_id_workspace"),
    )
    op.create_index(
        "ix_works_workspace_created", "works", ["workspace_id", "created_at", "id"]
    )
    op.create_table(
        "work_accesses",
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role IN ('maintainer', 'organizer', 'collaborator')",
            name="ck_work_access_role",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["work_id", "workspace_id"],
            ["works.id", "works.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("work_id", "account_id"),
    )
    op.create_index(
        "ix_work_accesses_account_work", "work_accesses", ["account_id", "work_id"]
    )

    app_role = _app_role()
    op.execute(f"GRANT SELECT, INSERT ON TABLE works TO {app_role}")
    op.execute(
        "GRANT UPDATE (name, description, creative_stage, target_experience, "
        "min_players, max_players, estimated_duration_minutes, revision, updated_at) "
        f"ON TABLE works TO {app_role}"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON TABLE work_accesses TO {app_role}")
    op.execute(f"GRANT UPDATE (role) ON TABLE work_accesses TO {app_role}")
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    op.execute(
        """
        CREATE FUNCTION public.work_management_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.work_management_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.work_management_workspace_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.work_management_workspace_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.work_access_cleanup_workspace_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.work_access_cleanup_workspace_id', true), '')::uuid
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION public.work_management_id() TO {app_role}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.work_management_workspace_id() TO {app_role}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.work_access_cleanup_workspace_id() TO {app_role}"
    )

    for table in ("works", "work_accesses"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY work_read ON works FOR SELECT USING (
            (
                EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = works.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
                AND EXISTS (
                    SELECT 1 FROM work_accesses access
                    WHERE access.work_id = works.id
                      AND access.account_id = public.workspace_actor_id()
                )
            )
            OR (
                works.id = public.work_management_id()
                AND EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id = works.workspace_id
                      AND member.account_id = public.workspace_actor_id()
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_insert ON works FOR INSERT WITH CHECK (
            EXISTS (
                SELECT 1 FROM workspace_members member
                WHERE member.workspace_id = works.workspace_id
                  AND member.account_id = public.workspace_actor_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_update ON works FOR UPDATE
        USING (
            works.id = public.work_management_id()
            AND EXISTS (
                SELECT 1 FROM workspace_members member
                WHERE member.workspace_id = works.workspace_id
                  AND member.account_id = public.workspace_actor_id()
            )
            AND EXISTS (
                SELECT 1 FROM work_accesses access
                WHERE access.work_id = works.id
                  AND access.account_id = public.workspace_actor_id()
                  AND access.role = 'maintainer'
            )
        )
        WITH CHECK (
            works.id = public.work_management_id()
            AND EXISTS (
                SELECT 1 FROM workspace_members member
                WHERE member.workspace_id = works.workspace_id
                  AND member.account_id = public.workspace_actor_id()
            )
            AND EXISTS (
                SELECT 1 FROM work_accesses access
                WHERE access.work_id = works.id
                  AND access.account_id = public.workspace_actor_id()
                  AND access.role = 'maintainer'
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_access_read ON work_accesses FOR SELECT USING (
            account_id = public.workspace_actor_id()
            OR (
                work_id = public.work_management_id()
                AND workspace_id = public.work_management_workspace_id()
            )
            OR workspace_id = public.work_access_cleanup_workspace_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_access_insert ON work_accesses FOR INSERT WITH CHECK (
            work_id = public.work_management_id()
            AND workspace_id = public.work_management_workspace_id()
            AND EXISTS (
                SELECT 1 FROM workspace_members member
                WHERE member.workspace_id = public.work_management_workspace_id()
                  AND member.account_id = work_accesses.account_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_access_update ON work_accesses FOR UPDATE
        USING (
            work_id = public.work_management_id()
            AND workspace_id = public.work_management_workspace_id()
        )
        WITH CHECK (
            work_id = public.work_management_id()
            AND workspace_id = public.work_management_workspace_id()
            AND EXISTS (
                SELECT 1 FROM workspace_members member
                WHERE member.workspace_id = public.work_management_workspace_id()
                  AND member.account_id = work_accesses.account_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_access_delete ON work_accesses FOR DELETE USING (
            (
                work_id = public.work_management_id()
                AND workspace_id = public.work_management_workspace_id()
            )
            OR workspace_id = public.work_access_cleanup_workspace_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY work_access_cleanup_lock ON work_accesses FOR UPDATE
        USING (workspace_id = public.work_access_cleanup_workspace_id())
        WITH CHECK (false)
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_member_work_management_read
        ON workspace_members FOR SELECT USING (
            workspace_id = public.work_management_workspace_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_member_work_management_lock
        ON workspace_members FOR UPDATE
        USING (workspace_id = public.work_management_workspace_id())
        WITH CHECK (false)
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS workspace_member_work_management_lock ON workspace_members"
    )
    op.execute(
        "DROP POLICY IF EXISTS workspace_member_work_management_read ON workspace_members"
    )
    for policy, table in (
        ("work_read", "works"),
        ("work_insert", "works"),
        ("work_update", "works"),
        ("work_access_read", "work_accesses"),
        ("work_access_insert", "work_accesses"),
        ("work_access_update", "work_accesses"),
        ("work_access_delete", "work_accesses"),
        ("work_access_cleanup_lock", "work_accesses"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.drop_index("ix_work_accesses_account_work", table_name="work_accesses")
    op.drop_table("work_accesses")
    op.drop_index("ix_works_workspace_created", table_name="works")
    op.drop_table("works")
    op.execute("DROP FUNCTION public.work_access_cleanup_workspace_id()")
    op.execute("DROP FUNCTION public.work_management_workspace_id()")
    op.execute("DROP FUNCTION public.work_management_id()")
