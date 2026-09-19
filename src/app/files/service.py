import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import (
    set_actor,
    set_file_lifecycle_scope,
    set_file_recovery_work_scope,
)
from app.files import materials, storage
from app.files.models import StoredFile
from app.files.policy import (
    ALLOWED_IMAGE_TYPES,
    ALLOWED_MATERIAL_TYPES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_BYTES_PER_WORK,
    MAX_IMAGE_PIXELS,
    MAX_IMAGES_PER_WORK,
    MAX_MATERIAL_BYTES,
    MAX_MATERIAL_BYTES_PER_WORK,
    MAX_MATERIALS_PER_WORK,
    PENDING_IMAGE_TTL,
    PENDING_MATERIAL_TTL,
    UPLOAD_CHUNK_SIZE,
)
from app.works import service as works_service
from app.workspaces import service as workspaces_service

_FORMAT_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ImageTypeNotAllowed(Exception):
    pass


class ImageLimitExceeded(Exception):
    pass


class ImageUnavailable(Exception):
    pass


class MaterialTypeNotAllowed(Exception):
    pass


class MaterialLimitExceeded(Exception):
    pass


class MaterialUnavailable(Exception):
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


@dataclass(frozen=True)
class MaterialLimits:
    allowed_content_types: tuple[str, ...]
    max_bytes: int
    max_count: int
    max_total_bytes: int


@dataclass
class UploadedFile:
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
    image: ImageData | materials.MaterialData
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


def material_limits() -> MaterialLimits:
    return MaterialLimits(
        allowed_content_types=tuple(sorted(ALLOWED_MATERIAL_TYPES)),
        max_bytes=MAX_MATERIAL_BYTES,
        max_count=MAX_MATERIALS_PER_WORK,
        max_total_bytes=MAX_MATERIAL_BYTES_PER_WORK,
    )


def _safe_display_name(filename: str, fallback: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    name = "".join(character for character in name if character.isprintable()).strip()
    return (name or fallback)[:160]


def _spool_upload(source: BinaryIO, max_bytes: int) -> tuple[BinaryIO, int, bytes]:
    data = SpooledTemporaryFile(max_size=UPLOAD_CHUNK_SIZE, mode="w+b")
    digest = sha256()
    size_bytes = 0
    try:
        while chunk := source.read(UPLOAD_CHUNK_SIZE):
            size_bytes += len(chunk)
            if size_bytes > max_bytes:
                raise ValueError
            digest.update(chunk)
            data.write(chunk)
        data.seek(0)
    except Exception:
        data.close()
        raise
    return data, size_bytes, digest.digest()


def _validate_image(data: BinaryIO) -> str:
    try:
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
        raise ImageTypeNotAllowed from error
    return cast(str, detected_content_type)


def _uploaded_file(
    data: BinaryIO,
    size_bytes: int,
    digest: bytes,
    filename: str,
    declared_content_type: str | None,
    detected_content_type: str,
    fallback: str,
) -> UploadedFile:
    return UploadedFile(
        display_name=_safe_display_name(filename, fallback),
        declared_content_type=(declared_content_type or "application/octet-stream")[
            :127
        ],
        detected_content_type=detected_content_type,
        size_bytes=size_bytes,
        digest=digest,
        data=data,
    )


def _read_image(
    source: BinaryIO, filename: str, declared_content_type: str | None
) -> UploadedFile:
    try:
        data, size_bytes, digest = _spool_upload(source, MAX_IMAGE_BYTES)
    except ValueError as error:
        raise ImageLimitExceeded from error
    try:
        return _uploaded_file(
            data,
            size_bytes,
            digest,
            filename,
            declared_content_type,
            _validate_image(data),
            "image",
        )
    except Exception:
        data.close()
        raise


def _read_material(
    source: BinaryIO, filename: str, declared_content_type: str | None
) -> UploadedFile:
    try:
        data, size_bytes, digest = _spool_upload(source, MAX_MATERIAL_BYTES)
    except ValueError as error:
        raise MaterialLimitExceeded from error
    try:
        if data.read(5) == b"%PDF-":
            data.seek(0)
            reader = PdfReader(data, strict=True)
            if reader.is_encrypted or not reader.pages:
                raise MaterialTypeNotAllowed
            len(reader.pages)
            detected_content_type = "application/pdf"
        else:
            detected_content_type = _validate_image(data)
        data.seek(0)
        return _uploaded_file(
            data,
            size_bytes,
            digest,
            filename,
            declared_content_type,
            detected_content_type,
            "material",
        )
    except (ImageTypeNotAllowed, PyPdfError, OSError, ValueError) as error:
        data.close()
        raise MaterialTypeNotAllowed from error
    except Exception:
        data.close()
        raise


def _pending_records(
    session: Session, workspace_id: UUID, work_id: UUID, kind: str
) -> list[StoredFile]:
    set_file_recovery_work_scope(session, work_id)
    ttl = PENDING_MATERIAL_TTL if kind == "material" else PENDING_IMAGE_TTL
    cutoff = _now() - ttl
    return list(
        session.scalars(
            select(StoredFile)
            .where(
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.kind == kind,
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
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
    manager_required: bool,
) -> bool:
    try:
        set_actor(session, actor_id)
        if manager_required:
            works_service.ensure_work_management(
                session, actor_id, workspace_id, work_id
            )
        else:
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
    except (works_service.WorkManagementForbidden, works_service.WorkUnavailable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    return True


def _stored_upload(file: StoredFile) -> UploadedFile | None:
    try:
        body = storage.open_object(file.object_key)
    except storage.StorageUnavailable as error:
        raise FileOperationRetryable from error
    if body is None:
        return None
    try:
        reader = _read_material if file.kind == "material" else _read_image
        return reader(body, file.display_name, file.declared_content_type)
    except (
        ImageLimitExceeded,
        ImageTypeNotAllowed,
        MaterialLimitExceeded,
        MaterialTypeNotAllowed,
    ):
        return None
    finally:
        body.close()


def _matches(file: StoredFile, actual: UploadedFile) -> bool:
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
    kind: str = "image",
) -> None:
    manager_required = kind == "material"
    try:
        set_actor(session, actor_id)
        if manager_required:
            works_service.ensure_work_management(
                session, actor_id, workspace_id, work_id
            )
        works_service.lock_work_for_files(session, actor_id, workspace_id, work_id)
        set_file_recovery_work_scope(session, work_id)
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.kind == kind,
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
            _mark_ready(
                session,
                actor_id,
                workspace_id,
                work_id,
                file.id,
                manager_required,
            )
        except (works_service.WorkManagementForbidden, works_service.WorkUnavailable):
            if _mark_failed(session, file.id):
                try:
                    storage.delete_object(file.object_key)
                except storage.StorageUnavailable:
                    pass
            raise
    except (works_service.WorkManagementForbidden, works_service.WorkUnavailable):
        raise
    except FileOperationRetryable:
        session.rollback()
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error


def _recover_pending(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    kind: str = "image",
) -> None:
    manager_required = kind == "material"
    try:
        set_actor(session, actor_id)
        if manager_required:
            works_service.ensure_work_management(
                session, actor_id, workspace_id, work_id
            )
        else:
            works_service.ensure_work_access(session, actor_id, workspace_id, work_id)
        file_ids = [
            file.id for file in _pending_records(session, workspace_id, work_id, kind)
        ]
    except (works_service.WorkManagementForbidden, works_service.WorkUnavailable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    session.rollback()
    for file_id in file_ids:
        _reconcile_pending(session, actor_id, workspace_id, work_id, file_id, kind)


def _reserve_file(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    upload: UploadedFile,
    *,
    kind: str = "image",
    max_count: int = MAX_IMAGES_PER_WORK,
    max_total_bytes: int = MAX_IMAGE_BYTES_PER_WORK,
) -> StoredFile:
    manager_required = kind == "material"
    try:
        set_actor(session, actor_id)
        if manager_required:
            works_service.ensure_work_management(
                session, actor_id, workspace_id, work_id
            )
        works_service.lock_work_for_files(session, actor_id, workspace_id, work_id)
        set_file_recovery_work_scope(session, work_id)
        count, total = session.execute(
            select(
                func.count(StoredFile.id),
                func.coalesce(func.sum(StoredFile.size_bytes), 0),
            ).where(
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.kind == kind,
                StoredFile.status.in_(("pending", "ready")),
            )
        ).one()
        if count >= max_count or total + upload.size_bytes > max_total_bytes:
            session.rollback()
            if kind == "material":
                raise MaterialLimitExceeded
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
            kind=kind,
        )
        set_file_lifecycle_scope(session, file.id)
        session.add(file)
        session.flush()
        _commit_or_rollback(session)
    except (
        ImageLimitExceeded,
        MaterialLimitExceeded,
        works_service.WorkManagementForbidden,
        works_service.WorkUnavailable,
    ):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    return file


def _upload_file(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    source: BinaryIO,
    filename: str,
    declared_content_type: str | None,
    *,
    kind: str,
) -> StoredFile:
    set_actor(session, actor_id)
    workspaces_service.ensure_workspace_writable(session, workspace_id)
    reader = _read_material if kind == "material" else _read_image
    upload = reader(source, filename, declared_content_type)
    manager_required = kind == "material"
    try:
        _recover_pending(session, actor_id, workspace_id, work_id, kind)
        file = _reserve_file(
            session,
            actor_id,
            workspace_id,
            work_id,
            upload,
            kind=kind,
            max_count=MAX_MATERIALS_PER_WORK
            if manager_required
            else MAX_IMAGES_PER_WORK,
            max_total_bytes=(
                MAX_MATERIAL_BYTES_PER_WORK
                if manager_required
                else MAX_IMAGE_BYTES_PER_WORK
            ),
        )
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
            _mark_ready(
                session,
                actor_id,
                workspace_id,
                work_id,
                file.id,
                manager_required,
            )
        except (works_service.WorkManagementForbidden, works_service.WorkUnavailable):
            try:
                storage.delete_object(file.object_key)
            except storage.StorageUnavailable:
                pass
            _mark_failed(session, file.id)
            raise
        return file
    finally:
        upload.close()


def upload_image(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    source: BinaryIO,
    filename: str,
    declared_content_type: str | None,
) -> ImageData:
    return _image_data(
        _upload_file(
            session,
            actor_id,
            workspace_id,
            work_id,
            source,
            filename,
            declared_content_type,
            kind="image",
        )
    )


def upload_material(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    source: BinaryIO,
    filename: str,
    declared_content_type: str | None,
) -> materials.MaterialData:
    return materials.material_data(
        _upload_file(
            session,
            actor_id,
            workspace_id,
            work_id,
            source,
            filename,
            declared_content_type,
            kind="material",
        )
    )


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
                    StoredFile.kind == "image",
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
                    StoredFile.kind == "image",
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


def list_materials(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
) -> list[materials.MaterialData]:
    try:
        set_actor(session, actor_id)
        works_service.ensure_work_management(session, actor_id, workspace_id, work_id)
        files = list(
            session.scalars(
                select(StoredFile)
                .where(
                    StoredFile.workspace_id == workspace_id,
                    StoredFile.work_id == work_id,
                    StoredFile.kind == "material",
                    StoredFile.status == "ready",
                )
                .order_by(StoredFile.created_at, StoredFile.id)
            )
        )
    except (works_service.WorkManagementForbidden, works_service.WorkUnavailable):
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    return [materials.material_data(file) for file in files]


def _open_file(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
    kind: str,
) -> ImageStream:
    try:
        set_actor(session, actor_id)
        works_service.ensure_work_access(session, actor_id, workspace_id, work_id)
        if kind == "material" and materials.is_current_material(
            session, work_id, file_id
        ):
            set_file_lifecycle_scope(session, file_id)
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.kind == kind,
                StoredFile.status == "ready",
            )
        )
    except works_service.WorkUnavailable:
        if kind == "material":
            raise MaterialUnavailable
        raise
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    if file is None:
        session.rollback()
        if kind == "material":
            raise MaterialUnavailable
        raise ImageUnavailable
    data = materials.material_data(file) if kind == "material" else _image_data(file)
    object_key = file.object_key
    session.rollback()
    try:
        body = storage.open_object(object_key)
    except storage.StorageUnavailable as error:
        raise FileOperationRetryable from error
    if body is None:
        raise FileOperationRetryable
    return ImageStream(image=data, body=body)


def open_image(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
) -> ImageStream:
    return _open_file(session, actor_id, workspace_id, work_id, file_id, "image")


def open_material(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
) -> ImageStream:
    return _open_file(session, actor_id, workspace_id, work_id, file_id, "material")


def open_playtest_material(session: Session, file_id: UUID) -> ImageStream:
    """仅供已验证场次关系调用，按精确材料 ID 打开私有对象。"""
    try:
        set_file_lifecycle_scope(session, file_id)
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.kind == "material",
                StoredFile.status == "ready",
            )
        )
    except SQLAlchemyError as error:
        session.rollback()
        raise FileOperationRetryable from error
    if file is None:
        session.rollback()
        raise MaterialUnavailable
    data = materials.material_data(file)
    object_key = file.object_key
    session.rollback()
    try:
        body = storage.open_object(object_key)
    except storage.StorageUnavailable as error:
        raise FileOperationRetryable from error
    if body is None:
        raise FileOperationRetryable
    return ImageStream(image=data, body=body)
