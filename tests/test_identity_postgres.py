from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.audit.models import SecurityAudit
from app.core.config import settings
from app.core.database import SessionLocal
from app.identity import service
from app.identity.models import Account, AttemptRecord, RecoveryRequestJob
from app.main import app
from app.notifications.models import MailOutbox
from app.notifications.service import decrypt_token

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test", reason="需要 DB_NAME=rulefolio_test"
)


def test_identity_flow_with_postgresql() -> None:
    email = f"br001-{uuid4().hex}@example.com"
    account_id: UUID | None = None
    recovery_job_id: UUID | None = None
    initial_job_ids: set[UUID]

    with SessionLocal() as session:
        initial_job_ids = set(session.scalars(select(RecoveryRequestJob.id)))
        account_id = service.provision_account(session, email, "pytest", "BR-001 验证")
        activation_outbox = session.scalar(
            select(MailOutbox).where(
                MailOutbox.recipient_account_id == account_id,
                MailOutbox.purpose == "account_activation",
            )
        )
        assert activation_outbox is not None
        activation_token = decrypt_token(activation_outbox)

    try:
        with TestClient(app) as client:
            activation = client.post(
                "/api/v1/account-activations/exchanges",
                json={"token": activation_token, "newPassword": "activation-password"},
            )
            assert activation.status_code == 200
            activation_session = activation.json()["data"]["sessionToken"]

            login = client.post(
                "/api/v1/sessions",
                json={"email": email, "password": "activation-password"},
            )
            assert login.status_code == 200
            login_session = login.json()["data"]["sessionToken"]
            assert (
                client.get(
                    "/api/v1/sessions/current",
                    headers={"Authorization": f"Bearer {login_session}"},
                ).status_code
                == 200
            )
            assert (
                client.delete(
                    "/api/v1/sessions/current",
                    headers={"Authorization": f"Bearer {login_session}"},
                ).status_code
                == 200
            )
            assert (
                client.get(
                    "/api/v1/sessions/current",
                    headers={"Authorization": f"Bearer {login_session}"},
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/api/v1/account-recovery-requests", json={"email": email}
                ).status_code
                == 202
            )

        with SessionLocal() as session:
            recovery_job_id = next(
                job_id
                for job_id in session.scalars(select(RecoveryRequestJob.id))
                if job_id not in initial_job_ids
            )
            assert service.process_next_recovery_request(session) == "completed"
            recovery_job = session.get(RecoveryRequestJob, recovery_job_id)
            assert recovery_job is not None
            assert recovery_job.email_ciphertext is None
            recovery_outbox = session.scalar(
                select(MailOutbox).where(
                    MailOutbox.recipient_account_id == account_id,
                    MailOutbox.purpose == "password_recovery",
                )
            )
            assert recovery_outbox is not None
            recovery_token = decrypt_token(recovery_outbox)

        with TestClient(app) as client:
            recovery = client.post(
                "/api/v1/account-recovery-exchanges",
                json={"token": recovery_token, "newPassword": "recovered-password"},
            )
            assert recovery.status_code == 200
            recovered_session = recovery.json()["data"]["sessionToken"]
            assert (
                client.get(
                    "/api/v1/sessions/current",
                    headers={"Authorization": f"Bearer {activation_session}"},
                ).status_code
                == 401
            )
            assert (
                client.get(
                    "/api/v1/sessions/current",
                    headers={"Authorization": f"Bearer {recovered_session}"},
                ).status_code
                == 200
            )
    finally:
        with SessionLocal() as session:
            if account_id is not None:
                session.execute(
                    delete(SecurityAudit).where(
                        (SecurityAudit.actor_account_id == account_id)
                        | (SecurityAudit.target_account_id == account_id)
                    )
                )
                session.execute(delete(Account).where(Account.id == account_id))
            session.execute(
                delete(AttemptRecord).where(
                    AttemptRecord.subject_hash == service._attempt_subject(email)
                )
            )
            if recovery_job_id is not None:
                session.execute(
                    delete(RecoveryRequestJob).where(
                        RecoveryRequestJob.id == recovery_job_id
                    )
                )
            session.commit()
