from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access.context import set_file_lifecycle_scope
from app.files.models import StoredFile
from app.works.models import WorkMaterialFile


@dataclass(frozen=True)
class MaterialData:
    id: UUID
    display_name: str
    detected_content_type: str
    size_bytes: int
    created_at: datetime


def material_data(file: StoredFile) -> MaterialData:
    return MaterialData(
        id=file.id,
        display_name=file.display_name,
        detected_content_type=file.detected_content_type,
        size_bytes=file.size_bytes,
        created_at=file.created_at,
    )


def selected_ready_materials(
    session: Session, workspace_id: UUID, work_id: UUID, file_ids: set[UUID]
) -> list[StoredFile]:
    if not file_ids:
        return []
    return list(
        session.scalars(
            select(StoredFile)
            .where(
                StoredFile.id.in_(file_ids),
                StoredFile.workspace_id == workspace_id,
                StoredFile.work_id == work_id,
                StoredFile.kind == "material",
                StoredFile.status == "ready",
            )
            .order_by(StoredFile.created_at, StoredFile.id)
        )
    )


def is_current_material(session: Session, work_id: UUID, file_id: UUID) -> bool:
    return (
        session.scalar(
            select(WorkMaterialFile.file_id).where(
                WorkMaterialFile.work_id == work_id,
                WorkMaterialFile.file_id == file_id,
            )
        )
        is not None
    )


def current_ready_materials(session: Session, work_id: UUID) -> list[MaterialData]:
    file_ids = session.scalars(
        select(WorkMaterialFile.file_id).where(WorkMaterialFile.work_id == work_id)
    )
    result = []
    for file_id in file_ids:
        set_file_lifecycle_scope(session, file_id)
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.kind == "material",
                StoredFile.status == "ready",
            )
        )
        if file is not None:
            result.append(material_data(file))
    return sorted(result, key=lambda material: (material.created_at, material.id))
