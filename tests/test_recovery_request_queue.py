from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.identity.models import RecoveryRequestJob
from app.identity.service import (
    _queue_recovery_request,
    _recovery_request_associated_data,
)


class RecordingSession:
    def __init__(self) -> None:
        self.items: list[object] = []

    def add(self, item: object) -> None:
        self.items.append(item)


def test_recovery_request_queue_encrypts_email() -> None:
    session = RecordingSession()
    email = "creator@example.com"

    _queue_recovery_request(session, email)

    assert len(session.items) == 1
    job = session.items[0]
    assert isinstance(job, RecoveryRequestJob)
    assert email.encode() not in job.email_ciphertext
    assert (
        AESGCM(settings.token_encryption_key_bytes)
        .decrypt(
            job.email_nonce,
            job.email_ciphertext,
            _recovery_request_associated_data(job.id),
        )
        .decode()
        == email
    )
