from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import api_error
from app.core.responses import ApiResponse, Page, PageParams
from app.identity import service as identity_service
from app.identity.models import Account
from app.identity.router import _bearer_token
from app.works import service
from app.workspaces import service as workspaces_service

router = APIRouter(tags=["works"])


class WorkValuesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2000)
    creative_stage: str = Field(
        min_length=1, max_length=160, validation_alias="creativeStage"
    )
    target_experience: str = Field(
        min_length=1, max_length=2000, validation_alias="targetExperience"
    )
    min_players: int = Field(gt=0, validation_alias="minPlayers")
    max_players: int = Field(gt=0, validation_alias="maxPlayers")
    estimated_duration_minutes: int = Field(
        gt=0, validation_alias="estimatedDurationMinutes"
    )

    @field_validator("name", "description", "creative_stage", "target_experience")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value

    @field_validator("max_players")
    @classmethod
    def validate_player_range(cls, value: int, info: object) -> int:
        min_players = getattr(info, "data", {}).get("min_players")
        if min_players is not None and value < min_players:
            raise ValueError("最大人数不能小于最小人数")
        return value


class WorkCreateRequest(WorkValuesRequest):
    pass


class WorkUpdateRequest(WorkValuesRequest):
    expected_revision: int = Field(gt=0, validation_alias="expectedRevision")


class RuleMaterialsUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule_name: str = Field(min_length=1, max_length=160, validation_alias="ruleName")
    rule_description: str | None = Field(
        default=None, max_length=2000, validation_alias="ruleDescription"
    )
    rule_content: str = Field(
        min_length=1, max_length=50_000, validation_alias="ruleContent"
    )
    material_file_ids: list[UUID] = Field(
        default_factory=list, validation_alias="materialFileIds"
    )
    expected_revision: int = Field(gt=0, validation_alias="expectedRevision")

    @field_validator("rule_name", "rule_content")
    @classmethod
    def strip_required_rule_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value

    @field_validator("rule_description")
    @classmethod
    def strip_optional_rule_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class WorkAccessSetRequest(BaseModel):
    role: Literal["maintainer", "organizer", "collaborator"]


class WorkResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    workspace_id: UUID = Field(serialization_alias="workspaceId")
    name: str
    description: str
    creative_stage: str = Field(serialization_alias="creativeStage")
    target_experience: str = Field(serialization_alias="targetExperience")
    min_players: int = Field(serialization_alias="minPlayers")
    max_players: int = Field(serialization_alias="maxPlayers")
    estimated_duration_minutes: int = Field(
        serialization_alias="estimatedDurationMinutes"
    )
    revision: int
    own_role: str = Field(serialization_alias="ownRole")
    can_manage_access: bool = Field(serialization_alias="canManageAccess")


class MaterialResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    display_name: str = Field(serialization_alias="displayName")
    detected_content_type: str = Field(serialization_alias="detectedContentType")
    size_bytes: int = Field(serialization_alias="sizeBytes")
    created_at: str = Field(serialization_alias="createdAt")


class RuleMaterialsResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule_name: str | None = Field(serialization_alias="ruleName")
    rule_description: str | None = Field(serialization_alias="ruleDescription")
    rule_content: str | None = Field(serialization_alias="ruleContent")
    materials: list[MaterialResponseData]
    revision: int


class WorkAccessResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    account_id: UUID = Field(serialization_alias="accountId")
    role: str


class WorkAccessMemberResponseData(WorkAccessResponseData):
    email: str
    role: str | None


class WorkAccessRevokedData(BaseModel):
    reason: str = "work_access_revoked"


def _authenticated_account(
    token: Annotated[str, Depends(_bearer_token)], session: Session = Depends(get_db)
) -> Account:
    try:
        return identity_service.authenticate(session, token).account
    except identity_service.SessionUnavailable as error:
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        ) from error


def _work_response(data: service.WorkData) -> WorkResponseData:
    return WorkResponseData(
        id=data.id,
        workspace_id=data.workspace_id,
        name=data.name,
        description=data.description,
        creative_stage=data.creative_stage,
        target_experience=data.target_experience,
        min_players=data.min_players,
        max_players=data.max_players,
        estimated_duration_minutes=data.estimated_duration_minutes,
        revision=data.revision,
        own_role=data.own_role,
        can_manage_access=data.can_manage_access,
    )


def _rule_materials_response(
    data: service.RuleMaterialsData,
) -> RuleMaterialsResponseData:
    return RuleMaterialsResponseData(
        rule_name=data.rule_name,
        rule_description=data.rule_description,
        rule_content=data.rule_content,
        materials=[
            MaterialResponseData(
                id=material.id,
                display_name=material.display_name,
                detected_content_type=material.detected_content_type,
                size_bytes=material.size_bytes,
                created_at=material.created_at.isoformat(),
            )
            for material in data.materials
        ],
        revision=data.revision,
    )


def _work_error(error: Exception) -> None:
    if isinstance(error, service.WorkspaceUnavailable):
        raise api_error(404, "工作空间不可用", "workspace_unavailable") from error
    if isinstance(error, service.WorkUnavailable):
        raise api_error(404, "作品不可用", "work_unavailable") from error
    if isinstance(error, service.WorkManagementForbidden):
        raise api_error(
            403, "当前会话不能管理该作品", "work_management_forbidden"
        ) from error
    if isinstance(error, service.WorkRevisionConflict):
        raise api_error(
            409, "作品已被更新，请重新加载后核对", "work_revision_conflict"
        ) from error
    if isinstance(error, service.MaterialSelectionInvalid):
        raise api_error(422, "所选材料不可用", "material_selection_invalid") from error
    if isinstance(error, service.WorkLastMaintainerRequired):
        raise api_error(
            409, "作品至少需要一名维护者", "work_last_maintainer_required"
        ) from error
    if isinstance(error, service.WorkAccessMemberUnavailable):
        raise api_error(404, "成员不可用", "workspace_member_unavailable") from error
    if isinstance(error, service.WorkAccessUnavailable):
        raise api_error(404, "作品访问关系不可用", "work_access_unavailable") from error
    if isinstance(error, workspaces_service.WorkspaceExitInProgress):
        raise api_error(
            409, "工作空间正在退出", "workspace_exit_in_progress"
        ) from error
    if isinstance(error, service.WorkOperationRetryable):
        raise api_error(
            503,
            "当前操作暂时无法完成，请重试。",
            "work_operation_retryable",
            {"Retry-After": "1"},
        ) from error
    raise error


@router.post(
    "/workspaces/{workspace_id}/works",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[WorkResponseData],
    summary="创建私有作品",
)
def create_work(
    workspace_id: UUID,
    request: WorkCreateRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkResponseData]:
    try:
        work = service.create_work(
            session,
            account.id,
            workspace_id,
            name=request.name,
            description=request.description,
            creative_stage=request.creative_stage,
            target_experience=request.target_experience,
            min_players=request.min_players,
            max_players=request.max_players,
            estimated_duration_minutes=request.estimated_duration_minutes,
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(code=201, message="作品已创建", data=_work_response(work))


@router.get(
    "/workspaces/{workspace_id}/works",
    response_model=ApiResponse[Page[WorkResponseData]],
    summary="列出当前账户可访问的作品",
)
def list_works(
    workspace_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[WorkResponseData]]:
    try:
        works, total = service.list_works(
            session, account.id, workspace_id, params.page, params.size
        )
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(
        code=200,
        message="作品列表已加载",
        data=Page(
            items=[_work_response(work) for work in works],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}",
    response_model=ApiResponse[WorkResponseData],
    summary="读取私有作品",
)
def read_work(
    workspace_id: UUID,
    work_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkResponseData]:
    try:
        work = service.read_work(session, account.id, workspace_id, work_id)
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(code=200, message="作品已加载", data=_work_response(work))


@router.patch(
    "/workspaces/{workspace_id}/works/{work_id}",
    response_model=ApiResponse[WorkResponseData],
    summary="编辑作品基础资料",
)
def update_work(
    workspace_id: UUID,
    work_id: UUID,
    request: WorkUpdateRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkResponseData]:
    try:
        work = service.update_work(
            session,
            account.id,
            workspace_id,
            work_id,
            name=request.name,
            description=request.description,
            creative_stage=request.creative_stage,
            target_experience=request.target_experience,
            min_players=request.min_players,
            max_players=request.max_players,
            estimated_duration_minutes=request.estimated_duration_minutes,
            expected_revision=request.expected_revision,
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(code=200, message="作品资料已更新", data=_work_response(work))


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/rule-materials",
    response_model=ApiResponse[RuleMaterialsResponseData],
    summary="读取当前规则与材料",
)
def read_rule_materials(
    workspace_id: UUID,
    work_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[RuleMaterialsResponseData]:
    try:
        data = service.read_rule_materials(session, account.id, workspace_id, work_id)
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(
        code=200,
        message="当前规则与材料已加载",
        data=_rule_materials_response(data),
    )


@router.put(
    "/workspaces/{workspace_id}/works/{work_id}/rule-materials",
    response_model=ApiResponse[RuleMaterialsResponseData],
    summary="保存当前规则与材料",
)
def update_rule_materials(
    workspace_id: UUID,
    work_id: UUID,
    request: RuleMaterialsUpdateRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[RuleMaterialsResponseData]:
    try:
        data = service.update_rule_materials(
            session,
            account.id,
            workspace_id,
            work_id,
            rule_name=request.rule_name,
            rule_description=request.rule_description,
            rule_content=request.rule_content,
            material_file_ids=request.material_file_ids,
            expected_revision=request.expected_revision,
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(
        code=200,
        message="当前规则与材料已保存",
        data=_rule_materials_response(data),
    )


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/access",
    response_model=ApiResponse[Page[WorkAccessMemberResponseData]],
    summary="读取作品访问管理成员",
)
def list_access_members(
    workspace_id: UUID,
    work_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[WorkAccessMemberResponseData]]:
    try:
        members, total = service.list_access_members(
            session, account.id, workspace_id, work_id, params.page, params.size
        )
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(
        code=200,
        message="作品访问成员已加载",
        data=Page(
            items=[
                WorkAccessMemberResponseData(
                    account_id=member.account_id, email=member.email, role=member.role
                )
                for member in members
            ],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.put(
    "/workspaces/{workspace_id}/works/{work_id}/access/{account_id}",
    response_model=ApiResponse[WorkAccessResponseData],
    summary="授予或调整作品角色",
)
def set_work_access(
    workspace_id: UUID,
    work_id: UUID,
    account_id: UUID,
    request: WorkAccessSetRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkAccessResponseData]:
    try:
        access = service.set_work_access(
            session,
            account.id,
            workspace_id,
            work_id,
            account_id,
            request.role,
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(
        code=200,
        message="作品访问角色已更新",
        data=WorkAccessResponseData(account_id=access.account_id, role=access.role),
    )


@router.delete(
    "/workspaces/{workspace_id}/works/{work_id}/access/{account_id}",
    response_model=ApiResponse[WorkAccessRevokedData],
    summary="撤销作品访问角色",
)
def revoke_work_access(
    workspace_id: UUID,
    work_id: UUID,
    account_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[WorkAccessRevokedData]:
    try:
        service.revoke_work_access(
            session, account.id, workspace_id, work_id, account_id
        )
    except Exception as error:
        _work_error(error)
        raise
    return ApiResponse(
        code=200,
        message="作品访问已撤销",
        data=WorkAccessRevokedData(),
    )
