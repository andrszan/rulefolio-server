from collections.abc import Iterator
from typing import Annotated, BinaryIO
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import api_error
from app.core.responses import ApiResponse, Page, PageParams
from app.files import service
from app.files.policy import UPLOAD_CHUNK_SIZE
from app.identity import service as identity_service
from app.identity.models import Account
from app.identity.router import _bearer_token
from app.works import service as works_service

router = APIRouter(tags=["files"])


class ImageResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    display_name: str = Field(serialization_alias="displayName")
    detected_content_type: str = Field(serialization_alias="detectedContentType")
    size_bytes: int = Field(serialization_alias="sizeBytes")
    created_at: str = Field(serialization_alias="createdAt")


class MaterialResponseData(ImageResponseData):
    pass


class ImageLimitsResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    allowed_content_types: list[str] = Field(serialization_alias="allowedContentTypes")
    max_bytes: int = Field(serialization_alias="maxBytes")
    max_pixels: int = Field(serialization_alias="maxPixels")
    max_count: int = Field(serialization_alias="maxCount")
    max_total_bytes: int = Field(serialization_alias="maxTotalBytes")


class ImagePageResponseData(Page[ImageResponseData]):
    limits: ImageLimitsResponseData


class MaterialLimitsResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    allowed_content_types: list[str] = Field(serialization_alias="allowedContentTypes")
    max_bytes: int = Field(serialization_alias="maxBytes")
    max_count: int = Field(serialization_alias="maxCount")
    max_total_bytes: int = Field(serialization_alias="maxTotalBytes")


class MaterialCandidatesResponseData(BaseModel):
    items: list[MaterialResponseData]
    limits: MaterialLimitsResponseData


def _authenticated_account(
    token: Annotated[str, Depends(_bearer_token)], session: Session = Depends(get_db)
) -> Account:
    try:
        return identity_service.authenticate(session, token).account
    except identity_service.SessionUnavailable as error:
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        ) from error


def _image_response(image: service.ImageData) -> ImageResponseData:
    return ImageResponseData(
        id=image.id,
        display_name=image.display_name,
        detected_content_type=image.detected_content_type,
        size_bytes=image.size_bytes,
        created_at=image.created_at.isoformat(),
    )


def _limits_response(limits: service.ImageLimits) -> ImageLimitsResponseData:
    return ImageLimitsResponseData(
        allowed_content_types=list(limits.allowed_content_types),
        max_bytes=limits.max_bytes,
        max_pixels=limits.max_pixels,
        max_count=limits.max_count,
        max_total_bytes=limits.max_total_bytes,
    )


def _material_response(
    material: service.materials.MaterialData,
) -> MaterialResponseData:
    return MaterialResponseData(
        id=material.id,
        display_name=material.display_name,
        detected_content_type=material.detected_content_type,
        size_bytes=material.size_bytes,
        created_at=material.created_at.isoformat(),
    )


def _material_limits_response(
    limits: service.MaterialLimits,
) -> MaterialLimitsResponseData:
    return MaterialLimitsResponseData(
        allowed_content_types=list(limits.allowed_content_types),
        max_bytes=limits.max_bytes,
        max_count=limits.max_count,
        max_total_bytes=limits.max_total_bytes,
    )


def _file_error(error: Exception) -> None:
    if isinstance(error, works_service.WorkUnavailable):
        raise api_error(404, "作品不可用", "work_unavailable") from error
    if isinstance(error, works_service.WorkOperationRetryable):
        raise api_error(
            503,
            "当前操作暂时无法完成，请重试。",
            "file_operation_retryable",
            {"Retry-After": "1"},
        ) from error
    if isinstance(error, works_service.WorkManagementForbidden):
        raise api_error(
            403, "当前会话不能管理该作品", "work_management_forbidden"
        ) from error
    if isinstance(error, service.ImageTypeNotAllowed):
        raise api_error(
            422, "只支持可安全解码的 JPEG、PNG 或 WebP 图片", "image_type_not_allowed"
        ) from error
    if isinstance(error, service.ImageLimitExceeded):
        raise api_error(
            422, "图片超过当前作品的上传限制", "image_limit_exceeded"
        ) from error
    if isinstance(error, service.ImageUnavailable):
        raise api_error(404, "图片不可用", "image_unavailable") from error
    if isinstance(error, service.MaterialTypeNotAllowed):
        raise api_error(
            422,
            "只支持可读取的 PDF、JPEG、PNG 或 WebP 材料",
            "material_type_not_allowed",
        ) from error
    if isinstance(error, service.MaterialLimitExceeded):
        raise api_error(
            422, "材料超过当前作品的上传限制", "material_limit_exceeded"
        ) from error
    if isinstance(error, service.MaterialUnavailable):
        raise api_error(404, "材料不可用", "material_unavailable") from error
    if isinstance(error, service.FileOperationRetryable):
        raise api_error(
            503,
            "当前操作暂时无法完成，请重试。",
            "file_operation_retryable",
            {"Retry-After": "1"},
        ) from error
    raise error


def _stream(source: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := source.read(UPLOAD_CHUNK_SIZE):
            yield chunk
    finally:
        source.close()


def _binary_response(image: service.ImageStream, disposition: str) -> StreamingResponse:
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(image.image.display_name)}",
    }
    return StreamingResponse(
        _stream(image.body),
        media_type=image.image.detected_content_type,
        headers=headers,
    )


@router.post(
    "/workspaces/{workspace_id}/works/{work_id}/images",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ImageResponseData],
    summary="上传私有作品图片",
)
def upload_image(
    workspace_id: UUID,
    work_id: UUID,
    image: Annotated[UploadFile, File(description="JPEG、PNG 或 WebP 图片")],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[ImageResponseData]:
    try:
        uploaded = service.upload_image(
            session,
            account.id,
            workspace_id,
            work_id,
            image.file,
            image.filename or "",
            image.content_type,
        )
    except Exception as error:
        _file_error(error)
        raise
    finally:
        image.file.close()
    return ApiResponse(
        code=201, message="作品图片已上传", data=_image_response(uploaded)
    )


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/images",
    response_model=ApiResponse[ImagePageResponseData],
    summary="列出私有作品图片",
)
def list_images(
    workspace_id: UUID,
    work_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[ImagePageResponseData]:
    try:
        images, total = service.list_images(
            session, account.id, workspace_id, work_id, params.page, params.size
        )
    except Exception as error:
        _file_error(error)
        raise
    return ApiResponse(
        code=200,
        message="作品图片已加载",
        data=ImagePageResponseData(
            items=[_image_response(image) for image in images],
            page=params.page,
            size=params.size,
            total=total,
            limits=_limits_response(service.image_limits()),
        ),
    )


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/images/{file_id}/preview",
    summary="预览私有作品图片",
)
def preview_image(
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        image = service.open_image(session, account.id, workspace_id, work_id, file_id)
    except Exception as error:
        _file_error(error)
        raise
    return _binary_response(image, "inline")


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/images/{file_id}/download",
    summary="下载私有作品图片",
)
def download_image(
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        image = service.open_image(session, account.id, workspace_id, work_id, file_id)
    except Exception as error:
        _file_error(error)
        raise
    return _binary_response(image, "attachment")


@router.post(
    "/workspaces/{workspace_id}/works/{work_id}/material-files",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[MaterialResponseData],
    summary="上传当前规则材料",
)
def upload_material(
    workspace_id: UUID,
    work_id: UUID,
    material: Annotated[UploadFile, File(description="PDF、JPEG、PNG 或 WebP 材料")],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[MaterialResponseData]:
    try:
        uploaded = service.upload_material(
            session,
            account.id,
            workspace_id,
            work_id,
            material.file,
            material.filename or "",
            material.content_type,
        )
    except Exception as error:
        _file_error(error)
        raise
    finally:
        material.file.close()
    return ApiResponse(
        code=201, message="材料已上传", data=_material_response(uploaded)
    )


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/material-files",
    response_model=ApiResponse[MaterialCandidatesResponseData],
    summary="列出可选的当前规则材料",
)
def list_materials(
    workspace_id: UUID,
    work_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[MaterialCandidatesResponseData]:
    try:
        materials = service.list_materials(session, account.id, workspace_id, work_id)
    except Exception as error:
        _file_error(error)
        raise
    return ApiResponse(
        code=200,
        message="可选材料已加载",
        data=MaterialCandidatesResponseData(
            items=[_material_response(material) for material in materials],
            limits=_material_limits_response(service.material_limits()),
        ),
    )


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/material-files/{file_id}/preview",
    summary="预览当前规则材料",
)
def preview_material(
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        material = service.open_material(
            session, account.id, workspace_id, work_id, file_id
        )
    except Exception as error:
        _file_error(error)
        raise
    return _binary_response(material, "inline")


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/material-files/{file_id}/download",
    summary="下载当前规则材料",
)
def download_material(
    workspace_id: UUID,
    work_id: UUID,
    file_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        material = service.open_material(
            session, account.id, workspace_id, work_id, file_id
        )
    except Exception as error:
        _file_error(error)
        raise
    return _binary_response(material, "attachment")
