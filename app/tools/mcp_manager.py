"""MCP 管理器:把后台登记的云账号(阿里云 / Cloudflare,可多账号)动态加载成工具。

每个 CloudAccount 是一个 MCP server。用 langchain-mcp-adapters 的 MultiServerMCPClient
统一拉起,工具名加上账号前缀(如 `aliyun-prod__RunInstances`)避免多账号冲突。
"""
from langchain_core.tools import BaseTool

from app.db.crypto import decrypt
from app.db.models import CloudAccount


def _account_to_server_config(acc: CloudAccount) -> dict:
    """把一个云账号转成 MultiServerMCPClient 需要的连接配置。"""
    secrets = {k: decrypt(v) for k, v in (acc.secrets_enc or {}).items()}

    if acc.transport == "streamable_http":
        return {
            "transport": "streamable_http",
            "url": acc.url,
            "headers": secrets,  # 如 {"Authorization": "Bearer <token>"}
        }
    # 默认 stdio:把密钥作为环境变量注入子进程
    return {
        "transport": "stdio",
        "command": acc.command,
        "args": acc.args or [],
        "env": secrets,
    }


async def test_cloud_account(acc: CloudAccount) -> dict:
    """测试单个云账号 MCP 是否可连、能拉到工具。

    成功:{ok:True, tool_count, tools}
    失败:{ok:False, error: "类型: 消息", traceback: 完整堆栈};同时把完整堆栈
          写入服务端日志(/tmp/operator_agent.log)。stdio 子进程自身的 stderr 也会
          直接打到该日志,是排查"命令能跑但 MCP 起不来"类问题的关键。
    """
    import logging
    import traceback

    from langchain_mcp_adapters.client import MultiServerMCPClient

    logger = logging.getLogger("operator_agent.mcp")
    try:
        client = MultiServerMCPClient({acc.name: _account_to_server_config(acc)})
        tools = await client.get_tools(server_name=acc.name)
        return {"ok": True, "tool_count": len(tools), "tools": [t.name for t in tools[:20]]}
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("测试云账号 '%s' 失败:\n%s", acc.name, tb)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb}


async def load_cloud_tools(accounts: list[CloudAccount]) -> list[BaseTool]:
    """根据传入的云账号列表,返回它们暴露的所有 MCP 工具(带账号前缀)。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    enabled = [a for a in accounts if a.enabled]
    if not enabled:
        return []

    connections = {acc.name: _account_to_server_config(acc) for acc in enabled}
    client = MultiServerMCPClient(connections)

    tools: list[BaseTool] = []
    for acc in enabled:
        try:
            acc_tools = await client.get_tools(server_name=acc.name)
            for t in acc_tools:
                t.name = f"{acc.name}__{t.name}"  # 防多账号同名冲突
            tools.extend(acc_tools)
        except Exception as e:  # noqa: BLE001  单个云账号失败不应拖垮整体
            print(f"[MCP] 加载云账号 '{acc.name}' 失败:{e}")
    return tools
