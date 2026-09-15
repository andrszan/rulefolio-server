from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.access.context import set_actor, set_workspace_management_scope
from app.core.config import settings
from app.core.database import SessionLocal
from app.files import service as files_service
from app.files import storage
from app.files.models import StoredFile
from app.files.policy import PENDING_IMAGE_TTL
from app.identity.models import Account
from app.works import service as works_service
from app.workspaces import service as workspaces_service
from app.workspaces.models import WorkspaceMember

pytestmark = pytest.mark.skipif(
    settings.db_name != "rulefolio_test"
    or settings.s3_bucket_name != settings.s3_test_bucket_name,
    reason="需要 TEST_DB_NAME 和 S3_TEST_BUCKET_NAME",
)

FIXTURES = Path(__file__).parent / "fixtures"


def _create_work(session: Session, actor_id, workspace_id):
    return works_service.create_work(
        session,
        actor_id,
        workspace_id,
        name="图片权限作品",
        description="验证私有图片存储",
        creative_stage="原型",
        target_experience="共同探索",
        min_players=2,
        max_players=4,
        estimated_duration_minutes=60,
    )


def _upload(session: Session, actor_id, workspace_id, work_id):
    with (FIXTURES / "board-game-box.jpg").open("rb") as source:
        return files_service.upload_image(
            session,
            actor_id,
            workspace_id,
            work_id,
            source,
            "图片权限作品.jpg",
            "image/jpeg",
        )


def _reserve_pending(
    session: Session, actor_id: UUID, workspace_id: UUID, work_id: UUID
) -> UUID:
    with (FIXTURES / "board-game-box.jpg").open("rb") as source:
        upload = files_service._read_image(source, "并发恢复图片.jpg", "image/jpeg")
    try:
        file = files_service._reserve_file(
            session, actor_id, workspace_id, work_id, upload
        )
        upload.data.seek(0)
        storage.put_object(file.object_key, upload.data, upload.detected_content_type)
        return file.id
    finally:
        upload.close()


def test_private_image_uses_real_bucket_and_rechecks_work_access() -> None:
    suffix = uuid4().hex
    owner = Account(email=f"files-owner-{suffix}@example.com", status="active")
    collaborator = Account(
        email=f"files-collaborator-{suffix}@example.com", status="active"
    )
    member = Account(email=f"files-member-{suffix}@example.com", status="active")

    with SessionLocal() as session:
        session.add_all((owner, collaborator, member))
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"图片权限-{suffix[:8]}", None
        )
        set_workspace_management_scope(session, workspace.id)
        session.add_all(
            (
                WorkspaceMember(workspace_id=workspace.id, account_id=collaborator.id),
                WorkspaceMember(workspace_id=workspace.id, account_id=member.id),
            )
        )
        session.commit()
        set_actor(session, owner.id)
        work = _create_work(session, owner.id, workspace.id)
        set_actor(session, owner.id)
        works_service.set_work_access(
            session,
            owner.id,
            workspace.id,
            work.id,
            collaborator.id,
            "collaborator",
        )

        set_actor(session, collaborator.id)
        image = _upload(session, collaborator.id, workspace.id, work.id)
        images, total = files_service.list_images(
            session, collaborator.id, workspace.id, work.id, 1, 12
        )
        assert total == 1
        assert [item.id for item in images] == [image.id]
        stream = files_service.open_image(
            session, collaborator.id, workspace.id, work.id, image.id
        )
        try:
            assert stream.body.read() == (FIXTURES / "board-game-box.jpg").read_bytes()
        finally:
            stream.body.close()

        set_actor(session, member.id)
        assert list(session.scalars(select(StoredFile))) == []
        with pytest.raises(works_service.WorkUnavailable):
            files_service.list_images(session, member.id, workspace.id, work.id, 1, 12)

        set_actor(session, owner.id)
        works_service.revoke_work_access(
            session, owner.id, workspace.id, work.id, collaborator.id
        )
        set_actor(session, collaborator.id)
        with pytest.raises(works_service.WorkUnavailable):
            files_service.open_image(
                session, collaborator.id, workspace.id, work.id, image.id
            )

        assert session.execute(
            text(
                "SELECT relrowsecurity AND relforcerowsecurity "
                "FROM pg_class WHERE relname = 'files'"
            )
        ).scalar_one()
        assert not session.execute(
            text("SELECT has_table_privilege(current_user, 'files', 'DELETE')")
        ).scalar_one()
        assert not session.execute(
            text(
                "SELECT has_table_privilege(current_user, 'alembic_version', 'SELECT')"
            )
        ).scalar_one()


def test_concurrent_pending_recovery_preserves_ready_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    owner = Account(email=f"recovery-owner-{suffix}@example.com", status="active")
    collaborator = Account(
        email=f"recovery-collaborator-{suffix}@example.com", status="active"
    )

    with SessionLocal() as session:
        session.add_all((owner, collaborator))
        session.commit()
        set_actor(session, owner.id)
        workspace = workspaces_service.create_workspace(
            session, owner.id, f"并发恢复-{suffix[:8]}", None
        )
        set_workspace_management_scope(session, workspace.id)
        session.add(
            WorkspaceMember(workspace_id=workspace.id, account_id=collaborator.id)
        )
        session.commit()
        set_actor(session, owner.id)
        work = _create_work(session, owner.id, workspace.id)
        set_actor(session, owner.id)
        works_service.set_work_access(
            session,
            owner.id,
            workspace.id,
            work.id,
            collaborator.id,
            "collaborator",
        )
        pending_id = _reserve_pending(session, collaborator.id, workspace.id, work.id)

    monkeypatch.setattr(
        files_service,
        "_now",
        lambda: datetime.now(UTC) + PENDING_IMAGE_TTL + timedelta(seconds=1),
    )
    start = Barrier(2)

    def recover() -> None:
        start.wait(timeout=10)
        with SessionLocal() as session:
            files_service._recover_pending(
                session, collaborator.id, workspace.id, work.id
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(recover) for _ in range(2)]
        for future in futures:
            future.result(timeout=20)

    with SessionLocal() as session:
        images, total = files_service.list_images(
            session, collaborator.id, workspace.id, work.id, 1, 12
        )
        assert total == 1
        assert [image.id for image in images] == [pending_id]
        stream = files_service.open_image(
            session, collaborator.id, workspace.id, work.id, pending_id
        )
        try:
            assert stream.body.read() == (FIXTURES / "board-game-box.jpg").read_bytes()
        finally:
            stream.body.close()
