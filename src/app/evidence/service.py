from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.access.context import set_feedback_answer_item_lock_scope
from app.evidence.models import (
    PlaytestFeedbackAnswer,
    PlaytestFeedbackItem,
    PlaytestFeedbackOption,
    PlaytestFeedbackSubmission,
    PlaytestObservation,
)
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


FEEDBACK_ITEM_KINDS = {"short_text", "single_choice", "number"}
FEEDBACK_SOURCES = {
    "direct",
    "oral_discussion",
    "paper_record",
    "organizer_observation",
    "temporary_alias",
}
FEEDBACK_STATUSES = {"draft", "submitted"}


class FeedbackInvalid(Exception):
    pass


class FeedbackItemUnavailable(Exception):
    pass


class FeedbackItemLocked(Exception):
    pass


class FeedbackItemRevisionConflict(Exception):
    pass


class FeedbackSubmissionUnavailable(Exception):
    pass


class FeedbackSubmissionForbidden(Exception):
    pass


class FeedbackSubmissionRevisionConflict(Exception):
    pass


class FeedbackOperationConflict(Exception):
    pass


@dataclass(frozen=True)
class FeedbackOptionData:
    id: UUID
    label: str
    position: int


@dataclass(frozen=True)
class FeedbackItemData:
    id: UUID
    kind: str
    question: str
    revision: int
    is_locked: bool
    options: tuple[FeedbackOptionData, ...]


@dataclass(frozen=True)
class FeedbackAnswerData:
    item_id: UUID
    kind: str
    text_value: str | None
    option_id: UUID | None
    number_value: float | None


@dataclass(frozen=True)
class FeedbackSubmissionData:
    id: UUID
    source: str
    temporary_alias: str | None
    status: str
    revision: int
    recorded_by_account_id: UUID
    recorded_by_email: str
    updated_at: datetime
    answers: tuple[FeedbackAnswerData, ...]


@dataclass(frozen=True)
class FeedbackData:
    items: tuple[FeedbackItemData, ...]
    submissions: tuple[FeedbackSubmissionData, ...]


@dataclass(frozen=True)
class FeedbackItemDraft:
    kind: str
    question: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class FeedbackAnswerDraft:
    item_id: UUID
    text_value: str | None = None
    option_id: UUID | None = None
    number_value: float | None = None


@dataclass(frozen=True)
class FeedbackSubmissionDraft:
    source: str
    temporary_alias: str | None
    status: str
    answers: tuple[FeedbackAnswerDraft, ...]


def _required_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise FeedbackInvalid
    value = value.strip()
    if not value or len(value) > limit:
        raise FeedbackInvalid
    return value


def _optional_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FeedbackInvalid
    value = value.strip()
    if len(value) > limit:
        raise FeedbackInvalid
    return value or None


def _item_draft(draft: FeedbackItemDraft) -> FeedbackItemDraft:
    if draft.kind not in FEEDBACK_ITEM_KINDS:
        raise FeedbackInvalid
    question = _required_text(draft.question, 4_000)
    options = tuple(_required_text(option, 1_000) for option in draft.options)
    if draft.kind == "single_choice":
        if len(options) < 2 or len(set(options)) != len(options):
            raise FeedbackInvalid
    elif options:
        raise FeedbackInvalid
    return FeedbackItemDraft(kind=draft.kind, question=question, options=options)


def _item_data(session: Session, item: PlaytestFeedbackItem) -> FeedbackItemData:
    options = tuple(
        FeedbackOptionData(id=option.id, label=option.label, position=option.position)
        for option in session.scalars(
            select(PlaytestFeedbackOption)
            .where(
                PlaytestFeedbackOption.session_id == item.session_id,
                PlaytestFeedbackOption.item_id == item.id,
            )
            .order_by(PlaytestFeedbackOption.position, PlaytestFeedbackOption.id)
        )
    )
    return FeedbackItemData(
        id=item.id,
        kind=item.kind,
        question=item.question,
        revision=item.revision,
        is_locked=item.is_locked,
        options=options,
    )


def _answer_data(answer: PlaytestFeedbackAnswer) -> FeedbackAnswerData:
    return FeedbackAnswerData(
        item_id=answer.item_id,
        kind=answer.kind,
        text_value=answer.text_value,
        option_id=answer.option_id,
        number_value=answer.number_value,
    )


def _submission_data(
    submission: PlaytestFeedbackSubmission,
    recorded_by_email: str,
    answers: tuple[FeedbackAnswerData, ...],
) -> FeedbackSubmissionData:
    return FeedbackSubmissionData(
        id=submission.id,
        source=submission.source,
        temporary_alias=submission.temporary_alias,
        status=submission.status,
        revision=submission.revision,
        recorded_by_account_id=submission.recorded_by_account_id,
        recorded_by_email=recorded_by_email,
        updated_at=submission.updated_at,
        answers=answers,
    )


def list_feedback(session: Session, session_id: UUID) -> FeedbackData:
    items = list(
        session.scalars(
            select(PlaytestFeedbackItem)
            .where(PlaytestFeedbackItem.session_id == session_id)
            .order_by(PlaytestFeedbackItem.created_at, PlaytestFeedbackItem.id)
        )
    )
    submissions_with_email = list(
        session.execute(
            select(PlaytestFeedbackSubmission, Account.email)
            .join(
                Account, Account.id == PlaytestFeedbackSubmission.recorded_by_account_id
            )
            .where(PlaytestFeedbackSubmission.session_id == session_id)
            .order_by(
                PlaytestFeedbackSubmission.updated_at.desc(),
                PlaytestFeedbackSubmission.id.desc(),
            )
        )
    )
    submission_ids = [submission.id for submission, _ in submissions_with_email]
    answers_by_submission: dict[UUID, list[FeedbackAnswerData]] = {
        submission_id: [] for submission_id in submission_ids
    }
    if submission_ids:
        for answer in session.scalars(
            select(PlaytestFeedbackAnswer)
            .where(PlaytestFeedbackAnswer.submission_id.in_(submission_ids))
            .order_by(PlaytestFeedbackAnswer.item_id, PlaytestFeedbackAnswer.id)
        ):
            answers_by_submission[answer.submission_id].append(_answer_data(answer))
    return FeedbackData(
        items=tuple(_item_data(session, item) for item in items),
        submissions=tuple(
            _submission_data(
                submission, email, tuple(answers_by_submission[submission.id])
            )
            for submission, email in submissions_with_email
        ),
    )


def _load_item(
    session: Session, session_id: UUID, item_id: UUID, *, lock: bool
) -> PlaytestFeedbackItem:
    statement = select(PlaytestFeedbackItem).where(
        PlaytestFeedbackItem.id == item_id,
        PlaytestFeedbackItem.session_id == session_id,
    )
    if lock:
        statement = statement.with_for_update()
    item = session.scalar(statement)
    if item is None:
        raise FeedbackItemUnavailable
    return item


def create_feedback_item(
    session: Session,
    session_id: UUID,
    operation_key: str,
    draft: FeedbackItemDraft,
) -> FeedbackItemData:
    draft = _item_draft(draft)
    if not operation_key or len(operation_key) > 128:
        raise FeedbackInvalid
    existing = session.scalar(
        select(PlaytestFeedbackItem)
        .where(
            PlaytestFeedbackItem.session_id == session_id,
            PlaytestFeedbackItem.creation_operation_key == operation_key,
        )
        .with_for_update()
    )
    if existing is not None:
        current = _item_data(session, existing)
        if (
            current.kind == draft.kind
            and current.question == draft.question
            and tuple(option.label for option in current.options) == draft.options
        ):
            return current
        raise FeedbackOperationConflict
    item = PlaytestFeedbackItem(
        session_id=session_id,
        kind=draft.kind,
        question=draft.question,
        creation_operation_key=operation_key,
    )
    session.add(item)
    session.flush()
    session.add_all(
        PlaytestFeedbackOption(
            session_id=session_id, item_id=item.id, label=option, position=index
        )
        for index, option in enumerate(draft.options, start=1)
    )
    session.flush()
    return _item_data(session, item)


def update_feedback_item(
    session: Session,
    session_id: UUID,
    item_id: UUID,
    expected_revision: int,
    draft: FeedbackItemDraft,
) -> FeedbackItemData:
    draft = _item_draft(draft)
    if expected_revision <= 0:
        raise FeedbackInvalid
    item = _load_item(session, session_id, item_id, lock=True)
    if item.is_locked:
        raise FeedbackItemLocked
    if item.revision != expected_revision:
        raise FeedbackItemRevisionConflict
    item.kind = draft.kind
    item.question = draft.question
    item.revision += 1
    session.execute(
        delete(PlaytestFeedbackOption).where(
            PlaytestFeedbackOption.session_id == session_id,
            PlaytestFeedbackOption.item_id == item.id,
        )
    )
    session.add_all(
        PlaytestFeedbackOption(
            session_id=session_id, item_id=item.id, label=option, position=index
        )
        for index, option in enumerate(draft.options, start=1)
    )
    session.flush()
    return _item_data(session, item)


def delete_feedback_item(session: Session, session_id: UUID, item_id: UUID) -> None:
    item = _load_item(session, session_id, item_id, lock=True)
    if item.is_locked:
        raise FeedbackItemLocked
    session.execute(
        delete(PlaytestFeedbackOption).where(
            PlaytestFeedbackOption.session_id == session_id,
            PlaytestFeedbackOption.item_id == item.id,
        )
    )
    session.delete(item)
    session.flush()


def _validated_answers(
    session: Session,
    session_id: UUID,
    answers: tuple[FeedbackAnswerDraft, ...],
) -> tuple[FeedbackAnswerDraft, ...]:
    if not answers or len({answer.item_id for answer in answers}) != len(answers):
        raise FeedbackInvalid
    item_ids = {answer.item_id for answer in answers}
    items = {
        item.id: item
        for item in session.scalars(
            select(PlaytestFeedbackItem).where(
                PlaytestFeedbackItem.session_id == session_id,
                PlaytestFeedbackItem.id.in_(item_ids),
            )
        )
    }
    if len(items) != len(item_ids):
        raise FeedbackInvalid
    option_ids = {
        answer.option_id for answer in answers if answer.option_id is not None
    }
    options = {
        option.id: option
        for option in session.scalars(
            select(PlaytestFeedbackOption).where(
                PlaytestFeedbackOption.session_id == session_id,
                PlaytestFeedbackOption.id.in_(option_ids),
            )
        )
    }
    result: list[FeedbackAnswerDraft] = []
    for answer in answers:
        item = items[answer.item_id]
        if item.kind == "short_text":
            if answer.option_id is not None or answer.number_value is not None:
                raise FeedbackInvalid
            result.append(
                FeedbackAnswerDraft(
                    item.id, text_value=_required_text(answer.text_value, 4_000)
                )
            )
        elif item.kind == "single_choice":
            option = options.get(answer.option_id)
            if (
                answer.text_value is not None
                or answer.number_value is not None
                or option is None
                or option.item_id != item.id
                or option.session_id != session_id
            ):
                raise FeedbackInvalid
            result.append(FeedbackAnswerDraft(item.id, option_id=option.id))
        else:
            if (
                answer.text_value is not None
                or answer.option_id is not None
                or not isinstance(answer.number_value, (int, float))
                or isinstance(answer.number_value, bool)
                or not isfinite(answer.number_value)
            ):
                raise FeedbackInvalid
            result.append(
                FeedbackAnswerDraft(item.id, number_value=float(answer.number_value))
            )
    return tuple(result)


def _replace_answers(
    session: Session,
    submission: PlaytestFeedbackSubmission,
    answers: tuple[FeedbackAnswerDraft, ...],
) -> None:
    session.execute(
        delete(PlaytestFeedbackAnswer).where(
            PlaytestFeedbackAnswer.submission_id == submission.id,
            PlaytestFeedbackAnswer.session_id == submission.session_id,
        )
    )
    for answer in answers:
        set_feedback_answer_item_lock_scope(session, answer.item_id)
        session.add(
            PlaytestFeedbackAnswer(
                submission_id=submission.id,
                session_id=submission.session_id,
                item_id=answer.item_id,
                kind=(
                    "short_text"
                    if answer.text_value is not None
                    else "single_choice"
                    if answer.option_id is not None
                    else "number"
                ),
                text_value=answer.text_value,
                option_id=answer.option_id,
                number_value=answer.number_value,
                source=submission.source,
                direct_author_account_id=submission.direct_author_account_id,
                recorded_by_account_id=submission.recorded_by_account_id,
                status=submission.status,
            )
        )
        session.flush()


def _submission_with_answers(
    session: Session, submission: PlaytestFeedbackSubmission
) -> FeedbackSubmissionData:
    return _submission_data(
        submission,
        _account_email(session, submission.recorded_by_account_id),
        tuple(
            _answer_data(answer)
            for answer in session.scalars(
                select(PlaytestFeedbackAnswer)
                .where(PlaytestFeedbackAnswer.submission_id == submission.id)
                .order_by(PlaytestFeedbackAnswer.item_id, PlaytestFeedbackAnswer.id)
            )
        ),
    )


def _organizer_draft(draft: FeedbackSubmissionDraft) -> FeedbackSubmissionDraft:
    if draft.source not in FEEDBACK_SOURCES - {"direct"} or draft.status != "submitted":
        raise FeedbackInvalid
    alias = _optional_text(draft.temporary_alias, 160)
    if (draft.source == "temporary_alias") != (alias is not None):
        raise FeedbackInvalid
    return FeedbackSubmissionDraft(
        source=draft.source,
        temporary_alias=alias,
        status=draft.status,
        answers=draft.answers,
    )


def create_organizer_submission(
    session: Session,
    session_id: UUID,
    actor_id: UUID,
    operation_key: str,
    draft: FeedbackSubmissionDraft,
) -> FeedbackSubmissionData:
    draft = _organizer_draft(draft)
    answers = _validated_answers(session, session_id, draft.answers)
    if not operation_key or len(operation_key) > 128:
        raise FeedbackInvalid
    existing = session.scalar(
        select(PlaytestFeedbackSubmission)
        .where(
            PlaytestFeedbackSubmission.session_id == session_id,
            PlaytestFeedbackSubmission.creation_operation_key == operation_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.recorded_by_account_id != actor_id:
            raise FeedbackOperationConflict
        return _submission_with_answers(session, existing)
    submission = PlaytestFeedbackSubmission(
        session_id=session_id,
        source=draft.source,
        temporary_alias=draft.temporary_alias,
        recorded_by_account_id=actor_id,
        status="submitted",
        creation_operation_key=operation_key,
    )
    session.add(submission)
    session.flush()
    _replace_answers(session, submission, answers)
    return _submission_with_answers(session, submission)


def update_organizer_submission(
    session: Session,
    session_id: UUID,
    submission_id: UUID,
    actor_id: UUID,
    expected_revision: int,
    draft: FeedbackSubmissionDraft,
) -> FeedbackSubmissionData:
    draft = _organizer_draft(draft)
    answers = _validated_answers(session, session_id, draft.answers)
    if expected_revision <= 0:
        raise FeedbackInvalid
    submission = session.scalar(
        select(PlaytestFeedbackSubmission).where(
            PlaytestFeedbackSubmission.id == submission_id,
            PlaytestFeedbackSubmission.session_id == session_id,
        )
    )
    if submission is None:
        raise FeedbackSubmissionUnavailable
    if submission.source == "direct" or submission.recorded_by_account_id != actor_id:
        raise FeedbackSubmissionForbidden
    submission = session.scalar(
        select(PlaytestFeedbackSubmission)
        .where(
            PlaytestFeedbackSubmission.id == submission_id,
            PlaytestFeedbackSubmission.session_id == session_id,
        )
        .with_for_update()
    )
    if submission is None:
        raise FeedbackSubmissionForbidden
    if submission.revision != expected_revision:
        raise FeedbackSubmissionRevisionConflict
    submission.source = draft.source
    submission.temporary_alias = draft.temporary_alias
    submission.updated_at = _now()
    submission.revision += 1
    _replace_answers(session, submission, answers)
    return _submission_with_answers(session, submission)


def save_direct_submission(
    session: Session,
    session_id: UUID,
    actor_id: UUID,
    operation_key: str | None,
    expected_revision: int | None,
    draft: FeedbackSubmissionDraft,
) -> FeedbackSubmissionData:
    if draft.source != "direct" or draft.status not in FEEDBACK_STATUSES:
        raise FeedbackInvalid
    if draft.temporary_alias is not None:
        raise FeedbackInvalid
    answers = _validated_answers(session, session_id, draft.answers)
    submission = session.scalar(
        select(PlaytestFeedbackSubmission)
        .where(
            PlaytestFeedbackSubmission.session_id == session_id,
            PlaytestFeedbackSubmission.direct_author_account_id == actor_id,
        )
        .with_for_update()
    )
    if submission is None:
        if (
            expected_revision is not None
            or not operation_key
            or len(operation_key) > 128
        ):
            raise FeedbackInvalid
        submission = PlaytestFeedbackSubmission(
            session_id=session_id,
            source="direct",
            direct_author_account_id=actor_id,
            recorded_by_account_id=actor_id,
            status=draft.status,
            creation_operation_key=operation_key,
        )
        session.add(submission)
        session.flush()
        _replace_answers(session, submission, answers)
        return _submission_with_answers(session, submission)
    if expected_revision is None:
        if operation_key == submission.creation_operation_key:
            return _submission_with_answers(session, submission)
        raise FeedbackOperationConflict
    if expected_revision <= 0 or submission.revision != expected_revision:
        raise FeedbackSubmissionRevisionConflict
    submission.status = draft.status
    submission.updated_at = _now()
    submission.revision += 1
    _replace_answers(session, submission, answers)
    return _submission_with_answers(session, submission)
