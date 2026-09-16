"""增加个人待办、受控邮件重投和站内邀请接收范围。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260916_24"
down_revision = "20260916_23"
branch_labels = None
depends_on = None


def _role(name: str | None) -> str:
    if name is None:
        raise ValueError("迁移必须配置对应数据库身份")
    return op.get_bind().dialect.identifier_preparer.quote(name)


def _app_role() -> str:
    return _role(settings.db_user)


def _scope_functions(app_role: str) -> None:
    functions = {
        "notification_todo_recipient_id": "uuid",
        "notification_todo_kind": "text",
        "notification_todo_source_key": "text",
        "notification_todo_target_kind": "text",
        "notification_todo_target_id": "uuid",
        "notification_todo_cleanup_recipient_id": "uuid",
        "notification_todo_cleanup_work_id": "uuid",
        "notification_todo_dispatch_id": "uuid",
        "workspace_invitation_inbox_id": "uuid",
    }
    for name, value_type in functions.items():
        op.execute(
            f"""
            CREATE FUNCTION public.{name}() RETURNS {value_type}
            LANGUAGE sql STABLE AS $$
                SELECT NULLIF(current_setting('app.{name}', true), '')::{value_type}
            $$
            """
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{name}() TO {app_role}")


def _drop_scope_functions() -> None:
    for name in (
        "workspace_invitation_inbox_id",
        "notification_todo_dispatch_id",
        "notification_todo_cleanup_work_id",
        "notification_todo_cleanup_recipient_id",
        "notification_todo_target_id",
        "notification_todo_target_kind",
        "notification_todo_source_key",
        "notification_todo_kind",
        "notification_todo_recipient_id",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{name}()")


def _todo_scope() -> str:
    return """
        recipient_account_id = public.notification_todo_recipient_id()
        AND kind = public.notification_todo_kind()
        AND source_key = public.notification_todo_source_key()
    """


def _target_scope() -> str:
    return """
        target_kind = public.notification_todo_target_kind()
        AND target_id = public.notification_todo_target_id()
    """


def _cleanup_scope() -> str:
    return """
        recipient_account_id = public.notification_todo_cleanup_recipient_id()
        AND work_id = public.notification_todo_cleanup_work_id()
    """


def _todo_access_scope() -> str:
    return f"""
        recipient_account_id = public.workspace_actor_id()
        OR ({_todo_scope()})
        OR ({_target_scope()})
        OR ({_cleanup_scope()})
        OR id = public.notification_todo_dispatch_id()
        OR public.project_baseline_maintenance()
    """


def upgrade() -> None:
    app_role = _app_role()
    op.create_table(
        "notification_todos",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recipient_account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True)),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_key", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(length=200), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="open"
        ),
        sa.Column(
            "retry_used", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('workspace_invitation', 'playtest_invitation', "
            "'playtest_arrangement_updated', 'playtest_material_updated', "
            "'playtest_cancelled', 'feedback_submitted', 'issue_opened', "
            "'retest_arrangement_needed', 'retest_arranged')",
            name="ck_notification_todo_kind",
        ),
        sa.CheckConstraint(
            "target_kind IN ('workspace_invitation', 'playtest_session', "
            "'feedback_submission', 'issue', 'issue_retest')",
            name="ck_notification_todo_target_kind",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'completed', 'cancelled')",
            name="ck_notification_todo_status",
        ),
        sa.CheckConstraint(
            "btrim(source_key) <> ''", name="ck_notification_todo_source_key"
        ),
        sa.CheckConstraint("btrim(summary) <> ''", name="ck_notification_todo_summary"),
        sa.ForeignKeyConstraint(
            ["recipient_account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recipient_account_id",
            "kind",
            "source_key",
            name="uq_notification_todo_recipient_kind_source",
        ),
    )
    op.create_index(
        "ix_notification_todo_recipient_status_created",
        "notification_todos",
        ["recipient_account_id", "status", sa.text("created_at DESC"), "id"],
    )
    op.add_column("mail_outbox", sa.Column("todo_id", postgresql.UUID(as_uuid=True)))
    op.add_column(
        "mail_outbox", sa.Column("retry_operation_key", sa.String(length=128))
    )
    op.create_foreign_key(
        "fk_mail_outbox_todo_id",
        "mail_outbox",
        "notification_todos",
        ["todo_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_mail_outbox_retry_business_only",
        "mail_outbox",
        "retry_operation_key IS NULL OR credential_id IS NULL",
    )
    op.create_index(
        "uq_mail_outbox_todo_retry_operation",
        "mail_outbox",
        ["todo_id", "retry_operation_key"],
        unique=True,
        postgresql_where=sa.text("retry_operation_key IS NOT NULL"),
    )
    op.create_index(
        "ix_mail_outbox_todo_created",
        "mail_outbox",
        ["todo_id", sa.text("created_at DESC"), "id"],
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE notification_todos "
        f"TO {app_role}"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    _scope_functions(app_role)

    op.execute("ALTER TABLE notification_todos ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notification_todos FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY notification_todo_read ON notification_todos FOR SELECT USING ("
        f"{_todo_access_scope()})"
    )
    op.execute(
        "CREATE POLICY notification_todo_insert ON notification_todos FOR INSERT WITH CHECK ("
        f"{_todo_scope()})"
    )
    op.execute(
        "CREATE POLICY notification_todo_update ON notification_todos FOR UPDATE USING ("
        f"{_todo_access_scope()}) WITH CHECK ({_todo_access_scope()})"
    )
    op.execute(
        "CREATE POLICY notification_todo_delete ON notification_todos FOR DELETE USING ("
        f"({_cleanup_scope()}) OR public.project_baseline_maintenance())"
    )

    op.execute(
        "CREATE POLICY workspace_invitation_inbox_read ON workspace_invitations "
        "FOR SELECT USING ("
        "id = public.workspace_invitation_inbox_id() "
        "AND account_id = public.workspace_actor_id())"
    )
    op.execute(
        "CREATE POLICY workspace_invitation_inbox_update ON workspace_invitations "
        "FOR UPDATE USING ("
        "id = public.workspace_invitation_inbox_id() "
        "AND account_id = public.workspace_actor_id()) WITH CHECK ("
        "id = public.workspace_invitation_inbox_id() "
        "AND account_id = public.workspace_actor_id())"
    )
    op.execute(
        "CREATE POLICY workspace_member_inbox_insert ON workspace_members FOR INSERT "
        "WITH CHECK (EXISTS ("
        "SELECT 1 FROM workspace_invitations invitation "
        "WHERE invitation.id = public.workspace_invitation_inbox_id() "
        "AND invitation.workspace_id = workspace_members.workspace_id "
        "AND invitation.account_id = workspace_members.account_id "
        "AND invitation.account_id = public.workspace_actor_id() "
        "AND invitation.status = 'active'))"
    )


def downgrade() -> None:
    for policy, table in (
        ("workspace_member_inbox_insert", "workspace_members"),
        ("workspace_invitation_inbox_update", "workspace_invitations"),
        ("workspace_invitation_inbox_read", "workspace_invitations"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    for policy in (
        "notification_todo_delete",
        "notification_todo_update",
        "notification_todo_insert",
        "notification_todo_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON notification_todos")
    _drop_scope_functions()
    op.drop_index("ix_mail_outbox_todo_created", table_name="mail_outbox")
    op.drop_index("uq_mail_outbox_todo_retry_operation", table_name="mail_outbox")
    op.drop_constraint(
        "ck_mail_outbox_retry_business_only", "mail_outbox", type_="check"
    )
    op.drop_constraint("fk_mail_outbox_todo_id", "mail_outbox", type_="foreignkey")
    op.drop_column("mail_outbox", "retry_operation_key")
    op.drop_column("mail_outbox", "todo_id")
    op.drop_index(
        "ix_notification_todo_recipient_status_created",
        table_name="notification_todos",
    )
    op.drop_table("notification_todos")
