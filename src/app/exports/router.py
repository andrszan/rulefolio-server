from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import api_error
from app.exports import service
from app.identity import service as identity_service
from app.identity.models import Account
from app.identity.router import _bearer_token

router = APIRouter(tags=["exports"])


def _authenticated_account(
    token: Annotated[str, Depends(_bearer_token)], session: Session = Depends(get_db)
) -> Account:
    try:
        return identity_service.authenticate(session, token).account
    except identity_service.SessionUnavailable as error:
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        ) from error


def _stream(archive: service.WorkExport) -> Iterator[bytes]:
    try:
        while chunk := archive.body.read(64 * 1024):
            yield chunk
    finally:
        archive.body.close()


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/export",
    summary="导出当前可访问的作品资料",
)
def download_work_export(
    workspace_id: UUID,
    work_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        archive = service.build_work_export(session, account.id, workspace_id, work_id)
    except service.WorkExportUnavailable as error:
        raise api_error(404, "作品资料不可用", "work_export_unavailable") from error
    except service.WorkExportRetryable as error:
        raise api_error(
            503,
            "导出未完成，请检查连接后重试",
            "work_export_retryable",
            {"Retry-After": "1"},
        ) from error
    return StreamingResponse(
        _stream(archive),
        media_type="application/zip",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(archive.filename)}",
        },
    )
