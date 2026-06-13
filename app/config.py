"""全局配置(从环境变量 / .env 读取)。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str = "sqlite:///./ops_agent.db"
    checkpoint_db_url: str = ""

    secret_encryption_key: str = "CHANGE_ME_GENERATE_A_FERNET_KEY"
    admin_token: str = "change-me"

    # 控制台登录(单用户,凭证写在 .env)。AUTH_PASSWORD 为空时不启用认证。
    auth_username: str = "admin"
    auth_password: str = ""
    session_max_age: int = 7 * 24 * 3600   # 登录态有效期(秒)

    require_approval_for_dangerous: bool = True
    # 所有"可执行命令"(SSH / 云操作)执行前都需用户确认(逐条审批)
    require_command_approval: bool = True
    default_dry_run: bool = False

    # 长期记忆:跨会话持久化关键事实。
    memory_enabled: bool = True            # 是否启用 save_memory/recall_memory 工具与召回注入
    memory_auto_extract: bool = True       # 会话结束/压缩时自动抽取事实入库
    memory_top_k: int = 5                  # 每轮注入提示词的记忆条数
    embedding_model: str = ""              # 覆盖默认 embedding 模型名(留空=按供应商默认)

    # 消息压缩:单会话过长时把旧消息摘要化,控制上下文体积。
    compress_enabled: bool = True
    compress_token_threshold: int = 12000  # 估算 token 超过此值触发压缩
    compress_keep_recent: int = 8          # 压缩时保留最近多少条原始消息

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
