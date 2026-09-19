"""将嵌套业务记录纳入工作空间退出 RLS 守卫。"""

from alembic import op
from app.core.config import settings

revision = "20260919_28"
down_revision = "20260919_27"
branch_labels = None
depends_on = None

_TABLES = (
    ("work_material_files", "work_material_files.work_id", "work"),
    ("playtest_session_materials", "playtest_session_materials.session_id", "session"),
    (
        "playtest_session_participants",
        "playtest_session_participants.session_id",
        "session",
    ),
    (
        "playtest_session_actual_materials",
        "playtest_session_actual_materials.session_id",
        "session",
    ),
    (
        "playtest_session_actual_participants",
        "playtest_session_actual_participants.session_id",
        "session",
    ),
    ("playtest_observations", "playtest_observations.session_id", "session"),
    ("playtest_feedback_items", "playtest_feedback_items.session_id", "session"),
    (
        "playtest_feedback_options",
        "playtest_feedback_options.item_id",
        "feedback_item",
    ),
    (
        "playtest_feedback_submissions",
        "playtest_feedback_submissions.session_id",
        "session",
    ),
    ("playtest_feedback_answers", "playtest_feedback_answers.session_id", "session"),
    ("issue_evidence_links", "issue_evidence_links.issue_id", "issue"),
    ("issue_retest_links", "issue_retest_links.issue_id", "issue"),
)

_GUARD_TABLES = (
    ("works", "workspace_exit_work_guard_read"),
    ("playtest_sessions", "workspace_exit_session_guard_read"),
    ("issues", "workspace_exit_issue_guard_read"),
    ("playtest_feedback_items", "workspace_exit_feedback_item_guard_read"),
)


def _role(name: str | None) -> str:
    if name is None:
        raise ValueError("迁移必须配置对应数据库身份")
    return op.get_bind().dialect.identifier_preparer.quote(name)


def _app_role() -> str:
    return _role(settings.db_user)


def _migrator_role() -> str:
    return _role(settings.migrator_db_user)


def _drop_exit_policies(table: str) -> None:
    for action in ("delete", "update", "insert", "read"):
        op.execute(f"DROP POLICY {table}_workspace_exit_{action} ON {table}")


def _create_exit_policies(table: str, identifier: str, kind: str) -> None:
    read = f"public.workspace_exit_allows_{kind}_read({identifier})"
    write = f"public.workspace_exit_allows_{kind}_write({identifier})"
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_read ON {table} AS RESTRICTIVE "
        f"FOR SELECT USING ({read})"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_insert ON {table} AS RESTRICTIVE "
        f"FOR INSERT WITH CHECK ({write})"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_update ON {table} AS RESTRICTIVE "
        f"FOR UPDATE USING ({write}) WITH CHECK ({write})"
    )
    op.execute(
        f"CREATE POLICY {table}_workspace_exit_delete ON {table} AS RESTRICTIVE "
        f"FOR DELETE USING ({write})"
    )


def _create_guard_function(kind: str, query: str) -> None:
    for action in ("read", "write"):
        op.execute(
            f"""
            CREATE FUNCTION public.workspace_exit_allows_{kind}_{action}(p_id uuid)
            RETURNS boolean
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog, public AS $$
                {query.format(action=action)}
            $$
            """
        )


def upgrade() -> None:
    app_role = _app_role()
    migrator_role = _migrator_role()
    for table, policy in _GUARD_TABLES:
        op.execute(
            f"CREATE POLICY {policy} ON {table} FOR SELECT TO {migrator_role} USING (true)"
        )
    _create_guard_function(
        "work",
        "SELECT public.workspace_exit_allows_{action}(work.workspace_id) "
        "FROM public.works work WHERE work.id = p_id",
    )
    _create_guard_function(
        "session",
        "SELECT public.workspace_exit_allows_{action}(session.workspace_id) "
        "FROM public.playtest_sessions session WHERE session.id = p_id",
    )
    _create_guard_function(
        "issue",
        "SELECT public.workspace_exit_allows_{action}(issue.workspace_id) "
        "FROM public.issues issue WHERE issue.id = p_id",
    )
    _create_guard_function(
        "feedback_item",
        "SELECT public.workspace_exit_allows_{action}(session.workspace_id) "
        "FROM public.playtest_feedback_items item "
        "JOIN public.playtest_sessions session ON session.id = item.session_id "
        "WHERE item.id = p_id",
    )
    for kind in ("work", "session", "issue", "feedback_item"):
        for action in ("read", "write"):
            signature = f"workspace_exit_allows_{kind}_{action}(uuid)"
            op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {app_role}")
    for table, identifier, kind in _TABLES:
        _create_exit_policies(table, identifier, kind)


def downgrade() -> None:
    for table, _, _ in _TABLES:
        _drop_exit_policies(table)
    for kind in ("feedback_item", "issue", "session", "work"):
        for action in ("write", "read"):
            op.execute(
                f"DROP FUNCTION public.workspace_exit_allows_{kind}_{action}(uuid)"
            )
    for table, policy in _GUARD_TABLES:
        op.execute(f"DROP POLICY {policy} ON {table}")
