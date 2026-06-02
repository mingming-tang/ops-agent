"""全局配置(从环境变量 / .env 读取)。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str = "sqlite:///./operator_agent.db"
    checkpoint_db_url: str = ""

    secret_encryption_key: str = "CHANGE_ME_GENERATE_A_FERNET_KEY"
    admin_token: str = "change-me"

    require_approval_for_dangerous: bool = True
    # 所有"可执行命令"(SSH / 云操作)执行前都需用户确认(逐条审批)
    require_command_approval: bool = True
    default_dry_run: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
