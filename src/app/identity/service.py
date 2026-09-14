import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import set_actor
from app.audit.models import SecurityAudit
from app.core.config import settings
from app.identity.models import (
    Account,
    AttemptRecord,
    OneTimeCredential,
    PasswordCredential,
    RecoveryRequestJob,
    SessionRecord,
)
from app.notifications.models import MailOutbox
from app.notifications.service import clear_outbox_envelopes, enqueue_token_mail

ACTIVE = "active"
PENDING_ACTIVATION = "pending_activation"
DISABLED = "disabled"
TOKEN_ACTIVE = "active"
TOKEN_CONSUMED = "consumed"
TOKEN_REVOKED = "revoked"
ACTIVATION = "account_activation"
RECOVERY = "password_recovery"
WORKSPACE_INVITATION = "workspace_invitation"
RECOVERY_JOB_PENDING = "pending"
RECOVERY_JOB_PROCESSING = "processing"
RECOVERY_JOB_COMPLETED = "completed"
RECOVERY_JOB_FAILED = "failed"


class AuthenticationFailed(Exception):
    pass


class SessionUnavailable(Exception):
    pass


class LinkUnavailable(Exception):
    pass


@dataclass(frozen=True)
class RateLimited(Exception):
    retry_after: int


@dataclass(frozen=True)
class SessionResult:
    token: str
    account_id: UUID
    account_status: str
    expires_at: datetime


@dataclass(frozen=True)
class RecoveryRequestClaim:
    job_id: UUID
    claim_id: UUID


@dataclass(frozen=True)
class AuthenticatedSession:
    record: SessionRecord
    account: Account


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_recovery_duration(started_at: float) -> None:
    remaining = settings.recovery_response_min_duration_ms / 1_000 - (
        time.monotonic() - started_at
    )
    if remaining > 0:
        time.sleep(remaining)


def normalize_email(email: str) -> str:
    try:
        return validate_email(email, check_deliverability=False).normalized.casefold()
    except EmailNotValidError as error:
        raise ValueError("邮箱格式不正确") from error


def validate_password(password: str) -> None:
    length = len(password)
    if not settings.password_min_length <= length <= settings.password_max_length:
        raise ValueError(
            f"密码长度需为 {settings.password_min_length} 至 {settings.password_max_length} 个字符"
        )


def _password_hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
        type=Type.ID,
    )


DUMMY_PASSWORD_HASH = _password_hasher().hash(secrets.token_urlsafe(32))


def _token_hash(token: str) -> bytes:
    return sha256(token.encode()).digest()


def _attempt_subject(value: str) -> bytes:
    pepper = settings.auth_attempt_pepper.get_secret_value()
    if not pepper:
        raise RuntimeError("AUTH_ATTEMPT_PEPPER 未配置")
    return hmac.digest(pepper.encode(), value.encode(), "sha256")


def attempt_subject(value: str) -> bytes:
    """返回可由其他业务复用的受保护尝试主体散列。"""
    return _attempt_subject(value)


def _attempt_retry_after(
    session: Session, purpose: str, subject_hash: bytes, maximum: int
) -> int | None:
    now = _now()
    record = session.scalar(
        select(AttemptRecord)
        .where(
            AttemptRecord.purpose == purpose, AttemptRecord.subject_hash == subject_hash
        )
        .with_for_update()
    )
    if record is None:
        return None
    window = timedelta(minutes=settings.auth_attempt_window_minutes)
    if now - record.window_started_at >= window:
        record.window_started_at = now
        record.count = 0
        return None
    if record.count >= maximum:
        return max(1, int((window - (now - record.window_started_at)).total_seconds()))
    return None


def _record_attempt(session: Session, purpose: str, subject_hash: bytes) -> None:
    now = _now()
    record = session.scalar(
        select(AttemptRecord)
        .where(
            AttemptRecord.purpose == purpose, AttemptRecord.subject_hash == subject_hash
        )
        .with_for_update()
    )
    if record is None:
        session.add(
            AttemptRecord(
                purpose=purpose,
                subject_hash=subject_hash,
                window_started_at=now,
                count=1,
            )
        )
        return
    if now - record.window_started_at >= timedelta(
        minutes=settings.auth_attempt_window_minutes
    ):
        record.window_started_at = now
        record.count = 1
        return
    record.count += 1


def _clear_attempt(session: Session, purpose: str, subject_hash: bytes) -> None:
    record = session.scalar(
        select(AttemptRecord).where(
            AttemptRecord.purpose == purpose, AttemptRecord.subject_hash == subject_hash
        )
    )
    if record is not None:
        session.delete(record)


def _create_session(session: Session, account: Account) -> SessionResult:
    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(hours=settings.session_ttl_hours)
    session.add(
        SessionRecord(
            account_id=account.id, token_hash=_token_hash(token), expires_at=expires_at
        )
    )
    return SessionResult(
        token=token,
        account_id=account.id,
        account_status=account.status,
        expires_at=expires_at,
    )


def _set_password(session: Session, account_id: UUID, password: str) -> None:
    validate_password(password)
    hashed = _password_hasher().hash(password)
    credential = session.get(PasswordCredential, account_id)
    if credential is None:
        session.add(PasswordCredential(account_id=account_id, password_hash=hashed))
    else:
        credential.password_hash = hashed
        credential.updated_at = _now()


def get_or_create_invitation_account(session: Session, email: str) -> Account:
    """取得受邀账户；首次邀请只建立待激活账户，不在此处提交事务。"""
    normalized_email = normalize_email(email)
    account = session.scalar(
        select(Account).where(Account.email == normalized_email).with_for_update()
    )
    if account is not None:
        return account

    try:
        with session.begin_nested():
            account = Account(email=normalized_email, status=PENDING_ACTIVATION)
            session.add(account)
            session.flush()
    except IntegrityError:
        account = session.scalar(
            select(Account).where(Account.email == normalized_email).with_for_update()
        )
        if account is None:
            raise RuntimeError("创建受邀账户时无法取得账户记录")
    return account


def issue_workspace_invitation_credential(
    session: Session, account: Account
) -> tuple[OneTimeCredential, str]:
    """签发工作空间邀请凭据，调用方负责写入 Outbox 与提交事务。"""
    token = secrets.token_urlsafe(32)
    credential = OneTimeCredential(
        account_id=account.id,
        purpose=WORKSPACE_INVITATION,
        token_hash=_token_hash(token),
        expires_at=_now() + timedelta(minutes=settings.one_time_token_ttl_minutes),
    )
    session.add(credential)
    session.flush()
    return credential, token


def find_workspace_invitation_credential_id(
    session: Session, token: str
) -> UUID | None:
    """以摘要无锁定位邀请凭据，调用方必须在加锁后复核。"""
    return session.scalar(
        select(OneTimeCredential.id).where(
            OneTimeCredential.purpose == WORKSPACE_INVITATION,
            OneTimeCredential.token_hash == _token_hash(token),
        )
    )


def lock_workspace_invitation_credential(
    session: Session, token: str
) -> OneTimeCredential | None:
    """以摘要定位并锁定邀请凭据；调用方据此设置精确 RLS 范围。"""
    digest = _token_hash(token)
    credential = session.scalar(
        select(OneTimeCredential)
        .where(
            OneTimeCredential.purpose == WORKSPACE_INVITATION,
            OneTimeCredential.token_hash == digest,
        )
        .with_for_update()
    )
    if credential is None or not hmac.compare_digest(credential.token_hash, digest):
        return None
    return credential


def consume_workspace_invitation_credential(
    session: Session, credential: OneTimeCredential, now: datetime
) -> bool:
    result = session.execute(
        update(OneTimeCredential)
        .where(
            OneTimeCredential.id == credential.id,
            OneTimeCredential.purpose == WORKSPACE_INVITATION,
            OneTimeCredential.status == TOKEN_ACTIVE,
            OneTimeCredential.expires_at > now,
        )
        .values(status=TOKEN_CONSUMED, consumed_at=now)
    )
    return result.rowcount == 1


def revoke_workspace_invitation_credential(
    session: Session, credential: OneTimeCredential, now: datetime
) -> bool:
    result = session.execute(
        update(OneTimeCredential)
        .where(
            OneTimeCredential.id == credential.id,
            OneTimeCredential.purpose == WORKSPACE_INVITATION,
            OneTimeCredential.status == TOKEN_ACTIVE,
        )
        .values(status=TOKEN_REVOKED, revoked_at=now)
    )
    return result.rowcount == 1


def lock_account(session: Session, account_id: UUID) -> Account | None:
    return session.scalar(
        select(Account).where(Account.id == account_id).with_for_update()
    )


def activate_invited_account(
    session: Session, account: Account, password: str
) -> SessionResult:
    """为待激活受邀账户设置密码、激活并签发一次 session，不提交。"""
    if account.status != PENDING_ACTIVATION:
        raise LinkUnavailable
    _set_password(session, account.id, password)
    account.status = ACTIVE
    clear_outbox_envelopes(
        session, _revoke_credentials(session, account.id, ACTIVATION)
    )
    return _create_session(session, account)


def _revoke_credentials(session: Session, account_id: UUID, purpose: str) -> list[UUID]:
    credentials = list(
        session.scalars(
            select(OneTimeCredential)
            .where(
                OneTimeCredential.account_id == account_id,
                OneTimeCredential.purpose == purpose,
                OneTimeCredential.status == TOKEN_ACTIVE,
            )
            .with_for_update()
        )
    )
    now = _now()
    for credential in credentials:
        credential.status = TOKEN_REVOKED
        credential.revoked_at = now
    return [credential.id for credential in credentials]


def _create_one_time_credential(
    session: Session, account: Account, purpose: str
) -> OneTimeCredential:
    clear_outbox_envelopes(session, _revoke_credentials(session, account.id, purpose))
    token = secrets.token_urlsafe(32)
    credential = OneTimeCredential(
        account_id=account.id,
        purpose=purpose,
        token_hash=_token_hash(token),
        expires_at=_now() + timedelta(minutes=settings.one_time_token_ttl_minutes),
    )
    session.add(credential)
    session.flush()
    enqueue_token_mail(session, credential.id, account.id, purpose, token)
    return credential


def _commit_or_rollback(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _recovery_request_associated_data(job_id: UUID) -> bytes:
    return f"recovery-request:{job_id}:{settings.token_encryption_key_version}".encode()


def _queue_recovery_request(session: Session, email: str) -> None:
    job_id = uuid4()
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(settings.token_encryption_key_bytes).encrypt(
        nonce,
        email.encode(),
        _recovery_request_associated_data(job_id),
    )
    session.add(
        RecoveryRequestJob(
            id=job_id,
            email_ciphertext=ciphertext,
            email_nonce=nonce,
            key_version=settings.token_encryption_key_version,
        )
    )


def _claim_recovery_request_job(session: Session) -> RecoveryRequestClaim | None:
    job = session.scalar(
        select(RecoveryRequestJob)
        .where(RecoveryRequestJob.status == RECOVERY_JOB_PENDING)
        .order_by(RecoveryRequestJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        session.rollback()
        return None
    claim_id = uuid4()
    job.status = RECOVERY_JOB_PROCESSING
    job.claim_id = claim_id
    job.processing_started_at = _now()
    _commit_or_rollback(session)
    return RecoveryRequestClaim(job.id, claim_id)


def recover_stale_recovery_request_jobs(session: Session) -> int:
    cutoff = _now() - timedelta(minutes=settings.recovery_job_stale_minutes)
    jobs = list(
        session.scalars(
            select(RecoveryRequestJob)
            .where(
                RecoveryRequestJob.status == RECOVERY_JOB_PROCESSING,
                RecoveryRequestJob.processing_started_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        job.status = RECOVERY_JOB_PENDING
        job.claim_id = None
        job.processing_started_at = None
    _commit_or_rollback(session)
    return len(jobs)


def _finish_recovery_request_job(
    session: Session, claim: RecoveryRequestClaim, status: str
) -> bool:
    result = session.execute(
        update(RecoveryRequestJob)
        .where(
            RecoveryRequestJob.id == claim.job_id,
            RecoveryRequestJob.status == RECOVERY_JOB_PROCESSING,
            RecoveryRequestJob.claim_id == claim.claim_id,
        )
        .values(
            status=status,
            claim_id=None,
            email_ciphertext=None,
            email_nonce=None,
            key_version=None,
            completed_at=_now(),
        )
    )
    _commit_or_rollback(session)
    return result.rowcount == 1


def process_next_recovery_request(session: Session) -> str | None:
    claim = _claim_recovery_request_job(session)
    if claim is None:
        return None
    job = session.scalar(
        select(RecoveryRequestJob)
        .where(
            RecoveryRequestJob.id == claim.job_id,
            RecoveryRequestJob.status == RECOVERY_JOB_PROCESSING,
            RecoveryRequestJob.claim_id == claim.claim_id,
        )
        .with_for_update()
    )
    if job is None:
        session.rollback()
        return "superseded"
    if job.email_nonce is None or job.email_ciphertext is None:
        return (
            "failed"
            if _finish_recovery_request_job(session, claim, RECOVERY_JOB_FAILED)
            else "superseded"
        )
    try:
        email = (
            AESGCM(settings.token_encryption_key_bytes)
            .decrypt(
                job.email_nonce,
                job.email_ciphertext,
                _recovery_request_associated_data(job.id),
            )
            .decode()
        )
    except Exception:
        return (
            "failed"
            if _finish_recovery_request_job(session, claim, RECOVERY_JOB_FAILED)
            else "superseded"
        )

    account = session.scalar(
        select(Account).where(Account.email == email).with_for_update()
    )
    if (
        account is not None
        and account.status == ACTIVE
        and session.get(PasswordCredential, account.id) is not None
    ):
        _create_one_time_credential(session, account, RECOVERY)
        session.add(
            SecurityAudit(action="recovery_requested", target_account_id=account.id)
        )
    return (
        "completed"
        if _finish_recovery_request_job(session, claim, RECOVERY_JOB_COMPLETED)
        else "superseded"
    )


def login(session: Session, email: str, password: str) -> SessionResult:
    normalized_email = normalize_email(email)
    subject_hash = _attempt_subject(normalized_email)
    retry_after = _attempt_retry_after(
        session, "login", subject_hash, settings.login_max_attempts
    )
    if retry_after is not None:
        _commit_or_rollback(session)
        raise RateLimited(retry_after)

    account = session.scalar(
        select(Account).where(Account.email == normalized_email).with_for_update()
    )
    credential = (
        session.get(PasswordCredential, account.id) if account is not None else None
    )
    eligible = (
        account is not None and account.status == ACTIVE and credential is not None
    )
    password_hash = credential.password_hash if eligible else DUMMY_PASSWORD_HASH
    hasher = _password_hasher()
    try:
        password_matches = hasher.verify(password_hash, password)
    except VerifyMismatchError:
        password_matches = False
    except InvalidHashError:
        try:
            hasher.verify(DUMMY_PASSWORD_HASH, password)
        except VerifyMismatchError:
            pass
        password_matches = False
    if not eligible or not password_matches:
        _record_attempt(session, "login", subject_hash)
        _commit_or_rollback(session)
        raise AuthenticationFailed

    if hasher.check_needs_rehash(credential.password_hash):
        credential.password_hash = hasher.hash(password)
    _clear_attempt(session, "login", subject_hash)
    result = _create_session(session, account)
    session.add(
        SecurityAudit(
            action="session_created",
            actor_account_id=account.id,
            target_account_id=account.id,
        )
    )
    _commit_or_rollback(session)
    return result


def authenticate(session: Session, token: str) -> AuthenticatedSession:
    if not token:
        raise SessionUnavailable
    digest = _token_hash(token)
    row = session.execute(
        select(SessionRecord, Account)
        .join(Account, Account.id == SessionRecord.account_id)
        .where(SessionRecord.token_hash == digest)
    ).one_or_none()
    if row is None:
        session.rollback()
        raise SessionUnavailable
    record, account = row
    if (
        not hmac.compare_digest(record.token_hash, digest)
        or record.revoked_at is not None
        or record.expires_at <= _now()
        or account.status != ACTIVE
    ):
        session.rollback()
        raise SessionUnavailable
    set_actor(session, account.id)
    return AuthenticatedSession(record=record, account=account)


def logout(session: Session, token: str) -> None:
    authenticated = authenticate(session, token)
    authenticated.record.revoked_at = _now()
    authenticated.record.revoke_reason = "logout"
    session.add(
        SecurityAudit(
            action="session_revoked",
            actor_account_id=authenticated.account.id,
            target_account_id=authenticated.account.id,
            scope="current_session",
        )
    )
    _commit_or_rollback(session)


def request_recovery(session: Session, email: str) -> None:
    started_at = time.monotonic()
    normalized_email = normalize_email(email)
    subject_hash = _attempt_subject(normalized_email)
    retry_after = _attempt_retry_after(
        session,
        "recovery_request",
        subject_hash,
        settings.recovery_request_max_attempts,
    )
    if retry_after is not None:
        _commit_or_rollback(session)
        raise RateLimited(retry_after)

    _record_attempt(session, "recovery_request", subject_hash)
    _queue_recovery_request(session, normalized_email)
    _commit_or_rollback(session)
    _normalize_recovery_duration(started_at)


def _exchange_one_time_credential(
    session: Session, token: str, purpose: str, password: str
) -> SessionResult:
    validate_password(password)
    subject_hash = _attempt_subject(token)
    retry_after = _attempt_retry_after(
        session,
        f"{purpose}_exchange",
        subject_hash,
        settings.recovery_exchange_max_attempts,
    )
    if retry_after is not None:
        _commit_or_rollback(session)
        raise RateLimited(retry_after)

    digest = _token_hash(token)
    credential = session.scalar(
        select(OneTimeCredential)
        .where(
            OneTimeCredential.purpose == purpose, OneTimeCredential.token_hash == digest
        )
        .with_for_update()
    )
    now = _now()
    if (
        credential is None
        or not hmac.compare_digest(credential.token_hash, digest)
        or credential.status != TOKEN_ACTIVE
        or credential.expires_at <= now
    ):
        _record_attempt(session, f"{purpose}_exchange", subject_hash)
        _commit_or_rollback(session)
        raise LinkUnavailable

    account = session.scalar(
        select(Account).where(Account.id == credential.account_id).with_for_update()
    )
    required_status = PENDING_ACTIVATION if purpose == ACTIVATION else ACTIVE
    if account is None or account.status != required_status:
        _record_attempt(session, f"{purpose}_exchange", subject_hash)
        _commit_or_rollback(session)
        raise LinkUnavailable

    consumed = session.execute(
        update(OneTimeCredential)
        .where(
            OneTimeCredential.id == credential.id,
            OneTimeCredential.status == TOKEN_ACTIVE,
            OneTimeCredential.expires_at > now,
        )
        .values(status=TOKEN_CONSUMED, consumed_at=now)
    )
    if consumed.rowcount != 1:
        _record_attempt(session, f"{purpose}_exchange", subject_hash)
        _commit_or_rollback(session)
        raise LinkUnavailable

    _set_password(session, account.id, password)
    if purpose == ACTIVATION:
        account.status = ACTIVE
    clear_outbox_envelopes(session, [credential.id])
    if purpose == RECOVERY:
        session.execute(
            update(SessionRecord)
            .where(
                SessionRecord.account_id == account.id,
                SessionRecord.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoke_reason="password_recovered")
        )
        clear_outbox_envelopes(
            session, _revoke_credentials(session, account.id, RECOVERY)
        )
        action = "password_recovered"
    else:
        action = "account_activated"
    result = _create_session(session, account)
    _clear_attempt(session, f"{purpose}_exchange", subject_hash)
    session.add(
        SecurityAudit(
            action=action, actor_account_id=account.id, target_account_id=account.id
        )
    )
    _commit_or_rollback(session)
    return result


def activate(session: Session, token: str, password: str) -> SessionResult:
    return _exchange_one_time_credential(session, token, ACTIVATION, password)


def recover_password(session: Session, token: str, password: str) -> SessionResult:
    return _exchange_one_time_credential(session, token, RECOVERY, password)


def provision_account(session: Session, email: str, operator: str, reason: str) -> UUID:
    normalized_email = normalize_email(email)
    account = session.scalar(
        select(Account).where(Account.email == normalized_email).with_for_update()
    )
    if account is None:
        account = Account(email=normalized_email, status=PENDING_ACTIVATION)
        session.add(account)
        session.flush()
    elif account.status == ACTIVE:
        session.rollback()
        raise ValueError("账户已经可用，不能再次开通")
    else:
        account.status = PENDING_ACTIVATION
        session.execute(
            update(SessionRecord)
            .where(
                SessionRecord.account_id == account.id,
                SessionRecord.revoked_at.is_(None),
            )
            .values(revoked_at=_now(), revoke_reason="reprovisioned")
        )
        clear_outbox_envelopes(
            session, _revoke_credentials(session, account.id, RECOVERY)
        )
    _create_one_time_credential(session, account, ACTIVATION)
    session.add(
        SecurityAudit(
            action="account_provisioned",
            target_account_id=account.id,
            operator=operator,
            reason=reason,
            scope="identity_provisioning",
        )
    )
    _commit_or_rollback(session)
    return account.id


def diagnose_outbox(
    session: Session, outbox_id: UUID, operator: str, reason: str
) -> dict[str, str | int]:
    outbox = session.get(MailOutbox, outbox_id)
    if outbox is None:
        session.rollback()
        raise ValueError("未找到邮件记录")
    session.add(
        SecurityAudit(
            action="outbox_diagnosed",
            target_account_id=outbox.recipient_account_id,
            operator=operator,
            reason=reason,
            scope=f"outbox:{outbox.id}",
        )
    )
    _commit_or_rollback(session)
    return {
        "id": str(outbox.id),
        "purpose": outbox.purpose,
        "status": outbox.status,
        "attemptCount": outbox.attempt_count,
    }
