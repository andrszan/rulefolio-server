from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import api_error
from app.core.responses import ApiResponse, Page, PageParams
from app.files.router import _binary_response
from app.identity import service as identity_service
from app.identity.models import Account
from app.identity.router import _bearer_token
from app.playtests import service

router = APIRouter(tags=["playtests"])


class SessionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scheduled_at: datetime = Field(validation_alias="scheduledAt")
    location: str = Field(min_length=1, max_length=240)
    capacity: int = Field(gt=0, le=100, validation_alias="capacity")
    material_file_ids: list[UUID] = Field(
        min_length=1, validation_alias="materialFileIds"
    )
    participant_emails: list[str] = Field(
        default_factory=list, max_length=100, validation_alias="participantEmails"
    )

    @field_validator("location")
    @classmethod
    def strip_location(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("地点不能为空")
        return value


class CreatePlanRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    observation_goal: str = Field(
        min_length=1, max_length=4_000, validation_alias="observationGoal"
    )
    recording_method: str = Field(
        min_length=1, max_length=4_000, validation_alias="recordingMethod"
    )
    sessions: list[SessionRequest] = Field(min_length=1, max_length=100)

    @field_validator("observation_goal", "recording_method")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value


class ArrangementRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scheduled_at: datetime = Field(validation_alias="scheduledAt")
    location: str = Field(min_length=1, max_length=240)
    capacity: int = Field(gt=0, le=100)
    expected_revision: int = Field(gt=0, validation_alias="expectedRevision")

    @field_validator("location")
    @classmethod
    def strip_location(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("地点不能为空")
        return value


class ReplaceMaterialsRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    material_file_ids: list[UUID] = Field(
        min_length=1, validation_alias="materialFileIds"
    )
    expected_revision: int = Field(gt=0, validation_alias="expectedRevision")


class RevisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    expected_revision: int = Field(gt=0, validation_alias="expectedRevision")


class NotificationResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str | None
    attempt_count: int | None = Field(serialization_alias="attemptCount")


class MaterialResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    display_name: str = Field(serialization_alias="displayName")
    detected_content_type: str = Field(serialization_alias="detectedContentType")
    size_bytes: int = Field(serialization_alias="sizeBytes")
    sha256: str


class ParticipantResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    account_id: UUID = Field(serialization_alias="accountId")
    email: str
    status: str
    latest_notification: NotificationResponseData = Field(
        serialization_alias="latestNotification"
    )


class SessionResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    scheduled_at: str = Field(serialization_alias="scheduledAt")
    location: str
    capacity: int
    status: str
    revision: int
    started_at: str | None = Field(serialization_alias="startedAt")
    observation_goal: str = Field(serialization_alias="observationGoal")
    recording_method: str = Field(serialization_alias="recordingMethod")
    work_name: str = Field(serialization_alias="workName")
    rule_name: str = Field(serialization_alias="ruleName")
    rule_description: str | None = Field(serialization_alias="ruleDescription")
    rule_content: str = Field(serialization_alias="ruleContent")
    confirmed_count: int = Field(serialization_alias="confirmedCount")
    materials: list[MaterialResponseData]
    participants: list[ParticipantResponseData]


class PlanResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    workspace_id: UUID = Field(serialization_alias="workspaceId")
    work_id: UUID = Field(serialization_alias="workId")
    observation_goal: str = Field(serialization_alias="observationGoal")
    recording_method: str = Field(serialization_alias="recordingMethod")
    created_at: str = Field(serialization_alias="createdAt")
    sessions: list[SessionResponseData]


class PlanSummaryResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    observation_goal: str = Field(serialization_alias="observationGoal")
    recording_method: str = Field(serialization_alias="recordingMethod")
    created_at: str = Field(serialization_alias="createdAt")
    session_count: int = Field(serialization_alias="sessionCount")
    next_session: SessionResponseData | None = Field(serialization_alias="nextSession")


class ParticipantSessionResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    work_name: str = Field(serialization_alias="workName")
    observation_goal: str = Field(serialization_alias="observationGoal")
    recording_method: str = Field(serialization_alias="recordingMethod")
    scheduled_at: str = Field(serialization_alias="scheduledAt")
    location: str
    capacity: int
    confirmed_count: int = Field(serialization_alias="confirmedCount")
    status: str
    own_status: str = Field(serialization_alias="confirmationStatus")
    rule_name: str = Field(serialization_alias="ruleName")
    rule_description: str | None = Field(serialization_alias="ruleDescription")
    rule_content: str = Field(serialization_alias="ruleContent")
    materials: list[MaterialResponseData]
    latest_notification: NotificationResponseData = Field(
        serialization_alias="latestNotification"
    )


class ConfirmationResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str
    confirmed_count: int = Field(serialization_alias="confirmedCount")
    capacity: int


def _authenticated_account(
    token: Annotated[str, Depends(_bearer_token)], session: Session = Depends(get_db)
) -> Account:
    try:
        return identity_service.authenticate(session, token).account
    except identity_service.SessionUnavailable as error:
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        ) from error


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _notification_response(
    data: service.NotificationData,
) -> NotificationResponseData:
    return NotificationResponseData(
        status=data.status, attempt_count=data.attempt_count
    )


def _material_response(data: service.MaterialData) -> MaterialResponseData:
    return MaterialResponseData(
        id=data.id,
        display_name=data.display_name,
        detected_content_type=data.detected_content_type,
        size_bytes=data.size_bytes,
        sha256=data.sha256,
    )


def _session_response(data: service.SessionData) -> SessionResponseData:
    return SessionResponseData(
        id=data.id,
        scheduled_at=data.scheduled_at.isoformat(),
        location=data.location,
        capacity=data.capacity,
        status=data.status,
        revision=data.revision,
        started_at=_iso(data.started_at),
        observation_goal=data.observation_goals,
        recording_method=data.recording_method,
        work_name=data.work_name,
        rule_name=data.rule_name,
        rule_description=data.rule_description,
        rule_content=data.rule_content,
        confirmed_count=data.confirmed_count,
        materials=[_material_response(material) for material in data.materials],
        participants=[
            ParticipantResponseData(
                account_id=participant.account_id,
                email=participant.email,
                status=participant.status,
                latest_notification=_notification_response(
                    participant.latest_notification
                ),
            )
            for participant in data.participants
        ],
    )


def _plan_response(data: service.PlanData) -> PlanResponseData:
    return PlanResponseData(
        id=data.id,
        workspace_id=data.workspace_id,
        work_id=data.work_id,
        observation_goal=data.observation_goals,
        recording_method=data.recording_method,
        created_at=data.created_at.isoformat(),
        sessions=[_session_response(item) for item in data.sessions],
    )


def _plan_summary_response(data: service.PlanSummaryData) -> PlanSummaryResponseData:
    return PlanSummaryResponseData(
        id=data.id,
        observation_goal=data.observation_goals,
        recording_method=data.recording_method,
        created_at=data.created_at.isoformat(),
        session_count=data.session_count,
        next_session=(
            _session_response(data.next_session)
            if data.next_session is not None
            else None
        ),
    )


def _draft(data: SessionRequest) -> service.SessionDraft:
    return service.SessionDraft(
        scheduled_at=data.scheduled_at,
        location=data.location,
        capacity=data.capacity,
        material_file_ids=tuple(data.material_file_ids),
        participant_emails=tuple(data.participant_emails),
    )


def _playtest_error(error: Exception) -> None:
    if isinstance(error, service.PlaytestUnavailable):
        raise api_error(404, "试玩场次不可用", "playtest_unavailable") from error
    if isinstance(error, service.PlaytestManagementForbidden):
        raise api_error(
            403, "当前会话不能管理该试玩场次", "playtest_management_forbidden"
        ) from error
    if isinstance(error, service.PlaytestParticipantUnavailable):
        raise api_error(
            422, "该参与者暂时不能邀请", "playtest_participant_unavailable"
        ) from error
    if isinstance(error, service.PlaytestMaterialSelectionInvalid):
        raise api_error(
            422, "所选场次材料或安排不可用", "material_selection_invalid"
        ) from error
    if isinstance(error, service.PlaytestSessionRevisionConflict):
        raise api_error(
            409,
            "场次已被更新，请重新加载后核对",
            "playtest_session_revision_conflict",
        ) from error
    if isinstance(error, service.PlaytestCapacityExceeded):
        raise api_error(409, "场次人数已满", "playtest_capacity_exceeded") from error
    if isinstance(error, service.PlaytestCapacityBelowConfirmed):
        raise api_error(
            409,
            "人数上限不能低于已确认人数",
            "playtest_capacity_below_confirmed",
        ) from error
    if isinstance(error, service.PlaytestSessionStateInvalid):
        raise api_error(
            409, "当前场次状态不允许此操作", "playtest_session_state_invalid"
        ) from error
    if isinstance(error, service.PlaytestOperationRetryable):
        raise api_error(
            503,
            "当前操作暂时无法完成，请重试。",
            "playtest_operation_retryable",
            {"Retry-After": "1"},
        ) from error
    raise error


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/playtest-plans",
    response_model=ApiResponse[Page[PlanSummaryResponseData]],
    summary="列出试玩计划",
)
def list_plans(
    workspace_id: UUID,
    work_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[PlanSummaryResponseData]]:
    try:
        plans, total = service.list_plans(
            session, account.id, workspace_id, work_id, params.page, params.size
        )
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(
        code=200,
        message="试玩计划已加载",
        data=Page(
            items=[_plan_summary_response(plan) for plan in plans],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.post(
    "/workspaces/{workspace_id}/works/{work_id}/playtest-plans",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[PlanResponseData],
    summary="创建试玩计划和初始场次",
)
def create_plan(
    workspace_id: UUID,
    work_id: UUID,
    request: CreatePlanRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[PlanResponseData]:
    try:
        plan = service.create_plan(
            session,
            account.id,
            workspace_id,
            work_id,
            request.observation_goal,
            request.recording_method,
            tuple(_draft(item) for item in request.sessions),
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(code=201, message="试玩计划已创建", data=_plan_response(plan))


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/playtest-plans/{plan_id}",
    response_model=ApiResponse[PlanResponseData],
    summary="读取试玩计划详情",
)
def read_plan(
    workspace_id: UUID,
    work_id: UUID,
    plan_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[PlanResponseData]:
    try:
        plan = service.read_plan(session, account.id, workspace_id, work_id, plan_id)
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(code=200, message="试玩计划已加载", data=_plan_response(plan))


@router.post(
    "/workspaces/{workspace_id}/works/{work_id}/playtest-plans/{plan_id}/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[SessionResponseData],
    summary="向试玩计划追加场次",
)
def append_session(
    workspace_id: UUID,
    work_id: UUID,
    plan_id: UUID,
    request: SessionRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[SessionResponseData]:
    try:
        item = service.append_session(
            session, account.id, workspace_id, work_id, plan_id, _draft(request)
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(code=201, message="试玩场次已创建", data=_session_response(item))


@router.patch(
    "/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/arrangement",
    response_model=ApiResponse[SessionResponseData],
    summary="修改试玩场次安排",
)
def update_arrangement(
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    request: ArrangementRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[SessionResponseData]:
    try:
        item = service.update_arrangement(
            session,
            account.id,
            workspace_id,
            work_id,
            session_id,
            scheduled_at=request.scheduled_at,
            location=request.location,
            capacity=request.capacity,
            expected_revision=request.expected_revision,
        )
    except ValueError as error:
        raise api_error(422, "请求参数有误", "validation_failed") from error
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(
        code=200, message="试玩场次安排已更新", data=_session_response(item)
    )


@router.put(
    "/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/materials",
    response_model=ApiResponse[SessionResponseData],
    summary="更换场次固定材料",
)
def replace_materials(
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    request: ReplaceMaterialsRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[SessionResponseData]:
    try:
        item = service.replace_materials(
            session,
            account.id,
            workspace_id,
            work_id,
            session_id,
            tuple(request.material_file_ids),
            request.expected_revision,
        )
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(
        code=200, message="场次固定材料已更新", data=_session_response(item)
    )


@router.post(
    "/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/start",
    response_model=ApiResponse[SessionResponseData],
    summary="标记试玩场次开始",
)
def start_session(
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    request: RevisionRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[SessionResponseData]:
    try:
        item = service.start_session(
            session,
            account.id,
            workspace_id,
            work_id,
            session_id,
            request.expected_revision,
        )
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(code=200, message="试玩场次已开始", data=_session_response(item))


@router.post(
    "/workspaces/{workspace_id}/works/{work_id}/playtest-sessions/{session_id}/cancel",
    response_model=ApiResponse[SessionResponseData],
    summary="取消试玩场次",
)
def cancel_session(
    workspace_id: UUID,
    work_id: UUID,
    session_id: UUID,
    request: RevisionRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[SessionResponseData]:
    try:
        item = service.cancel_session(
            session,
            account.id,
            workspace_id,
            work_id,
            session_id,
            request.expected_revision,
        )
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(code=200, message="试玩场次已取消", data=_session_response(item))


@router.get(
    "/playtest-sessions/{session_id}",
    response_model=ApiResponse[ParticipantSessionResponseData],
    summary="读取受邀试玩场次",
)
def read_participant_session(
    session_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[ParticipantSessionResponseData]:
    try:
        data = service.read_participant_session(session, account.id, session_id)
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(
        code=200,
        message="试玩场次已加载",
        data=ParticipantSessionResponseData(
            id=data.id,
            work_name=data.work_name,
            observation_goal=data.observation_goals,
            recording_method=data.recording_method,
            scheduled_at=data.scheduled_at.isoformat(),
            location=data.location,
            capacity=data.capacity,
            confirmed_count=data.confirmed_count,
            status=data.status,
            own_status=data.own_status,
            rule_name=data.rule_name,
            rule_description=data.rule_description,
            rule_content=data.rule_content,
            materials=[_material_response(item) for item in data.materials],
            latest_notification=_notification_response(data.latest_notification),
        ),
    )


@router.post(
    "/playtest-sessions/{session_id}/confirmation",
    response_model=ApiResponse[ConfirmationResponseData],
    summary="确认参加试玩场次",
)
def confirm_participation(
    session_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[ConfirmationResponseData]:
    try:
        data = service.confirm_participation(session, account.id, session_id)
    except Exception as error:
        _playtest_error(error)
        raise
    return ApiResponse(
        code=200,
        message="已确认参加试玩场次",
        data=ConfirmationResponseData(
            status=data.status,
            confirmed_count=data.confirmed_count,
            capacity=data.capacity,
        ),
    )


@router.get(
    "/playtest-sessions/{session_id}/materials/{file_id}/preview",
    summary="预览本场固定材料",
)
def preview_material(
    session_id: UUID,
    file_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        material = service.open_participant_material(
            session, account.id, session_id, file_id
        )
    except Exception as error:
        _playtest_error(error)
        raise
    return _binary_response(material, "inline")


@router.get(
    "/playtest-sessions/{session_id}/materials/{file_id}/download",
    summary="下载本场固定材料",
)
def download_material(
    session_id: UUID,
    file_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        material = service.open_participant_material(
            session, account.id, session_id, file_id
        )
    except Exception as error:
        _playtest_error(error)
        raise
    return _binary_response(material, "attachment")
