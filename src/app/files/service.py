import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import (
    set_actor,
    set_file_lifecycle_scope,
    set_file_recovery_work_scope,
)
from app.files import storage
from app.files.models import StoredFile
from app.files.policy import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_BYTES_PER_WORK,
    MAX_IMAGE_PIXELS,
    MAX_IMAGES_PER_WORK,
    PENDING_IMAGE_TTL,
    UPLOAD_CHUNK_SIZE,
)
from app.works import service as works_service

_FORMAT_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ImageTypeNotAllowed(Exception):
    pass


class ImageLimitExceeded(Exception):
    pass


class ImageUnavailable(Exception):
    pass


class FileOperationRetryable(Exception):
    pass


@dataclass(frozen=True)
class ImageData:
    id: UUID
    display_name: str
    detected_content_type: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class ImageLimits:
    allowed_content_types: tuple[str, ...]
    max_bytes: int
    max_pixels: int
    max_count: int
    max_total_bytes: int


@dataclass
class UploadedImage:
    display_name: str
    declared_content_type: str
    detected_content_type: str
    size_bytes: int
    digest: bytes
    data: BinaryIO

    def close(self) -> None:
        self.data.close()


@dataclass(frozen=True)
class ImageStream:
    image: ImageData
    body: BinaryIO


def _now() -> datetime:
    return datetime.now(UTC)


def _commit_or_rollback(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _image_data(file: StoredFile) -> ImageData:
    return ImageData(
        id=file.id,
        display_name=file.display_name,
        detected_content_type=file.detected_content_type,
        size_bytes=file.size_bytes,
        created_at=file.created_at,
    )


def image_limits() -> ImageLimits:
    return ImageLimits(
        allowed_content_types=tuple(sorted(ALLOWED_IMAGE_TYPES)),
        max_bytes=MAX_IMAGE_BYTES,
        max_pixels=MAX_IMAGE_PIXELS,
        max_count=MAX_IMAGES_PER_WORK,
        max_total_bytes=MAX_IMAGE_BYTES_PER_WORK,
    )


def _safe_display_name(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    name = "".join(character for character in name if character.isprintable()).strip()
    return (name or "image")[:160]


def _read_image(
    source: BinaryIO, filename: str, declared_content_type: str | None
) -> UploadedImage:
    data = SpooledTemporaryFile(max_size=UPLOAD_CHUNK_SIZE, mode="w+b")
    digest = sha256()
    size_bytes = 0
    try:
        while chunk := source.read(UPLOAD_CHUNK_SIZE):
            size_bytes += len(chunk)
            if size_bytes > MAX_IMAGE_BYTES:
                raise ImageLimitExceeded
            digest.update(chunk)
            data.write(chunk)
        data.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(data) as image:
                image.verify()
            data.seek(0)
            with Image.open(data) as image:
                width, height = image.size
                detected_content_type = _FORMAT_TYPES.get(image.format or "")
                if (
                    detected_content_type not in ALLOWED_IMAGE_TYPES
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise ImageTypeNotAllowed
                image.load()
        data.seek(0)
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
    ) as error:
        data.close()
        raise ImageTypeNotAllowed from error
    except Exception:
        data.close()
        raise
    return UploadedImage(
        display_name=_safe_display_name(filename),
        declared_content_type=(declared_content_type or "application/octet-stream")[
            :127
        ],
        detected_content_type=cast(str, detected_content_type),
        size_bytes=size_bytes,
        digest=digest.digest(),
        data=data,
    )


def _pending_records(
    session: Session, workspace_id: UUID, work_id: UUID
) -> list[StoredFile]:
    set_file_recovery_work_scope(session, work_id)
    cutoff = _now() - PENDING_IMAGE_TTL
    return list(
        session.scalars(
            select(StoredFile)
            .where(
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.status == "pending",
                StoredFile.created_at <= cutoff,
            )
            .order_by(StoredFile.created_at, StoredFile.id)
        )
    )


def _mark_failed(session: Session, file_id: UUID) -> bool:
    try:
        set_file_lifecycle_scope(session, file_id)
        updated = session.execute(
            update(StoredFile)
            .where(StoredFile.id == file_id, StoredFile.status == "pending")
            .values(status="failed", completed_at=func.now())
            .returning(StoredFile.id)
        ).scalar_one_or_none()
        _commit_or_rollback(session)
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    return updated is not None


def _mark_ready(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID, file_id: UUID
) -> bool:
    try:
        set_actor(session, actor_id)
        works_service.ensure_work_access(session, actor_id, workspace_id, work_id)
        set_file_lifecycle_scope(session, file_id)
        updated = session.execute(
            update(StoredFile)
            .where(StoredFile.id == file_id, StoredFile.status == "pending")
            .values(status="ready", completed_at=func.now())
            .returning(StoredFile.id)
        ).scalar_one_or_none()
        if updated is None:
            session.rollback()
            return False
        _commit_or_rollback(session)
    except works_service.WorkUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    return True


def _stored_upload(file: StoredFile) -> UploadedImage | None:
    try:
        body = storage.open_object(file.object_key)
    except storage.StorageUnavailable as error:
        raise FileOperationRetryable from error
    if body is None:
        return None
    try:
        return _read_image(body, file.display_name, file.declared_content_type)
    except (ImageLimitExceeded, ImageTypeNotAllowed):
        return None
    finally:
        body.close()


def _matches(file: StoredFile, actual: UploadedImage) -> bool:
    return (
        actual.detected_content_type == file.detected_content_type
        and actual.size_bytes == file.size_bytes
        and actual.digest == file.sha256
    )


def _reconcile_pending(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
) -> None:
    try:
        set_actor(session, actor_id)
        works_service.lock_work_for_files(session, actor_id, workspace_id, work_id)
        set_file_recovery_work_scope(session, work_id)
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.status == "pending",
            )
        )
        if file is None:
            session.rollback()
            return
        actual = _stored_upload(file)
        try:
            matches = actual is not None and _matches(file, actual)
        finally:
            if actual is not None:
                actual.close()
        if not matches:
            try:
                storage.delete_object(file.object_key)
            except storage.StorageUnavailable as error:
                raise FileOperationRetryable from error
            _mark_failed(session, file.id)
            return
        try:
            _mark_ready(session, actor_id, workspace_id, work_id, file.id)
        except works_service.WorkUnavailable:
            if _mark_failed(session, file.id):
                try:
                    storage.delete_object(file.object_key)
                except storage.StorageUnavailable:
                    pass
            raise
    except works_service.WorkUnavailable:
        raise
    except FileOperationRetryable:
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error


def _recover_pending(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> None:
    try:
        set_actor(session, actor_id)
        works_service.ensure_work_access(session, actor_id, workspace_id, work_id)
        file_ids = [
            file.id for file in _pending_records(session, workspace_id, work_id)
        ]
    except works_service.WorkUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    session.rollback()
    for file_id in file_ids:
        _reconcile_pending(session, actor_id, workspace_id, work_id, file_id)


def _reserve_file(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    upload: UploadedImage,
) -> StoredFile:
    try:
        set_actor(session, actor_id)
        works_service.lock_work_for_files(session, actor_id, workspace_id, work_id)
        set_file_recovery_work_scope(session, work_id)
        count, total = session.execute(
            select(
                func.count(StoredFile.id),
                func.coalesce(func.sum(StoredFile.size_bytes), 0),
            ).where(
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.status.in_(("pending", "ready")),
            )
        ).one()
        if (
            count >= MAX_IMAGES_PER_WORK
            or total + upload.size_bytes > MAX_IMAGE_BYTES_PER_WORK
        ):
            session.rollback()
            raise ImageLimitExceeded
        file = StoredFile(
            id=uuid4(),
            workspace_id=workspace_id,
            work_id=work_id,
            uploader_account_id=actor_id,
            display_name=upload.display_name,
            declared_content_type=upload.declared_content_type,
            detected_content_type=upload.detected_content_type,
            size_bytes=upload.size_bytes,
            sha256=upload.digest,
            object_key=f"works/{workspace_id}/{work_id}/{uuid4()}",
            status="pending",
        )
        set_file_lifecycle_scope(session, file.id)
        session.add(file)
        session.flush()
        _commit_or_rollback(session)
    except (ImageLimitExceeded, works_service.WorkUnavailable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    return file


def upload_image(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    source: BinaryIO,
    filename: str,
    declared_content_type: str | None,
) -> ImageData:
    upload = _read_image(source, filename, declared_content_type)
    try:
        _recover_pending(session, actor_id, workspace_id, work_id)
        file = _reserve_file(session, actor_id, workspace_id, work_id, upload)
        try:
            upload.data.seek(0)
            storage.put_object(
                file.object_key, upload.data, upload.detected_content_type
            )
        except storage.StorageUnavailable as error:
            raise FileOperationRetryable from error
        actual = _stored_upload(file)
        try:
            matches = actual is not None and _matches(file, actual)
        finally:
            if actual is not None:
                actual.close()
        if not matches:
            try:
                storage.delete_object(file.object_key)
            except storage.StorageUnavailable as error:
                raise FileOperationRetryable from error
            _mark_failed(session, file.id)
            raise FileOperationRetryable
        try:
            _mark_ready(session, actor_id, workspace_id, work_id, file.id)
        except works_service.WorkUnavailable:
            try:
                storage.delete_object(file.object_key)
            except storage.StorageUnavailable:
                pass
            _mark_failed(session, file.id)
            raise
        return _image_data(file)
    finally:
        upload.close()


def list_images(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    page: int,
    size: int,
) -> tuple[list[ImageData], int]:
    try:
        set_actor(session, actor_id)
        works_service.ensure_work_access(session, actor_id, workspace_id, work_id)
        total = (
            session.scalar(
                select(func.count())
                .select_from(StoredFile)
                .where(
                    StoredFile.workspace_id == workspace_id,
                    StoredFile.work_id == work_id,
                    StoredFile.status == "ready",
                )
            )
            or 0
        )
        files = list(
            session.scalars(
                select(StoredFile)
                .where(
                    StoredFile.workspace_id == workspace_id,
                    StoredFile.work_id == work_id,
                    StoredFile.status == "ready",
                )
                .order_by(StoredFile.created_at.desc(), StoredFile.id)
                .offset((page - 1) * size)
                .limit(size)
            )
        )
    except works_service.WorkUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    return [_image_data(file) for file in files], total


def open_image(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
) -> ImageStream:
    try:
        set_actor(session, actor_id)
        works_service.ensure_work_access(session, actor_id, workspace_id, work_id)
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.status == "ready",
            )
        )
    except works_service.WorkUnavailable:
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    if file is None:
        session.rollback()
        raise ImageUnavailable
    image = _image_data(file)
    object_key = file.object_key
    session.rollback()
    try:
        body = storage.open_object(object_key)
    except storage.StorageUnavailable as error:
        raise FileOperationRetryable from error
    if body is None:
        raise FileOperationRetryable
    return ImageStream(image=image, body=body)
