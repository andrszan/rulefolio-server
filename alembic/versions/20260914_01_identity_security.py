"""创建账户、会话、一次性凭据、审计与邮件 outbox。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260914_01"
down_revision = None
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    op.create_table(
        "identity_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending_activation', 'active', 'disabled')",
            name="ck_identity_account_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "identity_password_credentials",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_table(
        "identity_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.String(length=64)),
        sa.ForeignKeyConstraint(
            ["account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_identity_sessions_account", "identity_sessions", ["account_id"])
    op.create_table(
        "identity_one_time_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('account_activation', 'password_recovery')",
            name="ck_identity_token_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'consumed', 'revoked')",
            name="ck_identity_token_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_identity_credential_account",
        "identity_one_time_credentials",
        ["account_id"],
    )
    op.create_index(
        "uq_identity_active_credential_per_purpose",
        "identity_one_time_credentials",
        ["account_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "identity_attempt_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("subject_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count >= 0", name="ck_identity_attempt_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purpose", "subject_hash", name="uq_identity_attempt_subject"
        ),
    )
    op.create_table(
        "identity_recovery_request_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_ciphertext", sa.LargeBinary()),
        sa.Column("email_nonce", sa.LargeBinary(length=12)),
        sa.Column("key_version", sa.Integer()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True)),
        sa.Column("processing_started_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_identity_recovery_job_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_identity_recovery_job_claim",
        "identity_recovery_request_jobs",
        ["status", "created_at"],
    )
    op.create_table(
        "security_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor_account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("target_account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("operator", sa.String(length=128)),
        sa.Column("reason", sa.String(length=256)),
        sa.Column("scope", sa.String(length=128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "mail_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recipient_account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True)),
        sa.Column("token_ciphertext", sa.LargeBinary()),
        sa.Column("token_nonce", sa.LargeBinary(length=12)),
        sa.Column("key_version", sa.Integer()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True)),
        sa.Column("smtp_started_at", sa.DateTime(timezone=True)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'accepted', 'failed', 'unknown', 'cancelled', 'suppressed')",
            name="ck_mail_outbox_status",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["identity_one_time_credentials.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["recipient_account_id"], ["identity_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id"),
    )
    op.create_index(
        "ix_mail_outbox_dispatch", "mail_outbox", ["status", "next_attempt_at"]
    )

    app_role = _app_role()
    app_tables = ", ".join(
        (
            "identity_accounts",
            "identity_password_credentials",
            "identity_sessions",
            "identity_one_time_credentials",
            "identity_attempt_records",
            "identity_recovery_request_jobs",
            "security_audits",
            "mail_outbox",
        )
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {app_role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {app_tables} TO {app_role}"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE alembic_version FROM {app_role}")
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def downgrade() -> None:
    op.drop_index("ix_mail_outbox_dispatch", table_name="mail_outbox")
    op.drop_table("mail_outbox")
    op.drop_table("security_audits")
    op.drop_index(
        "ix_identity_recovery_job_claim", table_name="identity_recovery_request_jobs"
    )
    op.drop_table("identity_recovery_request_jobs")
    op.drop_table("identity_attempt_records")
    op.drop_index(
        "uq_identity_active_credential_per_purpose",
        table_name="identity_one_time_credentials",
    )
    op.drop_index(
        "ix_identity_credential_account", table_name="identity_one_time_credentials"
    )
    op.drop_table("identity_one_time_credentials")
    op.drop_index("ix_identity_sessions_account", table_name="identity_sessions")
    op.drop_table("identity_sessions")
    op.drop_table("identity_password_credentials")
    op.drop_table("identity_accounts")
