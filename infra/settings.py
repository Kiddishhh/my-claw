"""
infra/settings.py —— my-claw 的配置中枢

- 所有 API Key / Base URL / 连接串统一从这里取，不散落在业务代码里
- Pydantic Settings 做类型校验，缺失关键项时给清晰报错
- 字段前缀 SYC_ 是既有环境变量契约，保留以兼容已部署环境
"""
import os
from dotenv import dotenv_values
from functools import lru_cache
from pathlib import Path
from pydantic import SecretStr, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SYC_",
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== 模型 =====
    model_name: str = "deepseek-flash"
    model_provider: str = "openai"
    api_key: SecretStr                    # 用 SecretStr，避免密钥被 print/log 泄露
    base_url: str = "https://api.deepseek.com"

    # ===== 连接弹性 =====
    max_retries: int = 12
    timeout: int = 60

    # ===== 运行 =====
    log_level: str = "INFO"


class ResourceSettings(BaseSettings):
    """基础服务不要求模型密钥；连接串不在 repr 中显示。"""
    model_config = Settings.model_config.copy()
    postgres_url: SecretStr = SecretStr("postgresql://my_claw:my_claw@localhost:5432/my_claw")
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    pg_min_size: int = Field(default=1, ge=1)
    pg_max_size: int = Field(default=10, ge=1)
    redis_max_connections: int = Field(default=20, ge=1)
    resource_timeout: float = Field(default=5, gt=0)

    @model_validator(mode="after")
    def validate_pool_sizes(self):
        if self.pg_max_size < self.pg_min_size:
            raise ValueError("pg_max_size 不能小于 pg_min_size")
        return self


class LoggingSettings(BaseSettings):
    """日志仅需要级别；纯计算工具在无模型密钥的沙箱内也能运行。

    沿用 SYC_LOG_LEVEL 和根目录 .env 的配置契约，不加载模型或追踪环境。
    """
    model_config = Settings.model_config.copy()
    log_level: str = "INFO"


class SandboxSettings(BaseSettings):
    """沙箱配置独立于模型配置，回收程序无需读取或验证模型密钥。

    进程环境优先于 .env。DAYTONA_API_KEY 只给本机 SDK 使用，
    绝不能把本对象 model_dump() 后整体作为沙箱的 env_vars。
    """

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8", extra="ignore",
    )
    daytona_api_key: SecretStr
    daytona_api_url: str = "https://app.daytona.io/api"
    # 只用于旧的平台自身开发验收；正式 Web 会话默认空工作区，平台源码不上传。
    syc_sandbox_seed_platform: bool = False
    syc_sandbox_idle_seconds: int = Field(default=3600, gt=0)
    syc_sandbox_cleanup_seconds: int = Field(default=60, ge=10)
    syc_sandbox_command_timeout: int = Field(default=300, gt=0)
    syc_sandbox_task_timeout: int = Field(default=600, gt=0)
    syc_sandbox_auto_stop_minutes: int = Field(default=30, gt=0)
    syc_sandbox_auto_delete_minutes: int = Field(default=1440, gt=0)


def load_langsmith_environment(env_file: Path | None = None) -> None:
    """将 .env 的 LangSmith 配置补入进程环境，保留显式环境变量覆盖。"""
    values = dotenv_values(env_file or Path(__file__).resolve().parents[1] / ".env")
    changed = False
    for key in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT",
                "LANGSMITH_PROJECT", "LANGSMITH_WORKSPACE_ID"):
        value = values.get(key)
        if value is not None and key not in os.environ:
            os.environ[key] = value
            changed = True
    if changed:
        from langsmith import utils
        utils.get_env_var.cache_clear()


@lru_cache
def get_settings() -> Settings:
    """全进程单例。业务代码统一通过它拿配置。"""
    settings = Settings()
    load_langsmith_environment()
    return settings
