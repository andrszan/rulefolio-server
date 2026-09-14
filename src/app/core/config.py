import base64
from binascii import Error as BinasciiError
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: SecretStr
    migrator_db_host: str | None = None
    migrator_db_port: int | None = None
    migrator_db_name: str | None = None
    migrator_db_user: str | None = None
    migrator_db_password: SecretStr | None = None
    app_name: str = "好玩实验室 API"
    app_version: str = "1.0.0"
    app_public_url: str = "http://127.0.0.1:3105"
    api_prefix: str = "/api/v1"
    enable_api_docs: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cors_origins: list[str] = Field(default_factory=list)
    password_min_length: int = Field(default=12, ge=8)
    password_max_length: int = Field(default=128, ge=12)
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_cost: int = Field(default=65536, ge=8192)
    argon2_parallelism: int = Field(default=1, ge=1)
    session_ttl_hours: int = Field(default=8, ge=1)
    one_time_token_ttl_minutes: int = Field(default=30, ge=1)
    auth_attempt_window_minutes: int = Field(default=15, ge=1)
    login_max_attempts: int = Field(default=5, ge=1)
    recovery_request_max_attempts: int = Field(default=3, ge=1)
    recovery_response_min_duration_ms: int = Field(default=250, ge=0)
    recovery_job_stale_minutes: int = Field(default=5, ge=1)
    recovery_exchange_max_attempts: int = Field(default=5, ge=1)
    token_encryption_key: SecretStr = SecretStr("")
    token_encryption_key_version: int = Field(default=1, ge=1)
    auth_attempt_pepper: SecretStr = SecretStr("")
    mail_driver: Literal["smtp"] = "smtp"
    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_ssl: bool = True
    smtp_starttls: bool = False
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    mail_from_name: str = "好玩实验室"
    mail_from_address: str = ""
    mail_reply_to: str = ""
    mail_max_attempts: int = Field(default=3, ge=1)
    mail_dispatch_stale_minutes: int = Field(default=5, ge=1)

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @property
    def migrator_database_url(self) -> URL:
        values = (
            self.migrator_db_host,
            self.migrator_db_port,
            self.migrator_db_name,
            self.migrator_db_user,
            self.migrator_db_password,
        )
        if not all(values):
            raise ValueError("迁移必须配置独立的 MIGRATOR_DB_* 身份")
        return URL.create(
            "postgresql+psycopg",
            username=self.migrator_db_user,
            password=self.migrator_db_password.get_secret_value(),
            host=self.migrator_db_host,
            port=self.migrator_db_port,
            database=self.migrator_db_name,
        )

    @property
    def token_encryption_key_bytes(self) -> bytes:
        try:
            key = base64.urlsafe_b64decode(self.token_encryption_key.get_secret_value())
        except BinasciiError as error:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY 必须是 Base64 编码的 32 字节密钥"
            ) from error
        if len(key) != 32:
            raise ValueError("TOKEN_ENCRYPTION_KEY 必须是 Base64 编码的 32 字节密钥")
        return key


settings = Settings()  # type: ignore[call-arg]
