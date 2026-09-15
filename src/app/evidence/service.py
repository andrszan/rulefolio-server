from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.models import PlaytestObservation
from app.identity.models import Account

OBSERVATION_KINDS = {"fact", "organizer_interpretation", "temporary_variant"}


class ObservationInvalid(Exception):
    pass


class ObservationUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ObservationData:
    id: UUID
    kind: str
    content: str
    recorded_by_account_id: UUID
    recorded_by_email: str
    recorded_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _data(item: PlaytestObservation, recorded_by_email: str) -> ObservationData:
    return ObservationData(
        id=item.id,
        kind=item.kind,
        content=item.content,
        recorded_by_account_id=item.recorded_by_account_id,
        recorded_by_email=recorded_by_email,
        recorded_at=item.recorded_at,
    )


def _values(kind: str, content: str) -> tuple[str, str]:
    content = content.strip()
    if kind not in OBSERVATION_KINDS or not content:
        raise ObservationInvalid
    return kind, content


def _account_email(session: Session, account_id: UUID) -> str:
    email = session.scalar(select(Account.email).where(Account.id == account_id))
    if email is None:
        raise ObservationUnavailable
    return email


def list_observations(
    session: Session, session_id: UUID
) -> tuple[ObservationData, ...]:
    return tuple(
        _data(item, email)
        for item, email in session.execute(
            select(PlaytestObservation, Account.email)
            .join(Account, Account.id == PlaytestObservation.recorded_by_account_id)
            .where(PlaytestObservation.session_id == session_id)
            .order_by(
                PlaytestObservation.kind,
                PlaytestObservation.recorded_at,
                PlaytestObservation.id,
            )
        )
    )


def create_observation(
    session: Session,
    session_id: UUID,
    actor_id: UUID,
    kind: str,
    content: str,
) -> ObservationData:
    kind, content = _values(kind, content)
    item = PlaytestObservation(
        session_id=session_id,
        kind=kind,
        content=content,
        recorded_by_account_id=actor_id,
    )
    session.add(item)
    session.flush()
    return _data(item, _account_email(session, actor_id))


def update_observation(
    session: Session,
    session_id: UUID,
    observation_id: UUID,
    actor_id: UUID,
    kind: str,
    content: str,
) -> ObservationData:
    kind, content = _values(kind, content)
    item = session.scalar(
        select(PlaytestObservation)
        .where(
            PlaytestObservation.id == observation_id,
            PlaytestObservation.session_id == session_id,
        )
        .with_for_update()
    )
    if item is None:
        raise ObservationUnavailable
    item.kind = kind
    item.content = content
    item.recorded_by_account_id = actor_id
    item.recorded_at = _now()
    session.flush()
    return _data(item, _account_email(session, actor_id))
