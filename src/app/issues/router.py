import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import api_error
from app.core.responses import ApiResponse, Page, PageParams
from app.evidence import service as evidence_service
from app.identity import service as identity_service
from app.identity.models import Account
from app.identity.router import _bearer_token
from app.issues import service
from app.workspaces import service as workspaces_service

router = APIRouter(tags=["issues"])


class IssueEvidenceReferenceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_type: str = Field(validation_alias="sourceType")
    source_id: UUID = Field(validation_alias="sourceId")


class IssueCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    description: str
    decision: str
    reason: str
    status: str = "open"
    sources: list[IssueEvidenceReferenceRequest]


class IssueUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    description: str
    decision: str
    reason: str
    adjustment_note: str | None = Field(default=None, validation_alias="adjustmentNote")
    status: str
    expected_revision: int = Field(validation_alias="expectedRevision")


class IssueEvidenceAddRequest(IssueEvidenceReferenceRequest):
    expected_revision: int = Field(validation_alias="expectedRevision")


class IssueRevisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    expected_revision: int = Field(validation_alias="expectedRevision")


class IssueRetestConclusionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    conclusion: str
    reason: str
    status: str
    expected_revision: int = Field(validation_alias="expectedRevision")


class IssueEvidenceAnswerResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    item_id: UUID = Field(serialization_alias="itemId")
    kind: str
    question: str
    text_value: str | None = Field(serialization_alias="textValue")
    option_id: UUID | None = Field(serialization_alias="optionId")
    option_label: str | None = Field(serialization_alias="optionLabel")
    number_value: float | None = Field(serialization_alias="numberValue")


class IssueEvidenceSessionResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    status: str
    location: str
    scheduled_at: str = Field(serialization_alias="scheduledAt")
    started_at: str | None = Field(serialization_alias="startedAt")


class IssueEvidenceResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    link_id: UUID = Field(serialization_alias="linkId")
    source_type: str = Field(serialization_alias="sourceType")
    source_id: UUID = Field(serialization_alias="sourceId")
    session: IssueEvidenceSessionResponseData
    kind: str | None
    content: str | None
    source: str | None
    temporary_alias: str | None = Field(serialization_alias="temporaryAlias")
    recorded_by_account_id: UUID = Field(serialization_alias="recordedByAccountId")
    recorded_by_email: str = Field(serialization_alias="recordedByEmail")
    recorded_at: str | None = Field(serialization_alias="recordedAt")
    updated_at: str | None = Field(serialization_alias="updatedAt")
    answers: list[IssueEvidenceAnswerResponseData]


class IssueConclusionResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    retest_id: UUID = Field(serialization_alias="retestId")
    conclusion: str
    reason: str


class IssueResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    description: str
    decision: str
    reason: str
    adjustment_note: str | None = Field(serialization_alias="adjustmentNote")
    verification_status: str = Field(serialization_alias="verificationStatus")
    current_conclusion: IssueConclusionResponseData | None = Field(
        serialization_alias="currentConclusion"
    )
    status: str
    revision: int
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")
    source_count: int = Field(serialization_alias="sourceCount")


class IssueRetestResponseData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    plan_id: UUID = Field(serialization_alias="planId")
    session_id: UUID = Field(serialization_alias="sessionId")
    scheduled_at: str = Field(serialization_alias="scheduledAt")
    location: str
    status: str
    rule_name: str = Field(serialization_alias="ruleName")
    actual_material_recorded: bool = Field(serialization_alias="actualMaterialRecorded")
    current_adjustment: bool = Field(serialization_alias="currentAdjustment")
    conclusion: str | None
    conclusion_reason: str | None = Field(serialization_alias="conclusionReason")


def _authenticated_account(
    token: Annotated[str, Depends(_bearer_token)], session: Session = Depends(get_db)
) -> Account:
    try:
        return identity_service.authenticate(session, token).account
    except identity_service.SessionUnavailable as error:
        raise api_error(
            401, "当前会话不可用", "session_unavailable", {"WWW-Authenticate": "Bearer"}
        ) from error


def _required_idempotency_key(
    value: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if value is None or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None:
        raise api_error(422, "问题请求参数有误", "issue_invalid")
    return value


def _reference(
    request: IssueEvidenceReferenceRequest,
) -> evidence_service.IssueEvidenceReference:
    source_type = (
        "feedback_submission"
        if request.source_type == "feedbackSubmission"
        else request.source_type
    )
    return evidence_service.IssueEvidenceReference(
        source_type=source_type, source_id=request.source_id
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _issue_response(data: service.IssueData) -> IssueResponseData:
    return IssueResponseData(
        id=data.id,
        description=data.description,
        decision=data.decision,
        reason=data.reason,
        adjustment_note=data.adjustment_note,
        verification_status=data.verification_status,
        current_conclusion=(
            IssueConclusionResponseData(
                retest_id=data.current_conclusion.retest_id,
                conclusion=data.current_conclusion.conclusion,
                reason=data.current_conclusion.reason,
            )
            if data.current_conclusion is not None
            else None
        ),
        status=data.status,
        revision=data.revision,
        created_at=data.created_at.isoformat(),
        updated_at=data.updated_at.isoformat(),
        source_count=data.source_count,
    )


def _issue_retest_response(data: service.IssueRetestData) -> IssueRetestResponseData:
    return IssueRetestResponseData(
        id=data.id,
        plan_id=data.plan_id,
        session_id=data.session_id,
        scheduled_at=data.scheduled_at.isoformat(),
        location=data.location,
        status=data.status,
        rule_name=data.rule_name,
        actual_material_recorded=data.actual_material_recorded,
        current_adjustment=data.current_adjustment,
        conclusion=data.conclusion,
        conclusion_reason=data.conclusion_reason,
    )


def _issue_evidence_response(
    data: service.IssueEvidenceData,
) -> IssueEvidenceResponseData:
    source = data.source
    return IssueEvidenceResponseData(
        link_id=data.link_id,
        source_type=(
            "feedbackSubmission"
            if source.source_type == "feedback_submission"
            else source.source_type
        ),
        source_id=source.source_id,
        session=IssueEvidenceSessionResponseData(
            id=source.session_id,
            status=source.session_status,
            location=source.location,
            scheduled_at=source.scheduled_at.isoformat(),
            started_at=_iso(source.started_at),
        ),
        kind=source.kind,
        content=source.content,
        source=source.source,
        temporary_alias=source.temporary_alias,
        recorded_by_account_id=source.recorded_by_account_id,
        recorded_by_email=source.recorded_by_email,
        recorded_at=_iso(source.recorded_at),
        updated_at=_iso(source.updated_at),
        answers=[
            IssueEvidenceAnswerResponseData(
                item_id=answer.item_id,
                kind=answer.kind,
                question=answer.question,
                text_value=answer.text_value,
                option_id=answer.option_id,
                option_label=answer.option_label,
                number_value=answer.number_value,
            )
            for answer in source.answers
        ],
    )


def _issue_error(error: Exception) -> None:
    if isinstance(error, workspaces_service.WorkspaceExitInProgress):
        raise api_error(
            409, "工作空间正在退出", "workspace_exit_in_progress"
        ) from error
    if isinstance(error, service.IssueUnavailable):
        raise api_error(404, "问题内容不可用", "issue_unavailable") from error
    if isinstance(error, service.IssueManagementForbidden):
        raise api_error(
            403, "当前会话不能管理该作品的问题", "issue_management_forbidden"
        ) from error
    if isinstance(error, service.IssueInvalid):
        raise api_error(422, "问题内容有误", "issue_invalid") from error
    if isinstance(error, service.IssueRevisionConflict):
        raise api_error(
            409,
            "问题已被更新，请重新加载后核对",
            "issue_revision_conflict",
        ) from error
    if isinstance(error, service.IssueRetestResultRequired):
        raise api_error(
            409,
            "请先开始该复测场次并保存实际材料，再记录结论。",
            "issue_retest_result_required",
        ) from error
    if isinstance(error, service.IssueRetestEvidenceRequired):
        raise api_error(
            409,
            "请先关联该复测场次的现场观察或已提交反馈，再记录结论。",
            "issue_retest_evidence_required",
        ) from error
    if isinstance(error, service.IssueOperationConflict):
        raise api_error(
            409, "此操作已用于另一问题", "issue_operation_conflict"
        ) from error
    if isinstance(error, service.IssueEvidenceAlreadyLinked):
        raise api_error(
            409, "该来源已关联到此问题", "issue_evidence_already_linked"
        ) from error
    if isinstance(error, service.IssueLastEvidenceRequired):
        raise api_error(
            409, "问题至少需要保留一条来源", "issue_last_evidence_required"
        ) from error
    if isinstance(error, service.IssueOperationRetryable):
        raise api_error(
            503,
            "当前操作暂时无法完成，请重试。",
            "issue_operation_retryable",
            {"Retry-After": "1"},
        ) from error
    raise error


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/issues",
    response_model=ApiResponse[Page[IssueResponseData]],
    summary="列出作品问题",
)
def list_issues(
    workspace_id: UUID,
    work_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[IssueResponseData]]:
    try:
        issues, total = service.list_issues(
            session, account.id, workspace_id, work_id, params.page, params.size
        )
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(
        code=200,
        message="问题列表已加载",
        data=Page(
            items=[_issue_response(issue) for issue in issues],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.post(
    "/workspaces/{workspace_id}/works/{work_id}/issues",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[IssueResponseData],
    summary="从现有来源归纳问题",
)
def create_issue(
    workspace_id: UUID,
    work_id: UUID,
    request: IssueCreateRequest,
    operation_key: Annotated[str, Depends(_required_idempotency_key)],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[IssueResponseData]:
    try:
        issue = service.create_issue(
            session,
            account.id,
            workspace_id,
            work_id,
            operation_key,
            description=request.description,
            decision=request.decision,
            reason=request.reason,
            status=request.status,
            references=tuple(_reference(source) for source in request.sources),
        )
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(code=201, message="问题已创建", data=_issue_response(issue))


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/issues/{issue_id}",
    response_model=ApiResponse[IssueResponseData],
    summary="读取问题当前判断",
)
def read_issue(
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[IssueResponseData]:
    try:
        issue = service.read_issue(session, account.id, workspace_id, work_id, issue_id)
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(code=200, message="问题已加载", data=_issue_response(issue))


@router.patch(
    "/workspaces/{workspace_id}/works/{work_id}/issues/{issue_id}",
    response_model=ApiResponse[IssueResponseData],
    summary="更新问题当前判断",
)
def update_issue(
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    request: IssueUpdateRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[IssueResponseData]:
    try:
        issue = service.update_issue(
            session,
            account.id,
            workspace_id,
            work_id,
            issue_id,
            description=request.description,
            decision=request.decision,
            reason=request.reason,
            adjustment_note=request.adjustment_note,
            status=request.status,
            expected_revision=request.expected_revision,
        )
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(code=200, message="问题已更新", data=_issue_response(issue))


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/issues/{issue_id}/retests",
    response_model=ApiResponse[Page[IssueRetestResponseData]],
    summary="列出问题关联复测场次",
)
def list_retests(
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[IssueRetestResponseData]]:
    try:
        retests, total = service.list_retests(
            session,
            account.id,
            workspace_id,
            work_id,
            issue_id,
            params.page,
            params.size,
        )
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(
        code=200,
        message="关联复测场次已加载",
        data=Page(
            items=[_issue_retest_response(retest) for retest in retests],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.patch(
    "/workspaces/{workspace_id}/works/{work_id}/issues/{issue_id}/retests/{retest_id}/conclusion",
    response_model=ApiResponse[IssueResponseData],
    summary="记录问题当前结论",
)
def save_retest_conclusion(
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    retest_id: UUID,
    request: IssueRetestConclusionRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[IssueResponseData]:
    try:
        issue = service.save_retest_conclusion(
            session,
            account.id,
            workspace_id,
            work_id,
            issue_id,
            retest_id,
            conclusion=request.conclusion,
            reason=request.reason,
            status=request.status,
            expected_revision=request.expected_revision,
        )
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(code=200, message="当前结论已记录", data=_issue_response(issue))


@router.get(
    "/workspaces/{workspace_id}/works/{work_id}/issues/{issue_id}/evidence",
    response_model=ApiResponse[Page[IssueEvidenceResponseData]],
    summary="读取问题关联来源",
)
def list_issue_evidence(
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    params: Annotated[PageParams, Depends()],
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[Page[IssueEvidenceResponseData]]:
    try:
        evidence, total = service.list_issue_evidence(
            session,
            account.id,
            workspace_id,
            work_id,
            issue_id,
            params.page,
            params.size,
        )
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(
        code=200,
        message="问题来源已加载",
        data=Page(
            items=[_issue_evidence_response(item) for item in evidence],
            page=params.page,
            size=params.size,
            total=total,
        ),
    )


@router.post(
    "/workspaces/{workspace_id}/works/{work_id}/issues/{issue_id}/evidence",
    response_model=ApiResponse[IssueResponseData],
    summary="关联问题来源",
)
def add_issue_evidence(
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    request: IssueEvidenceAddRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[IssueResponseData]:
    try:
        issue = service.add_issue_evidence(
            session,
            account.id,
            workspace_id,
            work_id,
            issue_id,
            request.expected_revision,
            _reference(request),
        )
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(code=200, message="问题来源已关联", data=_issue_response(issue))


@router.delete(
    "/workspaces/{workspace_id}/works/{work_id}/issues/{issue_id}/evidence/{link_id}",
    response_model=ApiResponse[IssueResponseData],
    summary="移除问题误关联来源",
)
def remove_issue_evidence(
    workspace_id: UUID,
    work_id: UUID,
    issue_id: UUID,
    link_id: UUID,
    request: IssueRevisionRequest,
    account: Annotated[Account, Depends(_authenticated_account)],
    session: Session = Depends(get_db),
) -> ApiResponse[IssueResponseData]:
    try:
        issue = service.remove_issue_evidence(
            session,
            account.id,
            workspace_id,
            work_id,
            issue_id,
            link_id,
            request.expected_revision,
        )
    except Exception as error:
        _issue_error(error)
        raise
    return ApiResponse(code=200, message="问题来源已移除", data=_issue_response(issue))
