from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def _set_scope(session: Session, name: str, value: UUID | str) -> None:
    session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": name, "value": str(value)},
    )


def set_actor(session: Session, account_id: UUID) -> None:
    _set_scope(session, "app.actor_id", account_id)


def set_invitation_credential(session: Session, credential_id: UUID) -> None:
    _set_scope(session, "app.invitation_credential_id", credential_id)


def set_workspace_invitation_inbox_scope(session: Session, invitation_id: UUID) -> None:
    _set_scope(session, "app.workspace_invitation_inbox_id", invitation_id)


def set_notification_todo_source_scope(
    session: Session, recipient_id: UUID, kind: str, source_key: str
) -> None:
    _set_scope(session, "app.notification_todo_recipient_id", recipient_id)
    _set_scope(session, "app.notification_todo_kind", kind)
    _set_scope(session, "app.notification_todo_source_key", source_key)


def set_notification_todo_target_scope(
    session: Session, target_kind: str, target_id: UUID
) -> None:
    _set_scope(session, "app.notification_todo_target_kind", target_kind)
    _set_scope(session, "app.notification_todo_target_id", target_id)


def set_notification_todo_work_cleanup_scope(
    session: Session, recipient_id: UUID, work_id: UUID
) -> None:
    _set_scope(session, "app.notification_todo_cleanup_recipient_id", recipient_id)
    _set_scope(session, "app.notification_todo_cleanup_work_id", work_id)


def set_notification_todo_dispatch_scope(session: Session, todo_id: UUID) -> None:
    _set_scope(session, "app.notification_todo_dispatch_id", todo_id)


def set_workspace_management_scope(session: Session, workspace_id: UUID) -> None:
    _set_scope(session, "app.workspace_management_id", workspace_id)


def set_maintenance_workspace_scope(session: Session, workspace_id: UUID) -> None:
    _set_scope(session, "app.maintenance_workspace_id", workspace_id)


def set_work_management_scope(
    session: Session, work_id: UUID, workspace_id: UUID
) -> None:
    _set_scope(session, "app.work_management_id", work_id)
    _set_scope(session, "app.work_management_workspace_id", workspace_id)


def set_work_access_cleanup_scope(session: Session, workspace_id: UUID) -> None:
    _set_scope(session, "app.work_access_cleanup_workspace_id", workspace_id)


def set_file_lifecycle_scope(session: Session, file_id: UUID) -> None:
    _set_scope(session, "app.file_lifecycle_id", file_id)


def set_file_recovery_work_scope(session: Session, work_id: UUID) -> None:
    _set_scope(session, "app.file_recovery_work_id", work_id)


def set_playtest_management_scope(
    session: Session, workspace_id: UUID, work_id: UUID
) -> None:
    _set_scope(session, "app.playtest_management_workspace_id", workspace_id)
    _set_scope(session, "app.playtest_management_work_id", work_id)


def set_playtest_session_scope(session: Session, session_id: UUID) -> None:
    _set_scope(session, "app.playtest_session_id", session_id)


def set_playtest_result_material_work_scope(session: Session, work_id: UUID) -> None:
    _set_scope(session, "app.playtest_result_material_work_id", work_id)


def set_playtest_participant_lookup_scope(session: Session, session_id: UUID) -> None:
    _set_scope(session, "app.playtest_participant_lookup_session_id", session_id)


def set_feedback_management_scope(session: Session, session_id: UUID) -> None:
    _set_scope(session, "app.feedback_management_session_id", session_id)


def set_feedback_participant_scope(session: Session, session_id: UUID) -> None:
    _set_scope(session, "app.feedback_participant_session_id", session_id)


def set_feedback_answer_item_lock_scope(session: Session, item_id: UUID) -> None:
    _set_scope(session, "app.feedback_answer_item_lock_id", item_id)


def set_issue_evidence_scope(session: Session, issue_id: UUID) -> None:
    _set_scope(session, "app.issue_evidence_id", issue_id)


def set_issue_evidence_candidate_observation_scope(
    session: Session, observation_id: UUID
) -> None:
    _set_scope(session, "app.issue_evidence_candidate_observation_id", observation_id)


def set_issue_evidence_candidate_feedback_submission_scope(
    session: Session, submission_id: UUID
) -> None:
    _set_scope(
        session, "app.issue_evidence_candidate_feedback_submission_id", submission_id
    )


def set_project_baseline_scope(session: Session) -> None:
    session.execute(
        text("SELECT set_config('app.project_baseline_maintenance', 'active', true)")
    )
