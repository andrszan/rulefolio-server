from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import api_error
from app.core.responses import ApiResponse
from app.identity import service

router = APIRouter(tags=["identity"])


class CredentialsRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class RecoveryRequest(BaseModel):
    email: EmailStr


class TokenExchangeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(
        min_length=1, max_length=1024, validation_alias="newPassword"
    )


class AccountData(BaseModel):
    id: UUID
    status: str


class SessionData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_token: str = Field(serialization_alias="sessionToken")
    account: AccountData
    expires_at: datetime = Field(serialization_alias="expiresAt")


class CurrentSessionData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    account: AccountData
    expires_at: datetime = Field(serialization_alias="expiresAt")


class RecoveryAcceptedData(BaseModel):
    reason: str = "recovery_request_accepted"


class LoggedOutData(BaseModel):
    reason: str = "session_revoked"


def _bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        )
    return authorization.removeprefix("Bearer ").strip()


def _session_data(result: service.SessionResult) -> SessionData:
    return SessionData(
        session_token=result.token,
        account=AccountData(id=result.account_id, status=result.account_status),
        expires_at=result.expires_at,
    )


@router.post(
    "/sessions",
    response_model=ApiResponse[SessionData],
    summary="登录并创建当前浏览器会话",
)
def create_session(
    request: CredentialsRequest, session: Session = Depends(get_db)
) -> ApiResponse[SessionData]:
    try:
        result = service.login(session, str(request.email), request.password)
    except service.RateLimited as error:
        raise api_error(
            429,
            "登录尝试过于频繁，请稍后重试。",
            "authentication_rate_limited",
            {"Retry-After": str(error.retry_after)},
        ) from error
    except (service.AuthenticationFailed, ValueError) as error:
        raise api_error(
            401, "邮箱、密码或账户状态无法用于登录", "authentication_failed"
        ) from error
    return ApiResponse(code=200, message="登录成功", data=_session_data(result))


@router.get(
    "/sessions/current",
    response_model=ApiResponse[CurrentSessionData],
    summary="读取当前会话",
)
def read_current_session(
    token: Annotated[str, Depends(_bearer_token)], session: Session = Depends(get_db)
) -> ApiResponse[CurrentSessionData]:
    try:
        authenticated = service.authenticate(session, token)
    except service.SessionUnavailable as error:
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        ) from error
    return ApiResponse(
        code=200,
        message="当前会话有效",
        data=CurrentSessionData(
            account=AccountData(
                id=authenticated.account.id, status=authenticated.account.status
            ),
            expires_at=authenticated.record.expires_at,
        ),
    )


@router.delete(
    "/sessions/current",
    response_model=ApiResponse[LoggedOutData],
    summary="退出当前会话",
)
def delete_current_session(
    token: Annotated[str, Depends(_bearer_token)], session: Session = Depends(get_db)
) -> ApiResponse[LoggedOutData]:
    try:
        service.logout(session, token)
    except service.SessionUnavailable as error:
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        ) from error
    return ApiResponse(code=200, message="已退出登录", data=LoggedOutData())


@router.post(
    "/account-recovery-requests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[RecoveryAcceptedData],
    summary="请求账户恢复",
)
def create_recovery_request(
    request: RecoveryRequest, session: Session = Depends(get_db)
) -> ApiResponse[RecoveryAcceptedData]:
    try:
        service.request_recovery(session, str(request.email))
    except service.RateLimited as error:
        raise api_error(
            429,
            "当前请求过于频繁，请稍后重试。",
            "recovery_request_rate_limited",
            {"Retry-After": str(error.retry_after)},
        ) from error
    except Exception as error:
        session.rollback()
        raise api_error(
            503,
            "暂时无法受理恢复请求，请稍后重试。",
            "recovery_request_temporarily_unavailable",
        ) from error
    return ApiResponse(
        code=202,
        message="如果该邮箱可用于恢复账户，系统会发送后续说明。",
        data=RecoveryAcceptedData(),
    )


@router.post(
    "/account-activations/exchanges",
    response_model=ApiResponse[SessionData],
    summary="兑换账户激活链接",
)
def exchange_activation(
    request: TokenExchangeRequest, session: Session = Depends(get_db)
) -> ApiResponse[SessionData]:
    try:
        result = service.activate(session, request.token, request.new_password)
    except service.RateLimited as error:
        raise api_error(
            429,
            "当前操作过于频繁，请稍后重试。",
            "activation_exchange_rate_limited",
            {"Retry-After": str(error.retry_after)},
        ) from error
    except (service.LinkUnavailable, ValueError) as error:
        raise api_error(
            400, "此链接不能继续使用", "activation_link_unavailable"
        ) from error
    return ApiResponse(code=200, message="账户已激活", data=_session_data(result))


@router.post(
    "/account-recovery-exchanges",
    response_model=ApiResponse[SessionData],
    summary="兑换恢复链接并更新密码",
)
def exchange_recovery(
    request: TokenExchangeRequest, session: Session = Depends(get_db)
) -> ApiResponse[SessionData]:
    try:
        result = service.recover_password(session, request.token, request.new_password)
    except service.RateLimited as error:
        raise api_error(
            429,
            "当前操作过于频繁，请稍后重试。",
            "recovery_exchange_rate_limited",
            {"Retry-After": str(error.retry_after)},
        ) from error
    except (service.LinkUnavailable, ValueError) as error:
        raise api_error(
            400, "此链接不能继续使用", "recovery_link_unavailable"
        ) from error
    return ApiResponse(
        code=200, message="密码已更新，其他会话已退出", data=_session_data(result)
    )
