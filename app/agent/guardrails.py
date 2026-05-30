"""命令分级护栏。

把每个待执行的操作分成三级,危险级强制人工审批:
  - readonly  只读巡检(ls/cat/df/ps/...)            → 自动执行
  - mutating  会产生变更(systemctl restart/apt...)   → 执行并记录
  - dangerous 高危/不可逆(rm -rf/mkfs/drop/关防火墙) → interrupt 等审批

判定基于命令文本的规则匹配 + 云工具的写操作动词。规则可按需扩展,
也可以替换为"用一个小模型给命令打风险分"的 LLM 护栏。
"""
import re

from app.db.models import CommandLevel

# 危险:不可逆、可能造成数据/可用性损失
_DANGEROUS = [
    r"\brm\s+-[a-z]*[rf]",          # rm -rf
    r"\bmkfs\b", r"\bdd\b",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\b(drop|truncate)\s+(table|database)\b",
    r":\s*\(\)\s*\{",               # fork 炸弹
    r"\b>\s*/dev/sd",               # 直接写磁盘
    r"\bchmod\s+-R\s+777\s+/",
    r"\biptables\s+-F\b", r"\bufw\s+disable\b",
    r"\buserdel\b", r"\bkill\s+-9\s+1\b",
    r"\bgit\s+push\s+.*--force",
]

# 变更:有副作用但通常可控/可回滚
_MUTATING = [
    r"\b(systemctl|service)\s+(restart|stop|start|reload)\b",
    r"\b(apt|apt-get|yum|dnf|brew)\s+(install|remove|upgrade|update)\b",
    r"\b(docker|kubectl)\s+(run|rm|delete|apply|restart|scale)\b",
    r"\b(cp|mv|chmod|chown|ln|mkdir|touch|tee)\b",
    r"\b(pip|npm|yarn)\s+install\b",
    r"\b>\s*/", r"\bsed\s+-i\b",
    r"\bcrontab\b", r"\biptables\b",
]

# 云工具:动词以这些开头视为变更/危险(只读用 Describe/List/Get)
_CLOUD_WRITE_VERBS = ("create", "run", "delete", "release", "modify", "update",
                      "reboot", "stop", "start", "put", "set", "purge", "drain")
_CLOUD_DANGEROUS_VERBS = ("delete", "release", "purge", "destroy", "drain")


def classify_command(command: str) -> CommandLevel:
    cmd = command.lower()
    for pat in _DANGEROUS:
        if re.search(pat, cmd):
            return CommandLevel.dangerous
    for pat in _MUTATING:
        if re.search(pat, cmd):
            return CommandLevel.mutating
    return CommandLevel.readonly


def classify_cloud_tool(tool_name: str) -> CommandLevel:
    # 工具名形如 aliyun-prod__DeleteInstance → 取最后一段的动词
    action = tool_name.split("__")[-1].lower()
    if any(action.startswith(v) for v in _CLOUD_DANGEROUS_VERBS):
        return CommandLevel.dangerous
    if any(action.startswith(v) for v in _CLOUD_WRITE_VERBS):
        return CommandLevel.mutating
    return CommandLevel.readonly


def classify_tool_call(tool_name: str, args: dict) -> tuple[CommandLevel, str]:
    """返回 (风险级别, 人类可读的操作摘要)。"""
    if tool_name == "ssh_run":
        cmd = args.get("command", "")
        summary = f"在服务器 [{args.get('server_name')}] 执行:{cmd}"
        return classify_command(cmd), summary
    if "__" in tool_name:  # 云 MCP 工具
        summary = f"云操作 {tool_name},参数:{args}"
        return classify_cloud_tool(tool_name), summary
    # 其它工具(list_servers 等)默认只读
    return CommandLevel.readonly, f"{tool_name}({args})"
