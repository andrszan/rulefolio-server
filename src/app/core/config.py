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
    app_name: str = "好玩实验室 API"
    app_version: str = "1.0.0"
    api_prefix: str = "/api/v1"
    enable_api_docs: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cors_origins: list[str] = Field(default_factory=list)

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


# db_* 等必填项由环境变量 / .env 注入；IDE 会误报构造参数未填写。
settings = Settings()  # type: ignore[call-arg]
