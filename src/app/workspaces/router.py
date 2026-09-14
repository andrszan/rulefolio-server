import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import api_error
from app.core.responses import ApiResponse, Page, PageParams
from app.identity import service as identity_service
from app.identity.models import Account
from app.identity.router import SessionData, _bearer_token, _session_data
from app.workspaces import service

router = APIRouter(tags=["workspaces"])


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("工作空间名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class WorkspaceInvitationCreateRequest(BaseModel):
    email: EmailStr


class WorkspaceInvitationExchangeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=1024)


class WorkspaceInvitationActivationRequest(WorkspaceInvitationExchangeRequest):
    new_password: str = Field(
        min_length=1, max_length=1024, validation_alias="newPassword"
    )


class WorkspaceResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    name: str
    description: str | None
    is_owner: bool = Field(serialization_alias="isOwner")


class WorkspaceMemberResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    account_id: UUID = Field(serialization_alias="accountId")
    email: str
    is_owner: bool = Field(serialization_alias="isOwner")
    joined_at: datetime = Field(serialization_alias="joinedAt")


class WorkspaceInvitationResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    email: str
    status: str
    mail_status: str = Field(serialization_alias="mailStatus")
    expires_at: datetime = Field(serialization_alias="expiresAt")


class WorkspaceMemberRemovedData(BaseModel):
    reason: str = "workspace_member_removed"


class WorkspaceInvitationActivationData(BaseModel):
    workspace: WorkspaceResponseData
    session: SessionData | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def _workspace_response(data: service.WorkspaceData) -> WorkspaceResponseData:
    return WorkspaceResponseData(
        id=data.id,
        name=data.name,
        description=data.description,
        is_owner=data.is_owner,
    )


def _invitation_response(
    data: service.WorkspaceInvitationData,
) -> WorkspaceInvitationResponseData:
    return WorkspaceInvitationResponseData(
        id=data.id,
        email=data.email,
        status=data.status,
        mail_status=data.mail_status,
        expires_at=data.expires_at,
    )


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


def _workspace_error(error: Exception) -> None:
    if isinstance(error, service.WorkspaceUnavailable):
        raise api_error(404, "工作空间不可用", "workspace_unavailable") from error
    if isinstance(error, service.WorkspaceManagementForbidden):
        raise api_error(
            403, "当前会话不能管理该工作空间", "workspace_management_forbidden"
        ) from error
    if isinstance(error, service.WorkspaceMemberExists):
        raise api_error(409, "该账户已是成员", "workspace_member_exists") from error
    if isinstance(error, service.WorkspaceInvitationExists):
        raise api_error(
            409, "该邮箱已有有效邀请", "workspace_invitation_exists"
        ) from error
    if isinstance(error, service.InvitationRecipientUnavailable):
        raise api_error(
            422,
            "该受邀账户当前不能继续处理",
            "workspace_invitation_recipient_unavailable",
        ) from error
    if isinstance(error, service.WorkspaceOwnerCannotBeRemoved):
        raise api_error(
            409, "当前负责人不能被移除", "workspace_owner_cannot_be_removed"
        ) from error
    if isinstance(error, service.WorkspaceMemberUnavailable):
        raise api_error(404, "成员不可用", "workspace_member_unavailable") from error
    if isinstance(error, service.WorkspaceMemberLastMaintainerRequired):
        raise api_error(
            409, "作品至少需要一名维护者", "work_last_maintainer_required"
        ) from error
    if isinstance(error, service.WorkspaceInvitationUnavailable):
        raise api_error(
            400, "此邀请不能继续使用", "workspace_invitation_unavailable"
        ) from error
    if isinstance(error, service.WorkspaceOperationRetryable):
        raise api_error(
            503,
            "当前操作暂时无法完成，请重试。",
            "workspace_operation_retryable",
            {"Retry-After": "1"},
        ) from error
    raise error


@router.post(
    "/workspaces",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[WorkspaceResponseData],
    summary="创建工作空间",
)
def create_workspace(
    request: WorkspaceCreateRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkspaceResponseData]:
    try:
        workspace = service.create_workspace(
            session, account.id, request.name, request.description
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=201, message="工作空间已创建", data=_workspace_response(workspace)
    )


@router.get(
    "/workspaces",
    response_model=ApiResponse[Page[WorkspaceResponseData]],
    summary="列出当前账户可访问的工作空间",
)
def list_workspaces(
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[WorkspaceResponseData]]:
    workspaces, total = service.list_workspaces(
        session, account.id, params.page, params.size
    )
    return ApiResponse(
        code=200,
        message="工作空间列表已加载",
        data=Page(
            items=[_workspace_response(workspace) for workspace in workspaces],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.get(
    "/workspaces/{workspace_id}",
    response_model=ApiResponse[WorkspaceResponseData],
    summary="读取工作空间",
)
def read_workspace(
    workspace_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkspaceResponseData]:
    try:
        workspace = service.read_workspace(session, account.id, workspace_id)
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=200, message="工作空间已加载", data=_workspace_response(workspace)
    )


@router.get(
    "/workspaces/{workspace_id}/members",
    response_model=ApiResponse[Page[WorkspaceMemberResponseData]],
    summary="列出工作空间成员",
)
def list_members(
    workspace_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[WorkspaceMemberResponseData]]:
    try:
        members, total = service.list_members(
            session, account.id, workspace_id, params.page, params.size
        )
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=200,
        message="成员列表已加载",
        data=Page(
            items=[
                WorkspaceMemberResponseData(
                    account_id=member.account_id,
                    email=member.email,
                    is_owner=member.is_owner,
                    joined_at=member.joined_at,
                )
                for member in members
            ],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.get(
    "/workspaces/{workspace_id}/invitations",
    response_model=ApiResponse[Page[WorkspaceInvitationResponseData]],
    summary="列出工作空间邀请",
)
def list_invitations(
    workspace_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[WorkspaceInvitationResponseData]]:
    try:
        invitations, total = service.list_invitations(
            session, account.id, workspace_id, params.page, params.size
        )
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=200,
        message="邀请列表已加载",
        data=Page(
            items=[_invitation_response(invitation) for invitation in invitations],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.post(
    "/workspaces/{workspace_id}/invitations",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[WorkspaceInvitationResponseData],
    summary="邀请工作空间成员",
)
def create_invitation(
    workspace_id: UUID,
    request: WorkspaceInvitationCreateRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkspaceInvitationResponseData]:
    try:
        invitation = service.create_invitation(
            session, account.id, workspace_id, str(request.email)
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=201, message="邀请已建立", data=_invitation_response(invitation)
    )


@router.delete(
    "/workspaces/{workspace_id}/invitations/{invitation_id}",
    response_model=ApiResponse[WorkspaceInvitationResponseData],
    summary="撤销工作空间邀请",
)
def revoke_invitation(
    workspace_id: UUID,
    invitation_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkspaceInvitationResponseData]:
    try:
        invitation = service.revoke_invitation(
            session, account.id, workspace_id, invitation_id
        )
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=200, message="邀请状态已更新", data=_invitation_response(invitation)
    )


@router.delete(
    "/workspaces/{workspace_id}/members/{account_id}",
    response_model=ApiResponse[WorkspaceMemberRemovedData],
    summary="移除工作空间普通成员",
)
def remove_member(
    workspace_id: UUID,
    account_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkspaceMemberRemovedData]:
    try:
        service.remove_member(session, account.id, workspace_id, account_id)
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=200,
        message="成员已移除，后续工作空间访问已失效",
        data=WorkspaceMemberRemovedData(),
    )


@router.post(
    "/workspace-invitation-exchanges/current-session",
    response_model=ApiResponse[WorkspaceResponseData],
    summary="以当前会话兑换工作空间邀请",
)
def exchange_current_session_invitation(
    request: WorkspaceInvitationExchangeRequest,
    operation_key: Annotated[str, Depends(_idempotency_key)],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkspaceResponseData]:
    try:
        result = service.exchange_current_session_invitation(
            session, account.id, request.token, operation_key
        )
    except service.WorkspaceInvitationRateLimited as error:
        raise api_error(
            429,
            "当前操作过于频繁，请稍后重试。",
            "workspace_invitation_exchange_rate_limited",
            {"Retry-After": str(error.retry_after)},
        ) from error
    except service.WorkspaceInvitationAccountMismatch as error:
        raise api_error(
            403,
            "当前账户不能继续此邀请。",
            "workspace_invitation_account_mismatch",
        ) from error
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=200,
        message="已加入工作空间",
        data=_workspace_response(result.workspace),
    )


@router.post(
    "/workspace-invitation-exchanges/account-activation",
    response_model=ApiResponse[WorkspaceInvitationActivationData],
    summary="设置密码并兑换工作空间邀请",
)
def exchange_account_activation_invitation(
    request: WorkspaceInvitationActivationRequest,
    operation_key: Annotated[str, Depends(_idempotency_key)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkspaceInvitationActivationData]:
    try:
        result = service.exchange_account_activation_invitation(
            session, request.token, request.new_password, operation_key
        )
    except service.WorkspaceInvitationRateLimited as error:
        raise api_error(
            429,
            "当前操作过于频繁，请稍后重试。",
            "workspace_invitation_exchange_rate_limited",
            {"Retry-After": str(error.retry_after)},
        ) from error
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _workspace_error(error)
        raise
    return ApiResponse(
        code=200,
        message="已加入工作空间"
        if not result.login_required
        else "邀请已处理，请使用刚设置的密码登录",
        data=WorkspaceInvitationActivationData(
            workspace=_workspace_response(result.workspace),
            session=(
                _session_data(result.session_result)
                if result.session_result is not None
                else None
            ),
        ),
    )
