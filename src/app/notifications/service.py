import os
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.notifications.models import MailOutbox


def _associated_data(credential_id: UUID, purpose: str) -> bytes:
    return f"{credential_id}:{purpose}:{settings.token_encryption_key_version}".encode()


def enqueue_token_mail(
    session: Session,
    credential_id: UUID,
    account_id: UUID,
    purpose: str,
    token: str,
    workspace_name: str | None = None,
) -> MailOutbox:
    nonce = os.urandom(12)
    ciphertext = AESGCM(settings.token_encryption_key_bytes).encrypt(
        nonce,
        token.encode(),
        _associated_data(credential_id, purpose),
    )
    outbox = MailOutbox(
        credential_id=credential_id,
        recipient_account_id=account_id,
        purpose=purpose,
        workspace_name=workspace_name,
        token_ciphertext=ciphertext,
        token_nonce=nonce,
        key_version=settings.token_encryption_key_version,
    )
    session.add(outbox)
    return outbox


def clear_outbox_envelopes(
    session: Session, credential_ids: list[UUID], status: str = "cancelled"
) -> None:
    if not credential_ids:
        return
    session.execute(
        update(MailOutbox)
        .where(
            MailOutbox.credential_id.in_(credential_ids),
            MailOutbox.status.in_(("pending", "sending")),
        )
        .values(
            status=status,
            claim_id=None,
            token_ciphertext=None,
            token_nonce=None,
            key_version=None,
        )
    )


def decrypt_token(outbox: MailOutbox) -> str:
    if outbox.token_ciphertext is None or outbox.token_nonce is None:
        raise ValueError("邮件凭据已清除")
    return (
        AESGCM(settings.token_encryption_key_bytes)
        .decrypt(
            outbox.token_nonce,
            outbox.token_ciphertext,
            _associated_data(outbox.credential_id, outbox.purpose),
        )
        .decode()
    )
