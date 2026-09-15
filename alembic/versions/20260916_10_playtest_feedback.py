"""增加场次反馈题与当前反馈。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260916_10"
down_revision = "20260915_09"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    app_role = _app_role()
    op.create_table(
        "playtest_feedback_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("creation_operation_key", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('short_text', 'single_choice', 'number')",
            name="ck_playtest_feedback_item_kind",
        ),
        sa.CheckConstraint(
            "btrim(question) <> ''", name="ck_playtest_feedback_item_question"
        ),
        sa.CheckConstraint("revision > 0", name="ck_playtest_feedback_item_revision"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["playtest_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "session_id", name="uq_playtest_feedback_item_scope"),
        sa.UniqueConstraint(
            "session_id",
            "creation_operation_key",
            name="uq_playtest_feedback_item_operation",
        ),
    )
    op.alter_column("playtest_feedback_items", "revision", server_default=None)
    op.alter_column("playtest_feedback_items", "is_locked", server_default=None)
    op.create_index(
        "ix_playtest_feedback_items_session_created",
        "playtest_feedback_items",
        ["session_id", "created_at", "id"],
    )
    op.create_table(
        "playtest_feedback_options",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "btrim(label) <> ''", name="ck_playtest_feedback_option_label"
        ),
        sa.CheckConstraint("position > 0", name="ck_playtest_feedback_option_position"),
        sa.ForeignKeyConstraint(
            ["item_id", "session_id"],
            ["playtest_feedback_items.id", "playtest_feedback_items.session_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "item_id", "session_id", name="uq_playtest_feedback_option_scope"
        ),
        sa.UniqueConstraint(
            "item_id", "position", name="uq_playtest_feedback_option_position"
        ),
    )
    op.create_index(
        "ix_playtest_feedback_options_item_position",
        "playtest_feedback_options",
        ["item_id", "position", "id"],
    )
    op.create_table(
        "playtest_feedback_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("direct_author_account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("temporary_alias", sa.String(length=160)),
        sa.Column(
            "recorded_by_account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("creation_operation_key", sa.String(length=128), nullable=False),
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
        sa.CheckConstraint(
            "source IN ('direct', 'oral_discussion', 'paper_record', 'organizer_observation', 'temporary_alias')",
            name="ck_playtest_feedback_submission_source",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted')",
            name="ck_playtest_feedback_submission_status",
        ),
        sa.CheckConstraint(
            "revision > 0", name="ck_playtest_feedback_submission_revision"
        ),
        sa.CheckConstraint(
            "(source = 'direct' AND direct_author_account_id IS NOT NULL AND recorded_by_account_id = direct_author_account_id) "
            "OR (source <> 'direct' AND direct_author_account_id IS NULL AND status = 'submitted')",
            name="ck_playtest_feedback_submission_author",
        ),
        sa.CheckConstraint(
            "(source = 'temporary_alias' AND btrim(temporary_alias) <> '') "
            "OR (source <> 'temporary_alias' AND temporary_alias IS NULL)",
            name="ck_playtest_feedback_submission_alias",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["playtest_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["direct_author_account_id"], ["identity_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_account_id"], ["identity_accounts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "session_id", name="uq_playtest_feedback_submission_scope"
        ),
        sa.UniqueConstraint(
            "session_id",
            "direct_author_account_id",
            name="uq_playtest_feedback_submission_direct_author",
        ),
        sa.UniqueConstraint(
            "session_id",
            "creation_operation_key",
            name="uq_playtest_feedback_submission_operation",
        ),
    )
    op.alter_column("playtest_feedback_submissions", "revision", server_default=None)
    op.create_index(
        "ix_playtest_feedback_submissions_session_updated",
        "playtest_feedback_submissions",
        ["session_id", "updated_at", "id"],
    )
    op.create_table(
        "playtest_feedback_answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text_value", sa.Text()),
        sa.Column("option_id", postgresql.UUID(as_uuid=True)),
        sa.Column("number_value", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("direct_author_account_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "recorded_by_account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "kind IN ('short_text', 'single_choice', 'number')",
            name="ck_playtest_feedback_answer_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'short_text' AND btrim(text_value) <> '' AND option_id IS NULL AND number_value IS NULL) "
            "OR (kind = 'single_choice' AND text_value IS NULL AND option_id IS NOT NULL AND number_value IS NULL) "
            "OR (kind = 'number' AND text_value IS NULL AND option_id IS NULL AND number_value IS NOT NULL "
            "AND number_value NOT IN ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision))",
            name="ck_playtest_feedback_answer_value",
        ),
        sa.CheckConstraint(
            "source IN ('direct', 'oral_discussion', 'paper_record', 'organizer_observation', 'temporary_alias')",
            name="ck_playtest_feedback_answer_source",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted')",
            name="ck_playtest_feedback_answer_status",
        ),
        sa.CheckConstraint(
            "(source = 'direct' AND direct_author_account_id IS NOT NULL AND recorded_by_account_id = direct_author_account_id) "
            "OR (source <> 'direct' AND direct_author_account_id IS NULL AND status = 'submitted')",
            name="ck_playtest_feedback_answer_author",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id", "session_id"],
            [
                "playtest_feedback_submissions.id",
                "playtest_feedback_submissions.session_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id", "session_id"],
            ["playtest_feedback_items.id", "playtest_feedback_items.session_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["option_id", "item_id", "session_id"],
            [
                "playtest_feedback_options.id",
                "playtest_feedback_options.item_id",
                "playtest_feedback_options.session_id",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["direct_author_account_id"], ["identity_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_account_id"], ["identity_accounts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id", "item_id", name="uq_playtest_feedback_answer_item"
        ),
    )
    op.create_index(
        "ix_playtest_feedback_answers_session_item",
        "playtest_feedback_answers",
        ["session_id", "item_id", "id"],
    )

    for table in (
        "playtest_feedback_items",
        "playtest_feedback_options",
        "playtest_feedback_submissions",
        "playtest_feedback_answers",
    ):
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {app_role}"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE FUNCTION public.feedback_management_session_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.feedback_management_session_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.feedback_participant_session_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.feedback_participant_session_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.feedback_answer_item_lock_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.feedback_answer_item_lock_id', true), '')::uuid
        $$
        """
    )
    for function in (
        "feedback_management_session_id",
        "feedback_participant_session_id",
        "feedback_answer_item_lock_id",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function}() TO {app_role}")
    op.execute(
        """
        CREATE FUNCTION public.lock_playtest_feedback_item() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE playtest_feedback_items
            SET is_locked = true
            WHERE id = NEW.item_id AND session_id = NEW.session_id;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER playtest_feedback_answer_lock_item "
        "AFTER INSERT ON playtest_feedback_answers "
        "FOR EACH ROW EXECUTE FUNCTION public.lock_playtest_feedback_item()"
    )

    manager = "session_id = public.feedback_management_session_id()"
    participant = "session_id = public.feedback_participant_session_id()"
    actor = "public.workspace_actor_id()"
    op.execute(
        "CREATE POLICY playtest_feedback_item_read ON playtest_feedback_items FOR SELECT "
        f"USING ({manager} OR {participant})"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_item_insert ON playtest_feedback_items FOR INSERT "
        f"WITH CHECK ({manager})"
    )
    lock_item = "id = public.feedback_answer_item_lock_id()"
    op.execute(
        "CREATE POLICY playtest_feedback_item_update ON playtest_feedback_items FOR UPDATE "
        f"USING (({manager} AND NOT is_locked) OR {lock_item}) "
        f"WITH CHECK (({manager} AND NOT is_locked) OR ({lock_item} AND is_locked))"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_item_delete ON playtest_feedback_items FOR DELETE "
        f"USING ({manager} AND NOT is_locked)"
    )
    for table, prefix in (("playtest_feedback_options", "playtest_feedback_option"),):
        op.execute(
            f"CREATE POLICY {prefix}_read ON {table} FOR SELECT USING ({manager} OR {participant})"
        )
        op.execute(
            f"CREATE POLICY {prefix}_insert ON {table} FOR INSERT WITH CHECK ({manager})"
        )
        op.execute(
            f"CREATE POLICY {prefix}_update ON {table} FOR UPDATE USING ({manager}) WITH CHECK ({manager})"
        )
        op.execute(
            f"CREATE POLICY {prefix}_delete ON {table} FOR DELETE USING ({manager})"
        )

    manager_submission = f"({manager} AND status = 'submitted')"
    participant_submission = (
        f"({participant} AND source = 'direct' AND direct_author_account_id = {actor})"
    )
    manager_submission_write = (
        f"({manager} AND source <> 'direct' AND status = 'submitted' "
        f"AND direct_author_account_id IS NULL AND recorded_by_account_id = {actor})"
    )
    participant_submission_write = (
        f"({participant} AND source = 'direct' AND direct_author_account_id = {actor} "
        f"AND recorded_by_account_id = {actor})"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_submission_read ON playtest_feedback_submissions FOR SELECT "
        f"USING ({manager_submission} OR {participant_submission})"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_submission_insert ON playtest_feedback_submissions FOR INSERT "
        f"WITH CHECK ({manager_submission_write} OR {participant_submission_write})"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_submission_update ON playtest_feedback_submissions FOR UPDATE "
        f"USING ({manager_submission_write} OR {participant_submission_write}) "
        f"WITH CHECK ({manager_submission_write} OR {participant_submission_write})"
    )

    manager_answer = f"({manager} AND status = 'submitted')"
    participant_answer = (
        f"({participant} AND source = 'direct' AND direct_author_account_id = {actor})"
    )
    manager_answer_write = (
        f"({manager} AND source <> 'direct' AND status = 'submitted' "
        f"AND direct_author_account_id IS NULL AND recorded_by_account_id = {actor})"
    )
    participant_answer_write = (
        f"({participant} AND source = 'direct' AND direct_author_account_id = {actor} "
        f"AND recorded_by_account_id = {actor})"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_answer_read ON playtest_feedback_answers FOR SELECT "
        f"USING ({manager_answer} OR {participant_answer})"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_answer_insert ON playtest_feedback_answers FOR INSERT "
        f"WITH CHECK ({manager_answer_write} OR {participant_answer_write})"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_answer_update ON playtest_feedback_answers FOR UPDATE "
        f"USING ({manager_answer_write} OR {participant_answer_write}) "
        f"WITH CHECK ({manager_answer_write} OR {participant_answer_write})"
    )
    op.execute(
        "CREATE POLICY playtest_feedback_answer_delete ON playtest_feedback_answers FOR DELETE "
        f"USING ({manager_answer_write} OR {participant_answer_write})"
    )

    for table, prefix in (
        ("playtest_feedback_items", "item"),
        ("playtest_feedback_options", "option"),
        ("playtest_feedback_submissions", "submission"),
        ("playtest_feedback_answers", "answer"),
    ):
        op.execute(
            f"CREATE POLICY project_baseline_playtest_feedback_{prefix}_read ON {table} FOR SELECT "
            "USING (public.project_baseline_maintenance())"
        )
        op.execute(
            f"CREATE POLICY project_baseline_playtest_feedback_{prefix}_delete ON {table} FOR DELETE "
            "USING (public.project_baseline_maintenance())"
        )


def downgrade() -> None:
    for table, prefix in (
        ("playtest_feedback_answers", "answer"),
        ("playtest_feedback_submissions", "submission"),
        ("playtest_feedback_options", "option"),
        ("playtest_feedback_items", "item"),
    ):
        for suffix in ("delete", "read"):
            op.execute(
                f"DROP POLICY IF EXISTS project_baseline_playtest_feedback_{prefix}_{suffix} ON {table}"
            )

    for policy in (
        "playtest_feedback_answer_delete",
        "playtest_feedback_answer_update",
        "playtest_feedback_answer_insert",
        "playtest_feedback_answer_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON playtest_feedback_answers")
    for policy in (
        "playtest_feedback_submission_update",
        "playtest_feedback_submission_insert",
        "playtest_feedback_submission_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON playtest_feedback_submissions")
    for table, prefix in (
        ("playtest_feedback_options", "playtest_feedback_option"),
        ("playtest_feedback_items", "playtest_feedback_item"),
    ):
        for suffix in ("delete", "update", "insert", "read"):
            op.execute(f"DROP POLICY IF EXISTS {prefix}_{suffix} ON {table}")

    op.execute(
        "DROP TRIGGER playtest_feedback_answer_lock_item ON playtest_feedback_answers"
    )
    op.execute("DROP FUNCTION public.lock_playtest_feedback_item()")
    op.execute("DROP FUNCTION public.feedback_answer_item_lock_id()")
    op.execute("DROP FUNCTION public.feedback_participant_session_id()")
    op.execute("DROP FUNCTION public.feedback_management_session_id()")
    op.drop_index(
        "ix_playtest_feedback_answers_session_item",
        table_name="playtest_feedback_answers",
    )
    op.drop_table("playtest_feedback_answers")
    op.drop_index(
        "ix_playtest_feedback_submissions_session_updated",
        table_name="playtest_feedback_submissions",
    )
    op.drop_table("playtest_feedback_submissions")
    op.drop_index(
        "ix_playtest_feedback_options_item_position",
        table_name="playtest_feedback_options",
    )
    op.drop_table("playtest_feedback_options")
    op.drop_index(
        "ix_playtest_feedback_items_session_created",
        table_name="playtest_feedback_items",
    )
    op.drop_table("playtest_feedback_items")
