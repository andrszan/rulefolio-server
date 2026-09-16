"""将试玩概览改为受限投影函数。"""

from alembic import op
from app.core.config import settings

revision = "20260916_23"
down_revision = "20260916_22"
branch_labels = None
depends_on = None


def _role(name: str | None) -> str:
    if name is None:
        raise ValueError("迁移必须配置对应数据库身份")
    return op.get_bind().dialect.identifier_preparer.quote(name)


def _app_role() -> str:
    return _role(settings.db_user)


def _migrator_role() -> str:
    return _role(settings.migrator_db_user)


def _create_overview_functions(app_role: str) -> None:
    op.execute(
        """
        CREATE FUNCTION public.playtest_overview_summary(
            p_workspace_id uuid,
            p_work_id uuid,
            p_scheduled_from timestamptz,
            p_scheduled_before timestamptz,
            p_actual_headcount_min integer,
            p_actual_headcount_max integer,
            p_actual_play_mode text
        ) RETURNS jsonb
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            WITH authorized AS (
                SELECT EXISTS (
                    SELECT 1
                    FROM public.workspace_members member
                    JOIN public.work_accesses access
                      ON access.workspace_id = member.workspace_id
                     AND access.account_id = member.account_id
                    WHERE member.workspace_id = p_workspace_id
                      AND member.account_id = public.workspace_actor_id()
                      AND access.work_id = p_work_id
                ) AS allowed
            ), scoped_sessions AS (
                SELECT
                    session.id,
                    session.actual_headcount,
                    session.actual_duration_minutes,
                    session.completion_status,
                    session.actual_play_mode,
                    (
                        NULLIF(BTRIM(session.actual_material_change_reason), '') IS NOT NULL
                        OR EXISTS (
                            SELECT 1
                            FROM public.playtest_observations observation
                            WHERE observation.session_id = session.id
                              AND observation.kind = 'temporary_variant'
                        )
                    ) AS has_temporary_variant
                FROM public.playtest_sessions session
                WHERE (SELECT allowed FROM authorized)
                  AND session.workspace_id = p_workspace_id
                  AND session.work_id = p_work_id
                  AND session.status = 'started'
                  AND (p_scheduled_from IS NULL OR session.scheduled_at >= p_scheduled_from)
                  AND (p_scheduled_before IS NULL OR session.scheduled_at < p_scheduled_before)
                  AND (
                      p_actual_headcount_min IS NULL
                      OR session.actual_headcount >= p_actual_headcount_min
                  )
                  AND (
                      p_actual_headcount_max IS NULL
                      OR session.actual_headcount <= p_actual_headcount_max
                  )
                  AND (
                      p_actual_play_mode IS NULL
                      OR session.actual_play_mode = p_actual_play_mode
                  )
            ), coverage AS (
                SELECT
                    actual_headcount,
                    COUNT(*) FILTER (WHERE completion_status = 'completed') AS completed_count,
                    COUNT(*) FILTER (WHERE completion_status = 'interrupted') AS interrupted_count,
                    COUNT(*) FILTER (WHERE completion_status IS NULL) AS unrecorded_completion_count,
                    COUNT(*) FILTER (WHERE has_temporary_variant) AS temporary_variant_count
                FROM scoped_sessions
                WHERE actual_headcount IS NOT NULL
                GROUP BY actual_headcount
            ), play_modes AS (
                SELECT DISTINCT session.actual_play_mode AS actual_play_mode
                FROM public.playtest_sessions session
                WHERE (SELECT allowed FROM authorized)
                  AND session.workspace_id = p_workspace_id
                  AND session.work_id = p_work_id
                  AND session.status = 'started'
                  AND session.actual_play_mode IS NOT NULL
            )
            SELECT jsonb_build_object(
                'included_session_count', COUNT(*),
                'missing_actual_headcount_count', COUNT(*) FILTER (WHERE actual_headcount IS NULL),
                'missing_actual_duration_count', COUNT(*) FILTER (WHERE actual_duration_minutes IS NULL),
                'missing_completion_status_count', COUNT(*) FILTER (WHERE completion_status IS NULL),
                'missing_actual_play_mode_count', COUNT(*) FILTER (WHERE actual_play_mode IS NULL),
                'temporary_variant_count', COUNT(*) FILTER (WHERE has_temporary_variant),
                'headcount_coverage', COALESCE(
                    (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'actual_headcount', actual_headcount,
                                'completed_count', completed_count,
                                'interrupted_count', interrupted_count,
                                'unrecorded_completion_count', unrecorded_completion_count,
                                'temporary_variant_count', temporary_variant_count
                            )
                            ORDER BY actual_headcount
                        )
                        FROM coverage
                    ),
                    '[]'::jsonb
                ),
                'play_modes', COALESCE(
                    (SELECT jsonb_agg(actual_play_mode ORDER BY actual_play_mode) FROM play_modes),
                    '[]'::jsonb
                )
            )
            FROM scoped_sessions
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.playtest_overview_sessions(
            p_workspace_id uuid,
            p_work_id uuid,
            p_scheduled_from timestamptz,
            p_scheduled_before timestamptz,
            p_actual_headcount_min integer,
            p_actual_headcount_max integer,
            p_actual_play_mode text,
            p_page integer,
            p_size integer
        ) RETURNS TABLE(
            total bigint,
            id uuid,
            scheduled_at timestamptz,
            actual_headcount integer,
            actual_duration_minutes integer,
            completion_status varchar,
            actual_play_mode varchar,
            has_temporary_variant boolean
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            WITH authorized AS (
                SELECT EXISTS (
                    SELECT 1
                    FROM public.workspace_members member
                    JOIN public.work_accesses access
                      ON access.workspace_id = member.workspace_id
                     AND access.account_id = member.account_id
                    WHERE member.workspace_id = p_workspace_id
                      AND member.account_id = public.workspace_actor_id()
                      AND access.work_id = p_work_id
                ) AS allowed
            ), scoped_sessions AS (
                SELECT
                    session.id,
                    session.scheduled_at,
                    session.actual_headcount,
                    session.actual_duration_minutes,
                    session.completion_status,
                    session.actual_play_mode,
                    (
                        NULLIF(BTRIM(session.actual_material_change_reason), '') IS NOT NULL
                        OR EXISTS (
                            SELECT 1
                            FROM public.playtest_observations observation
                            WHERE observation.session_id = session.id
                              AND observation.kind = 'temporary_variant'
                        )
                    ) AS has_temporary_variant
                FROM public.playtest_sessions session
                WHERE (SELECT allowed FROM authorized)
                  AND session.workspace_id = p_workspace_id
                  AND session.work_id = p_work_id
                  AND session.status = 'started'
                  AND (p_scheduled_from IS NULL OR session.scheduled_at >= p_scheduled_from)
                  AND (p_scheduled_before IS NULL OR session.scheduled_at < p_scheduled_before)
                  AND (
                      p_actual_headcount_min IS NULL
                      OR session.actual_headcount >= p_actual_headcount_min
                  )
                  AND (
                      p_actual_headcount_max IS NULL
                      OR session.actual_headcount <= p_actual_headcount_max
                  )
                  AND (
                      p_actual_play_mode IS NULL
                      OR session.actual_play_mode = p_actual_play_mode
                  )
            )
            SELECT
                COUNT(*) OVER () AS total,
                id,
                scheduled_at,
                actual_headcount,
                actual_duration_minutes,
                completion_status,
                actual_play_mode,
                has_temporary_variant
            FROM scoped_sessions
            ORDER BY scheduled_at DESC, id DESC
            OFFSET (p_page - 1) * p_size
            LIMIT p_size
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.playtest_overview_issues(
            p_workspace_id uuid,
            p_work_id uuid,
            p_kind text,
            p_page integer,
            p_size integer
        ) RETURNS TABLE(
            total bigint,
            id uuid,
            description text,
            decision varchar,
            status varchar,
            verification_status text,
            current_conclusion_type varchar,
            current_conclusion_session_id uuid
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            WITH authorized AS (
                SELECT EXISTS (
                    SELECT 1
                    FROM public.workspace_members member
                    JOIN public.work_accesses access
                      ON access.workspace_id = member.workspace_id
                     AND access.account_id = member.account_id
                    WHERE member.workspace_id = p_workspace_id
                      AND member.account_id = public.workspace_actor_id()
                      AND access.work_id = p_work_id
                ) AS allowed
            ), scoped_issues AS (
                SELECT
                    issue.id,
                    issue.description,
                    issue.decision,
                    issue.status,
                    issue.adjustment_note,
                    issue.updated_at,
                    link.conclusion AS current_conclusion_type,
                    link.session_id AS current_conclusion_session_id
                FROM public.issues issue
                LEFT JOIN public.issue_retest_links link
                  ON link.issue_id = issue.id
                 AND link.adjustment_generation = issue.adjustment_generation
                 AND link.conclusion IS NOT NULL
                WHERE (SELECT allowed FROM authorized)
                  AND issue.workspace_id = p_workspace_id
                  AND issue.work_id = p_work_id
                  AND issue.status = 'open'
            ), filtered_issues AS (
                SELECT *,
                    CASE
                        WHEN current_conclusion_type IS NOT NULL THEN current_conclusion_type::text
                        WHEN adjustment_note IS NOT NULL THEN 'pending'
                        ELSE 'not_recorded'
                    END AS verification_status
                FROM scoped_issues
                WHERE (
                    p_kind = 'pending-retest'
                    AND adjustment_note IS NOT NULL
                    AND current_conclusion_type IS NULL
                ) OR (
                    p_kind = 'needs-action'
                    AND (
                        adjustment_note IS NULL
                        OR current_conclusion_type IS NOT NULL
                    )
                )
            )
            SELECT
                COUNT(*) OVER () AS total,
                id,
                description,
                decision,
                status,
                verification_status,
                current_conclusion_type,
                current_conclusion_session_id
            FROM filtered_issues
            ORDER BY updated_at DESC, id DESC
            OFFSET (p_page - 1) * p_size
            LIMIT p_size
        $$
        """
    )
    for signature in (
        "playtest_overview_summary(uuid, uuid, timestamptz, timestamptz, integer, integer, text)",
        "playtest_overview_sessions(uuid, uuid, timestamptz, timestamptz, integer, integer, text, integer, integer)",
        "playtest_overview_issues(uuid, uuid, text, integer, integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {app_role}")


def _drop_overview_functions() -> None:
    for signature in (
        "playtest_overview_issues(uuid, uuid, text, integer, integer)",
        "playtest_overview_sessions(uuid, uuid, timestamptz, timestamptz, integer, integer, text, integer, integer)",
        "playtest_overview_summary(uuid, uuid, timestamptz, timestamptz, integer, integer, text)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{signature}")


def _create_legacy_overview_scope(app_role: str) -> None:
    op.execute(
        """
        CREATE FUNCTION public.playtest_overview_workspace_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(
                current_setting('app.playtest_overview_workspace_id', true), ''
            )::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.playtest_overview_work_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(
                current_setting('app.playtest_overview_work_id', true), ''
            )::uuid
        $$
        """
    )
    for function in (
        "playtest_overview_workspace_id",
        "playtest_overview_work_id",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function}() TO {app_role}")

    overview_scope = (
        "workspace_id = public.playtest_overview_workspace_id() "
        "AND work_id = public.playtest_overview_work_id()"
    )
    op.execute(
        "CREATE POLICY playtest_session_overview_read ON playtest_sessions FOR SELECT "
        f"USING ({overview_scope})"
    )
    op.execute(
        """
        CREATE POLICY playtest_observation_overview_read
        ON playtest_observations FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM playtest_sessions session
                WHERE session.id = playtest_observations.session_id
                  AND session.workspace_id = public.playtest_overview_workspace_id()
                  AND session.work_id = public.playtest_overview_work_id()
            )
        )
        """
    )
    op.execute(
        "CREATE POLICY issue_overview_read ON issues FOR SELECT "
        f"USING ({overview_scope})"
    )
    op.execute(
        """
        CREATE POLICY issue_retest_link_overview_read
        ON issue_retest_links FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM issues issue
                WHERE issue.id = issue_retest_links.issue_id
                  AND issue.workspace_id = public.playtest_overview_workspace_id()
                  AND issue.work_id = public.playtest_overview_work_id()
            )
        )
        """
    )


def upgrade() -> None:
    app_role = _app_role()
    migrator_role = _migrator_role()
    _drop_overview_functions()
    for policy, table in (
        ("issue_retest_link_overview_read", "issue_retest_links"),
        ("issue_overview_read", "issues"),
        ("playtest_observation_overview_read", "playtest_observations"),
        ("playtest_session_overview_read", "playtest_sessions"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute("DROP FUNCTION public.playtest_overview_work_id()")
    op.execute("DROP FUNCTION public.playtest_overview_workspace_id()")

    for policy, table in (
        ("playtest_overview_function_session_read", "playtest_sessions"),
        ("playtest_overview_function_observation_read", "playtest_observations"),
        ("playtest_overview_function_issue_read", "issues"),
        ("playtest_overview_function_retest_read", "issue_retest_links"),
        ("playtest_overview_function_member_read", "workspace_members"),
        ("playtest_overview_function_access_read", "work_accesses"),
    ):
        op.execute(
            f"CREATE POLICY {policy} ON {table} FOR SELECT TO {migrator_role} USING (true)"
        )
    _create_overview_functions(app_role)


def downgrade() -> None:
    app_role = _app_role()
    _drop_overview_functions()
    for policy, table in (
        ("playtest_overview_function_access_read", "work_accesses"),
        ("playtest_overview_function_member_read", "workspace_members"),
        ("playtest_overview_function_retest_read", "issue_retest_links"),
        ("playtest_overview_function_issue_read", "issues"),
        ("playtest_overview_function_observation_read", "playtest_observations"),
        ("playtest_overview_function_session_read", "playtest_sessions"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    _create_legacy_overview_scope(app_role)
