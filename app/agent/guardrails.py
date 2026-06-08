"""命令分级护栏。

把每个待执行的操作分成三级,危险级强制人工审批:
  - readonly  只读巡检(ls/cat/df/ps/...)            → 自动执行
  - mutating  会产生变更(systemctl restart/apt...)   → 执行并记录
  - dangerous 高危/不可逆(rm -rf/mkfs/drop/关防火墙) → interrupt 等审批

判定基于命令文本的规则匹配 + 云工具的写操作动词。规则可按需扩展,
也可以替换为"用一个小模型给命令打风险分"的 LLM 护栏。
"""
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.db.base import SessionLocal
from app.db.models import AutoApproveRule, CommandLevel

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


_LLM_CLASSIFY_SYSTEM = SystemMessage(content=(
    "你是 Linux 运维命令风险分级器。判断给定 shell 命令的风险级别,只回复一个英文单词:\n"
    "- readonly:纯查询/只读,不修改任何状态(如 ls、cat、df、ps、grep、top、systemctl status、tail)\n"
    "- mutating:会产生副作用但通常可控/可回滚(如 systemctl restart、apt install、写文件、sed -i)\n"
    "- dangerous:高危/不可逆/可能造成数据或可用性损失(如 rm -rf、mkfs、dd、drop table、关闭防火墙、重启关机)\n"
    "注意:含管道、重定向、&&、子命令时,按其中风险最高的部分判定。\n"
    "只输出 readonly、mutating、dangerous 三者之一,不要任何解释或标点。"
))


async def classify_command_llm(command: str, model) -> CommandLevel:
    """问大模型判断命令风险级别;调用失败时回退到规则判定 classify_command。"""
    if not command.strip():
        return CommandLevel.readonly
    try:
        resp = await model.ainvoke([_LLM_CLASSIFY_SYSTEM, HumanMessage(content=command)])
        text = (resp.content if isinstance(resp.content, str) else str(resp.content)).lower()
        # 优先匹配高风险词,避免"不是 dangerous,是 readonly"这类措辞误判
        for level in (CommandLevel.dangerous, CommandLevel.mutating, CommandLevel.readonly):
            if level.value in text:
                return level
    except Exception:  # noqa: BLE001  模型不可用/超时等,降级到规则
        pass
    return classify_command(command)


# ---- "下次不再确认"免审批白名单(精确命令文本,持久化到 DB) ----
def is_auto_approved(command: str) -> bool:
    cmd = (command or "").strip()
    if not cmd:
        return False
    with SessionLocal() as db:
        return db.query(AutoApproveRule).filter(AutoApproveRule.command == cmd).first() is not None


def remember_auto_approve(command: str) -> None:
    cmd = (command or "").strip()
    if not cmd:
        return
    with SessionLocal() as db:
        if db.query(AutoApproveRule).filter(AutoApproveRule.command == cmd).first() is None:
            db.add(AutoApproveRule(command=cmd))
            db.commit()


def classify_cloud_tool(tool_name: str) -> CommandLevel:
    # 工具名形如 aliyun-prod__DeleteInstance → 取最后一段的动词
    action = tool_name.split("__")[-1].lower()
    if any(action.startswith(v) for v in _CLOUD_DANGEROUS_VERBS):
        return CommandLevel.dangerous
    if any(action.startswith(v) for v in _CLOUD_WRITE_VERBS):
        return CommandLevel.mutating
    return CommandLevel.readonly


def needs_approval(tool_name: str) -> bool:
    """是否属于"会在服务器/云上真正执行"的命令,需要用户确认。

    ssh_run 与云 MCP 工具(名含 '__')都算;list_servers 等本地只读元数据工具不算。
    """
    return tool_name == "ssh_run" or "__" in tool_name


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
