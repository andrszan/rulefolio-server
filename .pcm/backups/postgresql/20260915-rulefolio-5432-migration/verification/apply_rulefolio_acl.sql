\set ON_ERROR_STOP on

ALTER SCHEMA public OWNER TO rulefolio_migrator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rulefolio_app;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM rulefolio_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  identity_accounts,
  identity_password_credentials,
  identity_sessions,
  identity_one_time_credentials,
  identity_attempt_records,
  identity_recovery_request_jobs,
  security_audits,
  mail_outbox,
  workspaces,
  workspace_members,
  workspace_invitations,
  workspace_invitation_attempts
TO rulefolio_app;

GRANT SELECT, INSERT ON TABLE works TO rulefolio_app;
GRANT UPDATE (
  name,
  description,
  creative_stage,
  target_experience,
  min_players,
  max_players,
  estimated_duration_minutes,
  revision,
  updated_at
) ON TABLE works TO rulefolio_app;

GRANT SELECT, INSERT, DELETE ON TABLE work_accesses TO rulefolio_app;
GRANT UPDATE (role) ON TABLE work_accesses TO rulefolio_app;

REVOKE ALL PRIVILEGES ON TABLE alembic_version FROM rulefolio_app;

GRANT EXECUTE ON FUNCTION public.workspace_actor_id() TO rulefolio_app;
GRANT EXECUTE ON FUNCTION public.workspace_invitation_credential_id() TO rulefolio_app;
GRANT EXECUTE ON FUNCTION public.workspace_management_id() TO rulefolio_app;
GRANT EXECUTE ON FUNCTION public.workspace_maintenance_id() TO rulefolio_app;
GRANT EXECUTE ON FUNCTION public.work_management_id() TO rulefolio_app;
GRANT EXECUTE ON FUNCTION public.work_management_workspace_id() TO rulefolio_app;
GRANT EXECUTE ON FUNCTION public.work_access_cleanup_workspace_id() TO rulefolio_app;
