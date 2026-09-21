--
-- PostgreSQL database dump
--

-- Dumped from database version 14.15 (Homebrew)
-- Dumped by pg_dump version 14.15 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: work_access_cleanup_workspace_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.work_access_cleanup_workspace_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
            SELECT NULLIF(current_setting('app.work_access_cleanup_workspace_id', true), '')::uuid
        $$;


--
-- Name: work_management_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.work_management_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
            SELECT NULLIF(current_setting('app.work_management_id', true), '')::uuid
        $$;


--
-- Name: work_management_workspace_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.work_management_workspace_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
            SELECT NULLIF(current_setting('app.work_management_workspace_id', true), '')::uuid
        $$;


--
-- Name: workspace_actor_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.workspace_actor_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
            SELECT NULLIF(current_setting('app.actor_id', true), '')::uuid
        $$;


--
-- Name: workspace_invitation_credential_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.workspace_invitation_credential_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
            SELECT NULLIF(current_setting('app.invitation_credential_id', true), '')::uuid
        $$;


--
-- Name: workspace_maintenance_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.workspace_maintenance_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
            SELECT NULLIF(current_setting('app.maintenance_workspace_id', true), '')::uuid
        $$;


--
-- Name: workspace_management_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.workspace_management_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
            SELECT NULLIF(current_setting('app.workspace_management_id', true), '')::uuid
        $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: identity_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity_accounts (
    id uuid NOT NULL,
    email character varying(320) NOT NULL,
    status character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_identity_account_status CHECK (((status)::text = ANY (ARRAY[('pending_activation'::character varying)::text, ('active'::character varying)::text, ('disabled'::character varying)::text])))
);


--
-- Name: identity_attempt_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity_attempt_records (
    id uuid NOT NULL,
    purpose character varying(64) NOT NULL,
    subject_hash bytea NOT NULL,
    window_started_at timestamp with time zone NOT NULL,
    count integer NOT NULL,
    CONSTRAINT ck_identity_attempt_count CHECK ((count >= 0))
);


--
-- Name: identity_one_time_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity_one_time_credentials (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    purpose character varying(32) NOT NULL,
    token_hash bytea NOT NULL,
    status character varying(16) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_identity_token_purpose CHECK (((purpose)::text = ANY (ARRAY[('account_activation'::character varying)::text, ('password_recovery'::character varying)::text, ('workspace_invitation'::character varying)::text]))),
    CONSTRAINT ck_identity_token_status CHECK (((status)::text = ANY (ARRAY[('active'::character varying)::text, ('consumed'::character varying)::text, ('revoked'::character varying)::text])))
);


--
-- Name: identity_password_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity_password_credentials (
    account_id uuid NOT NULL,
    password_hash text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: identity_recovery_request_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity_recovery_request_jobs (
    id uuid NOT NULL,
    email_ciphertext bytea,
    email_nonce bytea,
    key_version integer,
    status character varying(16) NOT NULL,
    claim_id uuid,
    processing_started_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ck_identity_recovery_job_status CHECK (((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('processing'::character varying)::text, ('completed'::character varying)::text, ('failed'::character varying)::text])))
);


--
-- Name: identity_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity_sessions (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    token_hash bytea NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    revoke_reason character varying(64)
);


--
-- Name: mail_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mail_outbox (
    id uuid NOT NULL,
    credential_id uuid NOT NULL,
    recipient_account_id uuid NOT NULL,
    purpose character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    claim_id uuid,
    token_ciphertext bytea,
    token_nonce bytea,
    key_version integer,
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    dispatch_started_at timestamp with time zone,
    smtp_started_at timestamp with time zone,
    accepted_at timestamp with time zone,
    last_error_code character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    workspace_name character varying(160),
    CONSTRAINT ck_mail_outbox_status CHECK (((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('sending'::character varying)::text, ('accepted'::character varying)::text, ('failed'::character varying)::text, ('unknown'::character varying)::text, ('cancelled'::character varying)::text, ('suppressed'::character varying)::text])))
);


--
-- Name: security_audits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_audits (
    id uuid NOT NULL,
    action character varying(64) NOT NULL,
    actor_account_id uuid,
    target_account_id uuid,
    operator character varying(128),
    reason character varying(256),
    scope character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: work_accesses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.work_accesses (
    work_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    account_id uuid NOT NULL,
    role character varying(16) NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_work_access_role CHECK (((role)::text = ANY (ARRAY[('maintainer'::character varying)::text, ('organizer'::character varying)::text, ('collaborator'::character varying)::text])))
);

ALTER TABLE ONLY public.work_accesses FORCE ROW LEVEL SECURITY;


--
-- Name: works; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.works (
    id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    description text NOT NULL,
    creative_stage character varying(160) NOT NULL,
    target_experience text NOT NULL,
    min_players integer NOT NULL,
    max_players integer NOT NULL,
    estimated_duration_minutes integer NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_work_estimated_duration_positive CHECK ((estimated_duration_minutes > 0)),
    CONSTRAINT ck_work_min_players_positive CHECK ((min_players > 0)),
    CONSTRAINT ck_work_player_range CHECK ((max_players >= min_players)),
    CONSTRAINT ck_work_revision_positive CHECK ((revision > 0))
);

ALTER TABLE ONLY public.works FORCE ROW LEVEL SECURITY;


--
-- Name: workspace_invitation_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_invitation_attempts (
    id uuid NOT NULL,
    purpose character varying(64) NOT NULL,
    subject_hash bytea NOT NULL,
    window_started_at timestamp with time zone NOT NULL,
    count integer NOT NULL,
    CONSTRAINT ck_workspace_invitation_attempt_count CHECK ((count >= 0))
);

ALTER TABLE ONLY public.workspace_invitation_attempts FORCE ROW LEVEL SECURITY;


--
-- Name: workspace_invitations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_invitations (
    id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    account_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    status character varying(16) NOT NULL,
    accepted_at timestamp with time zone,
    revoked_at timestamp with time zone,
    accepted_operation_key character varying(128),
    accepted_operation character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_workspace_invitation_status CHECK (((status)::text = ANY (ARRAY[('active'::character varying)::text, ('accepted'::character varying)::text, ('revoked'::character varying)::text, ('expired'::character varying)::text])))
);

ALTER TABLE ONLY public.workspace_invitations FORCE ROW LEVEL SECURITY;


--
-- Name: workspace_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_members (
    id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    account_id uuid NOT NULL,
    joined_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.workspace_members FORCE ROW LEVEL SECURITY;


--
-- Name: workspaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspaces (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    description text,
    owner_account_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.workspaces FORCE ROW LEVEL SECURITY;


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: identity_accounts identity_accounts_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_accounts
    ADD CONSTRAINT identity_accounts_email_key UNIQUE (email);


--
-- Name: identity_accounts identity_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_accounts
    ADD CONSTRAINT identity_accounts_pkey PRIMARY KEY (id);


--
-- Name: identity_attempt_records identity_attempt_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_attempt_records
    ADD CONSTRAINT identity_attempt_records_pkey PRIMARY KEY (id);


--
-- Name: identity_one_time_credentials identity_one_time_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_one_time_credentials
    ADD CONSTRAINT identity_one_time_credentials_pkey PRIMARY KEY (id);


--
-- Name: identity_one_time_credentials identity_one_time_credentials_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_one_time_credentials
    ADD CONSTRAINT identity_one_time_credentials_token_hash_key UNIQUE (token_hash);


--
-- Name: identity_password_credentials identity_password_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_password_credentials
    ADD CONSTRAINT identity_password_credentials_pkey PRIMARY KEY (account_id);


--
-- Name: identity_recovery_request_jobs identity_recovery_request_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_recovery_request_jobs
    ADD CONSTRAINT identity_recovery_request_jobs_pkey PRIMARY KEY (id);


--
-- Name: identity_sessions identity_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_sessions
    ADD CONSTRAINT identity_sessions_pkey PRIMARY KEY (id);


--
-- Name: identity_sessions identity_sessions_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_sessions
    ADD CONSTRAINT identity_sessions_token_hash_key UNIQUE (token_hash);


--
-- Name: mail_outbox mail_outbox_credential_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mail_outbox
    ADD CONSTRAINT mail_outbox_credential_id_key UNIQUE (credential_id);


--
-- Name: mail_outbox mail_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mail_outbox
    ADD CONSTRAINT mail_outbox_pkey PRIMARY KEY (id);


--
-- Name: security_audits security_audits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_audits
    ADD CONSTRAINT security_audits_pkey PRIMARY KEY (id);


--
-- Name: identity_attempt_records uq_identity_attempt_subject; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_attempt_records
    ADD CONSTRAINT uq_identity_attempt_subject UNIQUE (purpose, subject_hash);


--
-- Name: works uq_work_id_workspace; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.works
    ADD CONSTRAINT uq_work_id_workspace UNIQUE (id, workspace_id);


--
-- Name: workspace_invitation_attempts uq_workspace_invitation_attempt_subject; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invitation_attempts
    ADD CONSTRAINT uq_workspace_invitation_attempt_subject UNIQUE (purpose, subject_hash);


--
-- Name: workspace_members uq_workspace_member; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT uq_workspace_member UNIQUE (workspace_id, account_id);


--
-- Name: work_accesses work_accesses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_accesses
    ADD CONSTRAINT work_accesses_pkey PRIMARY KEY (work_id, account_id);


--
-- Name: works works_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.works
    ADD CONSTRAINT works_pkey PRIMARY KEY (id);


--
-- Name: workspace_invitation_attempts workspace_invitation_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invitation_attempts
    ADD CONSTRAINT workspace_invitation_attempts_pkey PRIMARY KEY (id);


--
-- Name: workspace_invitations workspace_invitations_credential_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invitations
    ADD CONSTRAINT workspace_invitations_credential_id_key UNIQUE (credential_id);


--
-- Name: workspace_invitations workspace_invitations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invitations
    ADD CONSTRAINT workspace_invitations_pkey PRIMARY KEY (id);


--
-- Name: workspace_members workspace_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_pkey PRIMARY KEY (id);


--
-- Name: workspaces workspaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_pkey PRIMARY KEY (id);


--
-- Name: ix_identity_credential_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identity_credential_account ON public.identity_one_time_credentials USING btree (account_id);


--
-- Name: ix_identity_recovery_job_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identity_recovery_job_claim ON public.identity_recovery_request_jobs USING btree (status, created_at);


--
-- Name: ix_identity_sessions_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identity_sessions_account ON public.identity_sessions USING btree (account_id);


--
-- Name: ix_mail_outbox_dispatch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mail_outbox_dispatch ON public.mail_outbox USING btree (status, next_attempt_at);


--
-- Name: ix_work_accesses_account_work; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_work_accesses_account_work ON public.work_accesses USING btree (account_id, work_id);


--
-- Name: ix_works_workspace_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_works_workspace_created ON public.works USING btree (workspace_id, created_at, id);


--
-- Name: ix_workspace_invitations_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workspace_invitations_workspace ON public.workspace_invitations USING btree (workspace_id, created_at);


--
-- Name: ix_workspace_members_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workspace_members_account ON public.workspace_members USING btree (account_id, workspace_id);


--
-- Name: uq_identity_active_credential_per_purpose; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_identity_active_credential_per_purpose ON public.identity_one_time_credentials USING btree (account_id, purpose) WHERE (((status)::text = 'active'::text) AND ((purpose)::text = ANY (ARRAY[('account_activation'::character varying)::text, ('password_recovery'::character varying)::text])));


--
-- Name: uq_workspace_active_invitation; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_workspace_active_invitation ON public.workspace_invitations USING btree (workspace_id, account_id) WHERE ((status)::text = 'active'::text);


--
-- Name: identity_one_time_credentials identity_one_time_credentials_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_one_time_credentials
    ADD CONSTRAINT identity_one_time_credentials_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.identity_accounts(id) ON DELETE CASCADE;


--
-- Name: identity_password_credentials identity_password_credentials_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_password_credentials
    ADD CONSTRAINT identity_password_credentials_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.identity_accounts(id) ON DELETE CASCADE;


--
-- Name: identity_sessions identity_sessions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_sessions
    ADD CONSTRAINT identity_sessions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.identity_accounts(id) ON DELETE CASCADE;


--
-- Name: mail_outbox mail_outbox_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mail_outbox
    ADD CONSTRAINT mail_outbox_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.identity_one_time_credentials(id) ON DELETE CASCADE;


--
-- Name: mail_outbox mail_outbox_recipient_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mail_outbox
    ADD CONSTRAINT mail_outbox_recipient_account_id_fkey FOREIGN KEY (recipient_account_id) REFERENCES public.identity_accounts(id) ON DELETE CASCADE;


--
-- Name: work_accesses work_accesses_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_accesses
    ADD CONSTRAINT work_accesses_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.identity_accounts(id) ON DELETE CASCADE;


--
-- Name: work_accesses work_accesses_work_id_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_accesses
    ADD CONSTRAINT work_accesses_work_id_workspace_id_fkey FOREIGN KEY (work_id, workspace_id) REFERENCES public.works(id, workspace_id) ON DELETE CASCADE;


--
-- Name: works works_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.works
    ADD CONSTRAINT works_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: workspace_invitations workspace_invitations_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invitations
    ADD CONSTRAINT workspace_invitations_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.identity_accounts(id) ON DELETE CASCADE;


--
-- Name: workspace_invitations workspace_invitations_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invitations
    ADD CONSTRAINT workspace_invitations_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.identity_one_time_credentials(id) ON DELETE CASCADE;


--
-- Name: workspace_invitations workspace_invitations_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invitations
    ADD CONSTRAINT workspace_invitations_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: workspace_members workspace_members_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.identity_accounts(id) ON DELETE CASCADE;


--
-- Name: workspace_members workspace_members_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: workspaces workspaces_owner_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_owner_account_id_fkey FOREIGN KEY (owner_account_id) REFERENCES public.identity_accounts(id) ON DELETE RESTRICT;


--
-- Name: work_accesses work_access_cleanup_lock; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_access_cleanup_lock ON public.work_accesses FOR UPDATE USING ((workspace_id = public.work_access_cleanup_workspace_id())) WITH CHECK (false);


--
-- Name: work_accesses work_access_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_access_delete ON public.work_accesses FOR DELETE USING ((((work_id = public.work_management_id()) AND (workspace_id = public.work_management_workspace_id())) OR (workspace_id = public.work_access_cleanup_workspace_id())));


--
-- Name: work_accesses work_access_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_access_insert ON public.work_accesses FOR INSERT WITH CHECK (((work_id = public.work_management_id()) AND (workspace_id = public.work_management_workspace_id()) AND (EXISTS ( SELECT 1
   FROM public.workspace_members member
  WHERE ((member.workspace_id = public.work_management_workspace_id()) AND (member.account_id = work_accesses.account_id))))));


--
-- Name: work_accesses work_access_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_access_read ON public.work_accesses FOR SELECT USING (((account_id = public.workspace_actor_id()) OR ((work_id = public.work_management_id()) AND (workspace_id = public.work_management_workspace_id())) OR (workspace_id = public.work_access_cleanup_workspace_id())));


--
-- Name: work_accesses work_access_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_access_update ON public.work_accesses FOR UPDATE USING (((work_id = public.work_management_id()) AND (workspace_id = public.work_management_workspace_id()))) WITH CHECK (((work_id = public.work_management_id()) AND (workspace_id = public.work_management_workspace_id()) AND (EXISTS ( SELECT 1
   FROM public.workspace_members member
  WHERE ((member.workspace_id = public.work_management_workspace_id()) AND (member.account_id = work_accesses.account_id))))));


--
-- Name: work_accesses; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.work_accesses ENABLE ROW LEVEL SECURITY;

--
-- Name: works work_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_insert ON public.works FOR INSERT WITH CHECK ((EXISTS ( SELECT 1
   FROM public.workspace_members member
  WHERE ((member.workspace_id = works.workspace_id) AND (member.account_id = public.workspace_actor_id())))));


--
-- Name: works work_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_read ON public.works FOR SELECT USING ((((EXISTS ( SELECT 1
   FROM public.workspace_members member
  WHERE ((member.workspace_id = works.workspace_id) AND (member.account_id = public.workspace_actor_id())))) AND (EXISTS ( SELECT 1
   FROM public.work_accesses access
  WHERE ((access.work_id = works.id) AND (access.account_id = public.workspace_actor_id()))))) OR ((id = public.work_management_id()) AND (EXISTS ( SELECT 1
   FROM public.workspace_members member
  WHERE ((member.workspace_id = works.workspace_id) AND (member.account_id = public.workspace_actor_id())))))));


--
-- Name: works work_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_update ON public.works FOR UPDATE USING (((id = public.work_management_id()) AND (EXISTS ( SELECT 1
   FROM public.workspace_members member
  WHERE ((member.workspace_id = works.workspace_id) AND (member.account_id = public.workspace_actor_id())))) AND (EXISTS ( SELECT 1
   FROM public.work_accesses access
  WHERE ((access.work_id = works.id) AND (access.account_id = public.workspace_actor_id()) AND ((access.role)::text = 'maintainer'::text)))))) WITH CHECK (((id = public.work_management_id()) AND (EXISTS ( SELECT 1
   FROM public.workspace_members member
  WHERE ((member.workspace_id = works.workspace_id) AND (member.account_id = public.workspace_actor_id())))) AND (EXISTS ( SELECT 1
   FROM public.work_accesses access
  WHERE ((access.work_id = works.id) AND (access.account_id = public.workspace_actor_id()) AND ((access.role)::text = 'maintainer'::text))))));


--
-- Name: works; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.works ENABLE ROW LEVEL SECURITY;

--
-- Name: workspaces workspace_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_insert ON public.workspaces FOR INSERT WITH CHECK ((owner_account_id = public.workspace_actor_id()));


--
-- Name: workspace_invitation_attempts workspace_invitation_attempt_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_invitation_attempt_access ON public.workspace_invitation_attempts USING (true) WITH CHECK (true);


--
-- Name: workspace_invitation_attempts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspace_invitation_attempts ENABLE ROW LEVEL SECURITY;

--
-- Name: workspace_invitations workspace_invitation_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_invitation_insert ON public.workspace_invitations FOR INSERT WITH CHECK ((workspace_id = public.workspace_management_id()));


--
-- Name: workspace_invitations workspace_invitation_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_invitation_read ON public.workspace_invitations FOR SELECT USING (((workspace_id = public.workspace_management_id()) OR (workspace_id = public.workspace_maintenance_id()) OR (credential_id = public.workspace_invitation_credential_id())));


--
-- Name: workspace_invitations workspace_invitation_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_invitation_update ON public.workspace_invitations FOR UPDATE USING (((workspace_id = public.workspace_management_id()) OR (credential_id = public.workspace_invitation_credential_id()))) WITH CHECK (((workspace_id = public.workspace_management_id()) OR (credential_id = public.workspace_invitation_credential_id())));


--
-- Name: workspace_invitations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspace_invitations ENABLE ROW LEVEL SECURITY;

--
-- Name: workspace_members workspace_member_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_member_delete ON public.workspace_members FOR DELETE USING ((workspace_id = public.workspace_management_id()));


--
-- Name: workspace_members workspace_member_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_member_insert ON public.workspace_members FOR INSERT WITH CHECK (((workspace_id = public.workspace_management_id()) OR (EXISTS ( SELECT 1
   FROM public.workspace_invitations invitation
  WHERE ((invitation.workspace_id = workspace_members.workspace_id) AND (invitation.account_id = workspace_members.account_id) AND (invitation.credential_id = public.workspace_invitation_credential_id()) AND ((invitation.status)::text = 'active'::text))))));


--
-- Name: workspace_members workspace_member_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_member_read ON public.workspace_members FOR SELECT USING (((account_id = public.workspace_actor_id()) OR (workspace_id = public.workspace_management_id()) OR (workspace_id = public.workspace_maintenance_id())));


--
-- Name: workspace_members workspace_member_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_member_update ON public.workspace_members FOR UPDATE USING ((workspace_id = public.workspace_management_id())) WITH CHECK ((workspace_id = public.workspace_management_id()));


--
-- Name: workspace_members workspace_member_work_management_lock; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_member_work_management_lock ON public.workspace_members FOR UPDATE USING ((workspace_id = public.work_management_workspace_id())) WITH CHECK (false);


--
-- Name: workspace_members workspace_member_work_management_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_member_work_management_read ON public.workspace_members FOR SELECT USING ((workspace_id = public.work_management_workspace_id()));


--
-- Name: workspace_members; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspace_members ENABLE ROW LEVEL SECURITY;

--
-- Name: workspaces workspace_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY workspace_read ON public.workspaces FOR SELECT USING (((owner_account_id = public.workspace_actor_id()) OR (id = public.workspace_maintenance_id()) OR (EXISTS ( SELECT 1
   FROM public.workspace_members member
  WHERE ((member.workspace_id = workspaces.id) AND (member.account_id = public.workspace_actor_id())))) OR (EXISTS ( SELECT 1
   FROM public.workspace_invitations invitation
  WHERE ((invitation.workspace_id = workspaces.id) AND (invitation.credential_id = public.workspace_invitation_credential_id()))))));


--
-- Name: workspaces; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspaces ENABLE ROW LEVEL SECURITY;

--
-- PostgreSQL database dump complete
--

