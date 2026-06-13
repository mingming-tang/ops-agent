"""数据模型:服务器、云账号(MCP)、模型供应商、会话、审计日志。

凡是 *_enc 字段都是密文(见 db/crypto.py),通过 Pydantic schema 出入时自动加解密。
"""
import datetime as dt
from enum import Enum

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ProviderType(str, Enum):
    openai = "openai"
    anthropic = "anthropic"
    qwen = "qwen"
    minimax = "minimax"
    deepseek = "deepseek"


class CloudType(str, Enum):
    aliyun = "aliyun"
    cloudflare = "cloudflare"


class CommandLevel(str, Enum):
    readonly = "readonly"      # 只读,自动执行
    mutating = "mutating"      # 变更,执行并记录
    dangerous = "dangerous"    # 危险,强制人工审批


# ----------------------------------------------------------------------------
# 模型供应商:OpenAI / Anthropic / Qwen / MiniMax / DeepSeek
# ----------------------------------------------------------------------------
class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    provider_type: Mapped[ProviderType] = mapped_column(String(32))
    model_name: Mapped[str] = mapped_column(String(120))        # 如 gpt-4o / claude-opus-4-8 / qwen-max
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    temperature: Mapped[float] = mapped_column(default=0.0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ----------------------------------------------------------------------------
# 服务器:SSH 凭证
# ----------------------------------------------------------------------------
class SSHKey(Base):
    """可复用的 SSH 私钥库:添加服务器时可直接选用,避免反复粘贴私钥。"""
    __tablename__ = "ssh_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)     # 如 prod-deploy
    private_key_enc: Mapped[str] = mapped_column(Text)
    passphrase_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(100))
    auth_type: Mapped[str] = mapped_column(String(20), default="password")  # password | key
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    passphrase_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 选用密钥库中的私钥(auth_type=key 且选了已有密钥时);为空则用上面的 private_key_enc
    ssh_key_id: Mapped[int | None] = mapped_column(ForeignKey("ssh_keys.id"), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)          # 如 ["prod", "web"]
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ----------------------------------------------------------------------------
# 云账号:以 MCP server 形式接入。阿里云 / Cloudflare,每个云可多账号。
# transport=stdio 时用 command/args/env;transport=http 时用 url/headers。
# ----------------------------------------------------------------------------
class CloudAccount(Base):
    __tablename__ = "cloud_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)    # 如 aliyun-prod / cf-main
    cloud_type: Mapped[CloudType] = mapped_column(String(32))
    transport: Mapped[str] = mapped_column(String(20), default="stdio")  # stdio | streamable_http

    # stdio 方式
    command: Mapped[str | None] = mapped_column(String(300), nullable=True)
    args: Mapped[list] = mapped_column(JSON, default=list)

    # http 方式
    url: Mapped[str | None] = mapped_column(String(400), nullable=True)

    # 认证信息(env 变量值 / headers 值)统一密文存放
    secrets_enc: Mapped[dict] = mapped_column(JSON, default=dict)   # {"ALIBABA_CLOUD_ACCESS_KEY_ID": "<enc>", ...}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ----------------------------------------------------------------------------
# 会话 + 审计
# ----------------------------------------------------------------------------
class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # LangGraph thread
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | waiting_approval | done
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    audits: Mapped[list["AuditLog"]] = relationship(back_populates="conversation")


class Message(Base):
    """对话消息(用于历史记录查看)。role: user | assistant | tool。"""
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Memory(Base):
    """长期记忆:跨会话持久化的运维知识/事实。

    由 Agent 主动写入(save_memory 工具)或会话结束时自动抽取生成。
    embedding 为可选的语义向量(无 embedding 供应商时为 None,召回降级为关键词匹配)。
    """
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)                  # 记忆正文(一句话事实)
    kind: Mapped[str] = mapped_column(String(32), default="fact")  # fact | preference | runbook | env
    source_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)  # list[float] 语义向量
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AutoApproveRule(Base):
    """用户在审批时勾选"下次不再确认"后记住的命令(精确文本匹配),自动放行。"""
    __tablename__ = "auto_approve_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    command: Mapped[str] = mapped_column(Text)                 # 精确命令文本(已 strip)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TokenUsage(Base):
    """每次大模型调用的 token 用量(输入/输出分开),用于按小时/天/月统计消耗。

    在 llm/registry.py 给模型挂回调,所有调用(主推理、命令分级、摘要、记忆抽取)
    都会落一条记录。
    """
    __tablename__ = "token_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_name: Mapped[str] = mapped_column(String(120), index=True)   # 如 claude-opus-4-8
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 供应商配置名
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class AuditLog(Base):
    """每一次工具执行(SSH/云操作)都留痕,可回放、可追责。"""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)   # 服务器名 / 云账号名
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[CommandLevel] = mapped_column(String(20), default=CommandLevel.readonly)
    approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)    # None=无需审批
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    conversation: Mapped["Conversation | None"] = relationship(back_populates="audits")
