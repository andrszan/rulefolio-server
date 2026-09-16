import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import api_error
from app.core.responses import ApiResponse, Page, PageParams
from app.identity import service as identity_service
from app.identity.models import Account
from app.identity.router import _bearer_token
from app.notifications import service

router = APIRouter(tags=["notifications"])


class NotificationTodoResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    kind: str
    summary: str
    context_label: str = Field(serialization_alias="contextLabel")
    workspace_id: UUID = Field(serialization_alias="workspaceId")
    work_id: UUID | None = Field(serialization_alias="workId")
    target_kind: str = Field(serialization_alias="targetKind")
    target_id: UUID = Field(serialization_alias="targetId")
    status: str
    created_at: datetime = Field(serialization_alias="createdAt")
    resolved_at: datetime | None = Field(serialization_alias="resolvedAt")
    mail_status: str | None = Field(serialization_alias="mailStatus")
    last_attempt_at: datetime | None = Field(serialization_alias="lastAttemptAt")
    can_retry_mail: bool = Field(serialization_alias="canRetryMail")


def _authenticated_account(
    token: Annotated[str, Depends(_bearer_token)], session: Session = Depends(get_db)
) -> Account:
    try:
        return identity_service.authenticate(session, token).account
    except identity_service.SessionUnavailable as error:
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        ) from error


def _idempotency_key(
    value: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if value is None or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None:
        raise api_error(422, "请求参数有误", "validation_failed")
    return value


def _response(data: service.NotificationTodoData) -> NotificationTodoResponseData:
    return NotificationTodoResponseData(
        id=data.id,
        kind=data.kind,
        summary=data.summary,
        context_label=data.context_label,
        workspace_id=data.workspace_id,
        work_id=data.work_id,
        target_kind=data.target_kind,
        target_id=data.target_id,
        status=data.status,
        created_at=data.created_at,
        resolved_at=data.resolved_at,
        mail_status=data.mail_status,
        last_attempt_at=data.last_attempt_at,
        can_retry_mail=data.can_retry_mail,
    )


def _notification_error(error: Exception) -> None:
    if isinstance(error, service.NotificationTodoUnavailable):
        raise api_error(
            404, "待办内容不可用", "notification_todo_unavailable"
        ) from error
    if isinstance(error, service.NotificationTodoConflict):
        raise api_error(409, "当前待办不能执行此操作", error.reason) from error
    if isinstance(error, SQLAlchemyError):
        raise api_error(
            503,
            "当前操作暂时无法完成，请重试。",
            "notification_operation_retryable",
            {"Retry-After": "1"},
        ) from error
    raise error


@router.get(
    "/notifications/todos",
    response_model=ApiResponse[Page[NotificationTodoResponseData]],
    summary="列出当前账户待办",
)
def list_todos(
    todo_status: Annotated[
        Literal["open", "completed", "cancelled"], Query(alias="status")
    ] = "open",
    params: Annotated[PageParams, Depends()] = PageParams(),
    account: Annotated[Account, Depends(_authenticated_account)] = None,
    session: Session = Depends(get_db),
) -> ApiResponse[Page[NotificationTodoResponseData]]:
    try:
        todos, total = service.list_todos(
            session, account.id, todo_status, params.page, params.size
        )
    except Exception as error:
        session.rollback()
        _notification_error(error)
        raise
    return ApiResponse(
        code=200,
        message="待办列表已加载",
        data=Page(
            items=[_response(todo) for todo in todos],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.post(
    "/notifications/todos/{todo_id}/complete",
    response_model=ApiResponse[NotificationTodoResponseData],
    summary="标记待办已处理",
)
def complete_todo(
    todo_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[NotificationTodoResponseData]:
    try:
        todo = service.complete_todo(session, account.id, todo_id)
    except Exception as error:
        session.rollback()
        _notification_error(error)
        raise
    return ApiResponse(code=200, message="待办已处理", data=_response(todo))


@router.post(
    "/notifications/todos/{todo_id}/mail-retry",
    response_model=ApiResponse[NotificationTodoResponseData],
    summary="重投一次失败的业务邮件",
)
def retry_mail(
    todo_id: UUID,
    operation_key: Annotated[str, Depends(_idempotency_key)],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[NotificationTodoResponseData]:
    try:
        todo = service.retry_failed_mail(session, account.id, todo_id, operation_key)
    except Exception as error:
        session.rollback()
        _notification_error(error)
        raise
    return ApiResponse(code=200, message="邮件已重新进入发送队列", data=_response(todo))
