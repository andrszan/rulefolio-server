"""创建工作空间、成员、邀请与邀请兑换边界。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260915_02"
down_revision = "20260914_01"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    op.drop_constraint(
        "ck_identity_token_purpose", "identity_one_time_credentials", type_="check"
    )
    op.create_check_constraint(
        "ck_identity_token_purpose",
        "identity_one_time_credentials",
        "purpose IN ('account_activation', 'password_recovery', 'workspace_invitation')",
    )
    op.drop_index(
        "uq_identity_active_credential_per_purpose",
        table_name="identity_one_time_credentials",
    )
    op.create_index(
        "uq_identity_active_credential_per_purpose",
        "identity_one_time_credentials",
        ["account_id", "purpose"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND purpose IN ('account_activation', 'password_recovery')"
        ),
    )
    op.add_column("mail_outbox", sa.Column("workspace_name", sa.String(160)))

    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("owner_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_account_id"], ["identity_accounts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "workspace_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "account_id", name="uq_workspace_member"),
    )
    op.create_index(
        "ix_workspace_members_account",
        "workspace_members",
        ["account_id", "workspace_id"],
    )
    op.create_table(
        "workspace_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("accepted_operation_key", sa.String(length=128)),
        sa.Column("accepted_operation", sa.String(length=32)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'accepted', 'revoked', 'expired')",
            name="ck_workspace_invitation_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["identity_one_time_credentials.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id"),
    )
    op.create_index(
        "uq_workspace_active_invitation",
        "workspace_invitations",
        ["workspace_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_workspace_invitations_workspace",
        "workspace_invitations",
        ["workspace_id", "created_at"],
    )
    op.create_table(
        "workspace_invitation_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("subject_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count >= 0", name="ck_workspace_invitation_attempt_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purpose", "subject_hash", name="uq_workspace_invitation_attempt_subject"
        ),
    )

    app_role = _app_role()
    app_tables = ", ".join(
        (
            "workspaces",
            "workspace_members",
            "workspace_invitations",
            "workspace_invitation_attempts",
        )
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {app_tables} TO {app_role}"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    op.execute(
        """
        CREATE FUNCTION public.workspace_actor_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.actor_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.workspace_invitation_credential_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.invitation_credential_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.workspace_management_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.workspace_management_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.workspace_maintenance_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.maintenance_workspace_id', true), '')::uuid
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION public.workspace_actor_id() TO {app_role}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.workspace_invitation_credential_id() TO {app_role}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.workspace_management_id() TO {app_role}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.workspace_maintenance_id() TO {app_role}"
    )

    for table in (
        "workspaces",
        "workspace_members",
        "workspace_invitations",
        "workspace_invitation_attempts",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY workspace_read ON workspaces FOR SELECT USING (
            owner_account_id = public.workspace_actor_id()
            OR id = public.workspace_maintenance_id()
            OR EXISTS (
                SELECT 1 FROM workspace_members member
                WHERE member.workspace_id = workspaces.id
                  AND member.account_id = public.workspace_actor_id()
            )
            OR EXISTS (
                SELECT 1 FROM workspace_invitations invitation
                WHERE invitation.workspace_id = workspaces.id
                  AND invitation.credential_id = public.workspace_invitation_credential_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_insert ON workspaces FOR INSERT WITH CHECK (
            owner_account_id = public.workspace_actor_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_member_read ON workspace_members FOR SELECT USING (
            account_id = public.workspace_actor_id()
            OR workspace_id = public.workspace_management_id()
            OR workspace_id = public.workspace_maintenance_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_member_insert ON workspace_members FOR INSERT WITH CHECK (
            workspace_id = public.workspace_management_id()
            OR EXISTS (
                SELECT 1 FROM workspace_invitations invitation
                WHERE invitation.workspace_id = workspace_members.workspace_id
                  AND invitation.account_id = workspace_members.account_id
                  AND invitation.credential_id = public.workspace_invitation_credential_id()
                  AND invitation.status = 'active'
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_member_update ON workspace_members FOR UPDATE
        USING (workspace_id = public.workspace_management_id())
        WITH CHECK (workspace_id = public.workspace_management_id())
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_member_delete ON workspace_members FOR DELETE USING (
            workspace_id = public.workspace_management_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_invitation_read ON workspace_invitations FOR SELECT USING (
            workspace_id = public.workspace_management_id()
            OR workspace_id = public.workspace_maintenance_id()
            OR credential_id = public.workspace_invitation_credential_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_invitation_insert ON workspace_invitations FOR INSERT WITH CHECK (
            workspace_id = public.workspace_management_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_invitation_update ON workspace_invitations FOR UPDATE
        USING (
            workspace_id = public.workspace_management_id()
            OR credential_id = public.workspace_invitation_credential_id()
        )
        WITH CHECK (
            workspace_id = public.workspace_management_id()
            OR credential_id = public.workspace_invitation_credential_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY workspace_invitation_attempt_access
        ON workspace_invitation_attempts FOR ALL USING (true) WITH CHECK (true)
        """
    )


def downgrade() -> None:
    for policy, table in (
        ("workspace_read", "workspaces"),
        ("workspace_insert", "workspaces"),
        ("workspace_member_read", "workspace_members"),
        ("workspace_member_insert", "workspace_members"),
        ("workspace_member_update", "workspace_members"),
        ("workspace_member_delete", "workspace_members"),
        ("workspace_invitation_read", "workspace_invitations"),
        ("workspace_invitation_insert", "workspace_invitations"),
        ("workspace_invitation_update", "workspace_invitations"),
        ("workspace_invitation_attempt_access", "workspace_invitation_attempts"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.drop_table("workspace_invitation_attempts")
    op.drop_table("workspace_invitations")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.execute(
        "DELETE FROM identity_one_time_credentials WHERE purpose = 'workspace_invitation'"
    )
    op.drop_column("mail_outbox", "workspace_name")
    op.execute("DROP FUNCTION public.workspace_maintenance_id()")
    op.execute("DROP FUNCTION public.workspace_management_id()")
    op.execute("DROP FUNCTION public.workspace_invitation_credential_id()")
    op.execute("DROP FUNCTION public.workspace_actor_id()")
    op.drop_index(
        "uq_identity_active_credential_per_purpose",
        table_name="identity_one_time_credentials",
    )
    op.create_index(
        "uq_identity_active_credential_per_purpose",
        "identity_one_time_credentials",
        ["account_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_constraint(
        "ck_identity_token_purpose", "identity_one_time_credentials", type_="check"
    )
    op.create_check_constraint(
        "ck_identity_token_purpose",
        "identity_one_time_credentials",
        "purpose IN ('account_activation', 'password_recovery')",
    )
