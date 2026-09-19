from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from re import sub
from tempfile import TemporaryFile
from typing import BinaryIO
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access.context import set_actor
from app.files import service as files_service
from app.issues import service as issues_service
from app.playtests import service as playtests_service
from app.works import service as works_service


class WorkExportUnavailable(Exception):
    pass


class WorkExportRetryable(Exception):
    pass


PAGE_SIZE = 1_000


@dataclass
class WorkExport:
    body: BinaryIO
    filename: str


def _json_default(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"无法编码 {type(value).__name__}")


def _safe_display_name(value: str) -> str:
    value = sub(r"[\\/\x00-\x1f]+", "_", value).strip(" .")
    return value[:160] or "material"


def _archive_name(work_name: str) -> str:
    return f"{_safe_display_name(work_name)}-作品资料.zip"


def _read_stream(stream: files_service.ImageStream) -> bytes:
    try:
        return stream.body.read()
    finally:
        stream.body.close()


def _all_pages[PageItem](
    fetch: Callable[[int, int], tuple[list[PageItem], int]],
) -> list[PageItem]:
    result: list[PageItem] = []
    page = 1
    while True:
        items, total = fetch(page, PAGE_SIZE)
        result.extend(items)
        if len(result) == total:
            return result
        if len(result) > total or not items:
            raise WorkExportRetryable
        page += 1


def _append_manager_data(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
) -> tuple[dict[str, object], dict[UUID, playtests_service.MaterialData]]:
    plans = _all_pages(
        lambda page, size: playtests_service.list_plans(
            session, actor_id, workspace_id, work_id, page, size
        )
    )
    plan_data = []
    results = []
    feedback = []
    materials: dict[UUID, playtests_service.MaterialData] = {}
    for summary in plans:
        plan = playtests_service.read_plan(
            session, actor_id, workspace_id, work_id, summary.id
        )
        plan_data.append(asdict(plan))
        for item in plan.sessions:
            for material in item.materials:
                materials.setdefault(material.id, material)
            if item.status != "started":
                continue
            result = playtests_service.read_result(
                session, actor_id, workspace_id, work_id, item.id
            )
            results.append(asdict(result))
            if result.actual_material is not None:
                for material in result.actual_material.materials:
                    materials.setdefault(material.id, material)
            feedback.append(
                {
                    "sessionId": item.id,
                    "data": asdict(
                        playtests_service.read_feedback(
                            session, actor_id, workspace_id, work_id, item.id
                        )
                    ),
                }
            )
    return {
        "plans": plan_data,
        "results": results,
        "feedback": feedback,
    }, materials


def _append_maintainer_data(
    session: Session,
    actor_id: UUID,
    workspace_id: UUID,
    work_id: UUID,
) -> list[dict[str, object]]:
    issues = _all_pages(
        lambda page, size: issues_service.list_issues(
            session, actor_id, workspace_id, work_id, page, size
        )
    )
    result = []
    for item in issues:
        detail = issues_service.read_issue(
            session, actor_id, workspace_id, work_id, item.id
        )
        evidence = _all_pages(
            lambda page, size: issues_service.list_issue_evidence(
                session, actor_id, workspace_id, work_id, item.id, page, size
            )
        )
        retests = _all_pages(
            lambda page, size: issues_service.list_retests(
                session, actor_id, workspace_id, work_id, item.id, page, size
            )
        )
        result.append(
            {
                "issue": asdict(detail),
                "evidence": [asdict(source) for source in evidence],
                "retests": [asdict(retest) for retest in retests],
            }
        )
    return result


def build_work_export(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> WorkExport:
    archive = TemporaryFile(mode="w+b")
    try:
        session.rollback()
        session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        set_actor(session, actor_id)
        work = works_service.read_work(session, actor_id, workspace_id, work_id)
        rule_materials = works_service.read_rule_materials(
            session, actor_id, workspace_id, work_id
        )
        current_materials = {
            material.id: material for material in rule_materials.materials
        }
        data: dict[str, object] = {
            "work": asdict(work),
            "currentRule": {
                "name": rule_materials.rule_name,
                "description": rule_materials.rule_description,
                "content": rule_materials.rule_content,
                "revision": rule_materials.revision,
                "materials": [
                    asdict(material) for material in rule_materials.materials
                ],
            },
        }
        manager_materials: dict[UUID, playtests_service.MaterialData] = {}
        if work.own_role in {"maintainer", "organizer"}:
            manager_data, manager_materials = _append_manager_data(
                session, actor_id, workspace_id, work_id
            )
            data.update(manager_data)
        if work.own_role == "maintainer":
            data["issues"] = _append_maintainer_data(
                session, actor_id, workspace_id, work_id
            )

        manifest_files = []
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
            for file_id, material in sorted(
                current_materials.items(), key=lambda item: str(item[0])
            ):
                set_actor(session, actor_id)
                stream = files_service.open_material(
                    session, actor_id, workspace_id, work_id, file_id
                )
                body = _read_stream(stream)
                digest = sha256(body).hexdigest()
                if len(body) != material.size_bytes or digest != material.sha256:
                    raise WorkExportRetryable
                path = (
                    f"materials/{file_id}-{_safe_display_name(material.display_name)}"
                )
                zip_file.writestr(path, body)
                manifest_files.append(
                    {
                        "id": file_id,
                        "path": path,
                        "contentType": material.detected_content_type,
                        "sizeBytes": len(body),
                        "sha256": digest,
                    }
                )
            for file_id, material in sorted(
                manager_materials.items(), key=lambda item: str(item[0])
            ):
                if file_id in current_materials:
                    continue
                stream = files_service.open_playtest_material(session, file_id)
                body = _read_stream(stream)
                digest = sha256(body).hexdigest()
                if len(body) != material.size_bytes or digest != material.sha256:
                    raise WorkExportRetryable
                path = (
                    f"materials/{file_id}-{_safe_display_name(material.display_name)}"
                )
                zip_file.writestr(path, body)
                manifest_files.append(
                    {
                        "id": file_id,
                        "path": path,
                        "contentType": material.detected_content_type,
                        "sizeBytes": len(body),
                        "sha256": digest,
                    }
                )
            zip_file.writestr(
                "manifest.json",
                dumps(
                    {
                        "format": "rulefolio-work-export",
                        "formatVersion": 1,
                        "exportedAt": datetime.now(UTC),
                        "workspaceId": workspace_id,
                        "workId": work_id,
                        "files": manifest_files,
                    },
                    ensure_ascii=False,
                    default=_json_default,
                    sort_keys=True,
                ),
            )
            zip_file.writestr(
                "data.json",
                dumps(data, ensure_ascii=False, default=_json_default, sort_keys=True),
            )
        session.rollback()
        archive.seek(0)
        return WorkExport(archive, _archive_name(work.name))
    except (
        works_service.WorkUnavailable,
        works_service.WorkManagementForbidden,
        playtests_service.PlaytestManagementForbidden,
        issues_service.IssueManagementForbidden,
    ) as error:
        session.rollback()
        archive.close()
        raise WorkExportUnavailable from error
    except WorkExportRetryable:
        session.rollback()
        archive.close()
        raise
    except (files_service.FileOperationRetryable, SQLAlchemyError) as error:
        session.rollback()
        archive.close()
        raise WorkExportRetryable from error
    except Exception as error:
        session.rollback()
        archive.close()
        raise WorkExportRetryable from error
