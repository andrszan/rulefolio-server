import logging
import os
import subprocess
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

DB_ENV_NAMES = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
has_postgresql_config = all(name in os.environ for name in DB_ENV_NAMES)

for name, value in {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "5432",
    "DB_NAME": "app",
    "DB_USER": "postgres",
    "DB_PASSWORD": "not-used",
}.items():
    os.environ.setdefault(name, value)

from app import health  # noqa: E402
from app.api import router as api_router  # noqa: E402
from app.core.database import engine, get_db  # noqa: E402


@api_router.get("/_test/versioned")
def versioned_test() -> dict[str, str]:
    return {"status": "ok"}


from app.core.config import Settings  # noqa: E402
from app.core.errors import register_exception_handlers  # noqa: E402
from app.core.middleware import register_middlewares  # noqa: E402
from app.core.responses import ApiResponse, Page, PageParams  # noqa: E402
from app.main import app  # noqa: E402


@app.get("/_test/validation")
def validation_test(value: int) -> dict[str, int]:
    return {"value": value}


@app.get("/_test/http-error")
def http_error_test() -> None:
    raise HTTPException(
        status_code=401, detail="认证失败", headers={"WWW-Authenticate": "Bearer"}
    )


@app.get("/_test/unsafe-http-error")
def unsafe_http_error_test() -> None:
    raise HTTPException(status_code=400, detail={"internal": "不应泄露"})


@app.get("/_test/unhandled")
def unhandled_test() -> None:
    raise RuntimeError("数据库密码不应泄露")


def test_health_does_not_access_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connect() -> None:
        raise AssertionError("/health 不应连接数据库")

    monkeypatch.setattr(engine, "connect", fail_connect)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_available(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def execute(self, statement: object) -> None:
            assert str(statement) == "SELECT 1"

        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(health.engine, "connect", Connection)
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_hides_database_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connect() -> None:
        raise OperationalError("SELECT 1", {}, RuntimeError("secret"))

    monkeypatch.setattr(health.engine, "connect", fail_connect)
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_database_url_and_settings() -> None:
    settings = Settings(
        _env_file=None,
        db_host="127.0.0.1",
        db_port=5432,
        db_name="app",
        db_user="student",
        db_password="p@ss:/%word",
        cors_origins=["https://example.com"],
    )

    assert settings.database_url.drivername == "postgresql+psycopg"
    assert settings.database_url.username == "student"
    assert settings.database_url.password == "p@ss:/%word"
    assert settings.database_url.host == "127.0.0.1"
    assert settings.database_url.port == 5432
    assert settings.database_url.database == "app"
    assert not settings.database_url.query
    assert settings.app_name == "好玩实验室 API"
    assert settings.app_version == "1.0.0"
    assert settings.api_prefix == "/api/v1"
    assert settings.enable_api_docs
    assert settings.log_level == "INFO"
    assert settings.cors_origins == ["https://example.com"]
    assert (
        Settings(
            _env_file=None,
            db_host="127.0.0.1",
            db_port=5432,
            db_name="app",
            db_user="student",
            db_password="not-used",
        ).cors_origins
        == []
    )


def test_get_db_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        closed = False
        committed = False

        def close(self) -> None:
            self.closed = True

        def commit(self) -> None:
            self.committed = True

    session = Session()
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: session)
    dependency = get_db()

    assert next(dependency) is session
    dependency.close()
    assert session.closed
    assert not session.committed


def test_response_models() -> None:
    assert ApiResponse(code=200, message="OK", data={"id": 1}).model_dump() == {
        "code": 200,
        "message": "OK",
        "data": {"id": 1},
    }
    assert Page(items=[1], page=1, size=20, total=1).model_dump() == {
        "items": [1],
        "page": 1,
        "size": 20,
        "total": 1,
    }
    assert PageParams().model_dump() == {"page": 1, "size": 20}


def test_versioned_api_and_documentation() -> None:
    with TestClient(app) as client:
        versioned = client.get("/api/v1/_test/versioned")
        docs = client.get("/api/v1/docs")
        redoc = client.get("/api/v1/redoc")
        schema = client.get("/api/v1/openapi.json")
        root_docs = client.get("/docs")
        versioned_health = client.get("/api/v1/health")

    assert app.title == "好玩实验室 API"
    assert app.version == "1.0.0"
    assert versioned.json() == {"status": "ok"}
    assert docs.status_code == 200
    assert redoc.status_code == 200
    assert schema.status_code == 200
    assert "/api/v1/_test/versioned" in schema.json()["paths"]
    assert root_docs.status_code == 404
    assert versioned_health.status_code == 404


def test_docs_can_be_disabled() -> None:
    environment = {
        **os.environ,
        "ENABLE_API_DOCS": "false",
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "5432",
        "DB_NAME": "app",
        "DB_USER": "postgres",
        "DB_PASSWORD": "not-used",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.main import app; print(app.docs_url, app.redoc_url, app.openapi_url)",
        ],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.stdout.strip() == "None None None"


def test_error_envelopes_and_request_id(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.core.middleware")
    with TestClient(app, raise_server_exceptions=False) as client:
        missing = client.get("/missing", headers={"X-Request-ID": "trace-123"})
        validation = client.get("/_test/validation", params={"value": "not-an-int"})
        http_error = client.get("/_test/http-error")
        unsafe_http_error = client.get("/_test/unsafe-http-error")
        unhandled = client.get("/_test/unhandled")

    assert missing.status_code == 404
    assert missing.json() == {"code": 404, "message": "Not Found", "data": None}
    assert missing.headers["X-Request-ID"] == "trace-123"
    assert validation.status_code == 422
    assert validation.json()["code"] == 422
    assert validation.json()["data"]["errors"][0]["type"] == "int_parsing"
    assert "input" not in validation.json()["data"]["errors"][0]
    assert http_error.status_code == 401
    assert http_error.headers["WWW-Authenticate"] == "Bearer"
    assert http_error.json() == {"code": 401, "message": "认证失败", "data": None}
    assert unsafe_http_error.status_code == 400
    assert unsafe_http_error.json() == {
        "code": 400,
        "message": "Bad Request",
        "data": None,
    }
    assert "不应泄露" not in unsafe_http_error.text
    assert unhandled.status_code == 500
    assert unhandled.json() == {
        "code": 500,
        "message": "Internal Server Error",
        "data": None,
    }
    assert len(validation.headers["X-Request-ID"]) == 32
    assert "not-an-int" not in caplog.text
    assert "数据库密码不应泄露" not in caplog.text


def test_lifespan_disposes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    disposed = False

    def dispose() -> None:
        nonlocal disposed
        disposed = True

    monkeypatch.setattr(engine, "dispose", dispose)
    with TestClient(app):
        pass

    assert disposed


def test_error_cors_and_request_id_are_combined() -> None:
    isolated_app = FastAPI()
    register_exception_handlers(isolated_app)
    register_middlewares(isolated_app, ["https://example.com"])

    @isolated_app.get("/failure")
    def failure() -> None:
        raise RuntimeError("不应泄露")

    with TestClient(isolated_app, raise_server_exceptions=False) as client:
        response = client.get(
            "/failure",
            headers={"Origin": "https://example.com", "X-Request-ID": "trace-456"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "code": 500,
        "message": "Internal Server Error",
        "data": None,
    }
    assert response.headers["X-Request-ID"] == "trace-456"
    assert response.headers["Access-Control-Allow-Origin"] == "https://example.com"


def test_invalid_request_id_is_replaced() -> None:
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "bad value"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "bad value"
    assert len(response.headers["X-Request-ID"]) == 32


@pytest.mark.integration
def test_postgresql_connection() -> None:
    if not has_postgresql_config:
        pytest.skip("需要显式提供全部 DB_* 配置")

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1
