import os
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import and_, or_, update
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


def enqueue_business_mail(
    session: Session,
    account_id: UUID,
    purpose: str,
    subject: str,
    body: str,
    *,
    business_scope: str,
) -> MailOutbox:
    """写入已冻结正文的业务邮件，不附带一次性凭据。"""
    outbox = MailOutbox(
        recipient_account_id=account_id,
        purpose=purpose,
        business_scope=business_scope,
        frozen_subject=subject,
        frozen_body=body,
    )
    session.add(outbox)
    session.flush()
    return outbox


def suppress_business_mails(session: Session, business_scope: str) -> None:
    """取消场次时只抑制尚未被 SMTP 领取的本场业务邮件。"""
    session.execute(
        update(MailOutbox)
        .where(
            MailOutbox.business_scope == business_scope,
            MailOutbox.credential_id.is_(None),
            or_(
                MailOutbox.status == "pending",
                and_(
                    MailOutbox.status == "sending",
                    MailOutbox.smtp_started_at.is_(None),
                ),
            ),
        )
        .values(status="suppressed", claim_id=None)
    )


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
