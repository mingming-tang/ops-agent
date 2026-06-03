"""SSH 执行工具。

把"对某台服务器执行命令"封装成一个 LangChain 工具暴露给 Agent。
凭证从 DB 取(密文解密),用 asyncssh 异步执行,带超时。
真正的危险判定/审批不在这里,而在 guardrail 节点(见 agent/guardrails.py),
本工具只负责"安全连接 + 执行 + 回传结果"。
"""
import asyncio

import asyncssh
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.db.base import SessionLocal
from app.db.crypto import decrypt
from app.db.models import Server


class SSHRunInput(BaseModel):
    server_name: str = Field(description="目标服务器名称(在后台已登记)")
    command: str = Field(description="要在服务器上执行的 shell 命令")
    timeout: int = Field(default=60, description="超时秒数")


async def _run_on_server(server_name: str, command: str, timeout: int = 60) -> str:
    with SessionLocal() as db:
        server = db.query(Server).filter(Server.name == server_name).first()
        if server is None:
            return f"[错误] 未找到服务器 '{server_name}',请先在后台登记。"
        conn_kwargs: dict = {
            "host": server.host,
            "port": server.port,
            "username": server.username,
            "known_hosts": None,  # 生产应配置 known_hosts 做主机指纹校验
        }
        if server.auth_type == "key":
            key = decrypt(server.private_key_enc)
            passphrase = decrypt(server.passphrase_enc)
            conn_kwargs["client_keys"] = [asyncssh.import_private_key(key, passphrase)]
        else:
            conn_kwargs["password"] = decrypt(server.password_enc)

    try:
        async with asyncssh.connect(**conn_kwargs) as conn:
            result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            parts = [f"[exit={result.exit_status}]"]
            if out:
                parts.append(f"stdout:\n{out}")
            if err:
                parts.append(f"stderr:\n{err}")
            return "\n".join(parts)
    except asyncio.TimeoutError:
        return f"[错误] 命令超时(>{timeout}s):{command}"
    except (OSError, asyncssh.Error) as e:
        return f"[错误] SSH 连接/执行失败:{e}"


ssh_run_tool = StructuredTool.from_function(
    coroutine=_run_on_server,
    name="ssh_run",
    description=(
        "在指定服务器上通过 SSH 执行 shell 命令并返回 stdout/stderr/exit code。"
        "用于巡检、诊断、变更等运维操作。先用 list_servers 查看可用服务器。"
    ),
    args_schema=SSHRunInput,
)


async def test_server_connection(server_name: str, timeout: int = 10) -> dict:
    """测试到某台服务器的 SSH 连通性,返回 {ok, detail}。"""
    result = await _run_on_server(server_name, "whoami; hostname; uptime", timeout=timeout)
    return {"ok": not result.startswith("[错误]"), "detail": result}


def list_servers() -> str:
    """列出后台已登记的所有服务器(名称、地址、标签),供 Agent 选目标。"""
    with SessionLocal() as db:
        servers = db.query(Server).all()
        if not servers:
            return "当前没有已登记的服务器。"
        lines = [f"- {s.name}  {s.username}@{s.host}:{s.port}  tags={s.tags}" for s in servers]
        return "可用服务器:\n" + "\n".join(lines)


list_servers_tool = StructuredTool.from_function(
    func=list_servers,
    name="list_servers",
    description="列出所有可用服务器及其标签,用于选择操作目标。",
)


def make_scoped_ssh_tools(allowed: set[str] | None) -> list[StructuredTool]:
    """按"当前操作对象"限定 SSH 工具。allowed=None 表示不限制(可操作全部已登记服务器)。"""

    async def _scoped_run(server_name: str, command: str, timeout: int = 60) -> str:
        if allowed is not None and server_name not in allowed:
            return (f"[错误] 本次会话被限定只能操作:{', '.join(sorted(allowed))};"
                    f"不允许操作 '{server_name}'。")
        return await _run_on_server(server_name, command, timeout)

    def _scoped_list() -> str:
        with SessionLocal() as db:
            servers = [s for s in db.query(Server).all()
                       if allowed is None or s.name in allowed]
        if not servers:
            return "当前没有可操作的服务器。"
        lines = [f"- {s.name}  {s.username}@{s.host}:{s.port}  tags={s.tags}" for s in servers]
        return "可用服务器:\n" + "\n".join(lines)

    return [
        StructuredTool.from_function(func=_scoped_list, name="list_servers",
                                     description="列出当前可操作的服务器及标签。"),
        StructuredTool.from_function(coroutine=_scoped_run, name="ssh_run",
                                     description=ssh_run_tool.description, args_schema=SSHRunInput),
    ]
