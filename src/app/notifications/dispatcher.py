import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.identity.models import Account, OneTimeCredential
from app.notifications.models import MailOutbox
from app.notifications.service import decrypt_token


@dataclass(frozen=True)
class DispatchClaim:
    outbox_id: UUID
    claim_id: UUID
    attempt_count: int


def _now() -> datetime:
    return datetime.now(UTC)


def _message_content(purpose: str, token: str) -> tuple[str, str]:
    if purpose == "account_activation":
        subject = "设置好玩实验室账户密码"
        path = "/auth/activate"
        action = "设置密码并激活账户"
    else:
        subject = "恢复好玩实验室账户"
        path = "/auth/reset"
        action = "更新密码"
    link = f"{settings.app_public_url.rstrip('/')}{path}#token={token}"
    return (
        subject,
        f"请在有效期内打开以下链接{action}：\n{link}\n\n如果不是你本人发起的操作，请忽略此邮件。",
    )


def _send(to_address: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{settings.mail_from_name} <{settings.mail_from_address}>"
    message["To"] = to_address
    if settings.mail_reply_to:
        message["Reply-To"] = settings.mail_reply_to
    message.set_content(body)

    password = settings.smtp_password.get_secret_value()
    if settings.smtp_ssl:
        with smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, context=ssl.create_default_context()
        ) as client:
            if settings.smtp_username:
                client.login(settings.smtp_username, password)
            refused = client.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as client:
            if settings.smtp_starttls:
                client.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                client.login(settings.smtp_username, password)
            refused = client.send_message(message)
    if refused:
        raise smtplib.SMTPRecipientsRefused(refused)


def recover_stale_dispatches(session: Session) -> int:
    cutoff = _now() - timedelta(minutes=settings.mail_dispatch_stale_minutes)
    outboxes = list(
        session.scalars(
            select(MailOutbox)
            .where(
                MailOutbox.status == "sending", MailOutbox.dispatch_started_at < cutoff
            )
            .with_for_update(skip_locked=True)
        )
    )
    for outbox in outboxes:
        outbox.claim_id = None
        if outbox.smtp_started_at is None:
            outbox.status = "pending"
            outbox.dispatch_started_at = None
        else:
            outbox.status = "unknown"
            outbox.last_error_code = "smtp_result_unknown"
            outbox.token_ciphertext = None
            outbox.token_nonce = None
            outbox.key_version = None
    session.commit()
    return len(outboxes)


def _claim_next(session: Session) -> DispatchClaim | None:
    outbox = session.scalar(
        select(MailOutbox)
        .where(MailOutbox.status == "pending", MailOutbox.next_attempt_at <= _now())
        .order_by(MailOutbox.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if outbox is None:
        session.rollback()
        return None
    claim_id = uuid4()
    outbox.status = "sending"
    outbox.claim_id = claim_id
    outbox.dispatch_started_at = _now()
    outbox.attempt_count += 1
    session.commit()
    return DispatchClaim(outbox.id, claim_id, outbox.attempt_count)


def _load_claim(session: Session, claim: DispatchClaim) -> MailOutbox | None:
    outbox = session.scalar(
        select(MailOutbox)
        .where(
            MailOutbox.id == claim.outbox_id,
            MailOutbox.status == "sending",
            MailOutbox.claim_id == claim.claim_id,
        )
        .with_for_update()
    )
    if outbox is None:
        session.rollback()
    return outbox


def _update_claim(
    session: Session, claim: DispatchClaim, values: dict[str, object]
) -> bool:
    result = session.execute(
        update(MailOutbox)
        .where(
            MailOutbox.id == claim.outbox_id,
            MailOutbox.status == "sending",
            MailOutbox.claim_id == claim.claim_id,
        )
        .values(claim_id=None, **values)
    )
    session.commit()
    return result.rowcount == 1


def _mark_ineligible(session: Session, claim: DispatchClaim) -> bool:
    return _update_claim(
        session,
        claim,
        {
            "status": "cancelled",
            "last_error_code": "credential_unavailable",
            "token_ciphertext": None,
            "token_nonce": None,
            "key_version": None,
        },
    )


def _mark_accepted(session: Session, claim: DispatchClaim) -> bool:
    return _update_claim(
        session,
        claim,
        {
            "status": "accepted",
            "accepted_at": _now(),
            "last_error_code": None,
            "token_ciphertext": None,
            "token_nonce": None,
            "key_version": None,
        },
    )


def _mark_failed(session: Session, claim: DispatchClaim) -> bool:
    if claim.attempt_count < settings.mail_max_attempts:
        values: dict[str, object] = {
            "status": "pending",
            "next_attempt_at": _now() + timedelta(minutes=claim.attempt_count),
            "dispatch_started_at": None,
            "smtp_started_at": None,
            "last_error_code": "smtp_explicit_failure",
        }
    else:
        values = {
            "status": "failed",
            "last_error_code": "smtp_explicit_failure",
            "token_ciphertext": None,
            "token_nonce": None,
            "key_version": None,
        }
    return _update_claim(session, claim, values)


def _mark_unknown(session: Session, claim: DispatchClaim) -> bool:
    return _update_claim(
        session,
        claim,
        {
            "status": "unknown",
            "last_error_code": "smtp_result_unknown",
            "token_ciphertext": None,
            "token_nonce": None,
            "key_version": None,
        },
    )


def dispatch_one(session: Session) -> str | None:
    claim = _claim_next(session)
    if claim is None:
        return None

    outbox = _load_claim(session, claim)
    if outbox is None:
        return "superseded"
    credential = session.get(OneTimeCredential, outbox.credential_id)
    account = session.get(Account, outbox.recipient_account_id)
    if (
        credential is None
        or account is None
        or (
            outbox.purpose == "account_activation"
            and account.status != "pending_activation"
        )
        or (outbox.purpose == "password_recovery" and account.status != "active")
        or credential.status != "active"
        or credential.expires_at <= _now()
    ):
        _mark_ineligible(session, claim)
        return "cancelled"

    try:
        token = decrypt_token(outbox)
        subject, body = _message_content(outbox.purpose, token)
    except Exception:
        _mark_ineligible(session, claim)
        return "cancelled"

    outbox.smtp_started_at = _now()
    session.commit()
    try:
        _send(account.email, subject, body)
    except (TimeoutError, OSError, smtplib.SMTPServerDisconnected):
        return "unknown" if _mark_unknown(session, claim) else "superseded"
    except smtplib.SMTPException:
        if not _mark_failed(session, claim):
            return "superseded"
        return (
            "pending" if claim.attempt_count < settings.mail_max_attempts else "failed"
        )
    return "accepted" if _mark_accepted(session, claim) else "superseded"
