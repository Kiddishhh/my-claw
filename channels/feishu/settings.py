"""飞书专用配置，不改动 Agent 引擎的配置模型。"""
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeishuSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FEISHU_",
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8", extra="ignore",
    )
    app_id: str = Field(min_length=1)
    app_secret: SecretStr
    queue_size: int = Field(default=50, ge=1, le=1000)

    @field_validator("app_id")
    @classmethod
    def nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("FEISHU_APP_ID 不能为空")
        return value.strip()

    @field_validator("app_secret")
    @classmethod
    def nonblank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("FEISHU_APP_SECRET 不能为空")
        return value
