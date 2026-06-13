"""LangGraph 主图。

   START → agent ──(有工具调用?)──> guardrail ──(安全/已批准)──> execute_tools ──┐
             ▲                          │                                          │
             │                    (危险→interrupt 等审批)                          │
             └──────────────────────────┴──────────(被拒绝/无工具调用)────────────┘→ END

设计模式落地:
  - Human-in-the-loop:guardrail 节点对危险操作 `interrupt()`,挂起等审批。
  - Durable execution:compile 时传入 checkpointer,可中断续跑。
  - Guardrails 分级:每个工具调用执行前分级。
  - 审计:execute_tools 对每次调用写 AuditLog。
"""
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.guardrails import (
    classify_command_llm,
    classify_tool_call,
    is_auto_approved,
    needs_approval,
    remember_auto_approve,
)
from app.agent.memory import (
    extract_and_store,
    format_memories,
    search_memories,
    _render_for_extract,
)
from app.agent.prompts import SUMMARY_PROMPT, SYSTEM_PROMPT
from app.agent.state import AgentState
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import AuditLog, CommandLevel
from app.tools.clarify import CLARIFY_TOOL_NAME

settings = get_settings()


def build_agent(model: BaseChatModel, tools: list[BaseTool], checkpointer=None,
                system_suffix: str = ""):
    tools_by_name = {t.name: t for t in tools}
    model_with_tools = model.bind_tools(tools)
    system_text = SYSTEM_PROMPT + (("\n\n" + system_suffix) if system_suffix else "")

    # -- 节点:推理(含消息压缩 + 长期记忆注入)--
    async def agent_node(state: AgentState) -> dict:
        # state["messages"] 不含 SystemMessage(每轮在此临时拼接,不持久化)
        messages = [m for m in state["messages"] if not isinstance(m, SystemMessage)]
        notes = state.get("notes") or ""
        remove_msgs: list = []

        # 1) 压缩:估算 token 超阈值时,把较早消息摘要进 notes,并从本次调用中剔除
        if settings.compress_enabled and _estimate_tokens(messages) > settings.compress_token_threshold:
            new_notes, remove_msgs, dropped = await _compact(messages, notes, model)
            if remove_msgs:
                notes = new_notes
                if settings.memory_auto_extract:
                    await extract_and_store(dropped, model)   # 摘要丢弃前抢救事实入库
                drop_ids = {id(m) for m in dropped}
                messages = [m for m in messages if id(m) not in drop_ids]

        # 2) 召回:按最近一条用户消息检索相关长期记忆
        memory_block = ""
        if settings.memory_enabled:
            query = _last_human_text(messages)
            if query:
                memory_block = format_memories(search_memories(query, settings.memory_top_k))

        # 3) 组装本次调用消息:system + (摘要/记忆 context) + 压缩后的对话(均不写回 state)
        call_messages: list = [SystemMessage(content=system_text)]
        ctx = _build_context(notes, memory_block)
        if ctx:
            call_messages.append(SystemMessage(content=ctx))
        call_messages += messages

        response = await model_with_tools.ainvoke(call_messages)
        usage = getattr(response, "usage_metadata", None) or {}
        update = {"messages": [*remove_msgs, response],
                  "last_io": {"prompt": _render_prompt(call_messages),
                              "response": _render_response(response),
                              "usage": {"input": usage.get("input_tokens"),
                                        "output": usage.get("output_tokens")}}}
        if remove_msgs:
            update["notes"] = notes
        return update

    # -- 节点:护栏 + 逐条审批 --
    async def guardrail_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        # 本会话勾选了"所有命令无需确认":全部直接放行,连分级都跳过
        if state.get("auto_approve_all"):
            return {"approved_ids": [tc["id"] for tc in last.tool_calls]}
        pending = []          # 需用户确认的命令(变更 / 危险)
        auto_ids = []         # 无需确认直接执行的(只读命令、白名单、list_servers 等本地工具)
        for tc in last.tool_calls:
            if not needs_approval(tc["name"]):
                auto_ids.append(tc["id"])          # 本地只读工具
                continue
            if tc["name"] == "ssh_run":
                cmd = tc["args"].get("command", "")
                if is_auto_approved(cmd):          # 用户此前选过"下次不再确认"
                    auto_ids.append(tc["id"])
                    continue
                # 是否只读交给大模型判断;只读直接执行,否则需人工确认
                level = await classify_command_llm(cmd, model)
                summary = f"在服务器 [{tc['args'].get('server_name')}] 执行:{cmd}"
            else:
                level, summary = classify_tool_call(tc["name"], tc["args"])
            if level == CommandLevel.readonly:
                auto_ids.append(tc["id"])          # 只读命令:直接执行,无需确认
            else:
                pending.append({"id": tc["id"], "name": tc["name"], "level": level.value,
                                "summary": summary, "command": tc["args"].get("command"),
                                "intent": tc["args"].get("intent", ""), "args": tc["args"]})

        if not pending or not settings.require_command_approval:
            return {"approved_ids": [tc["id"] for tc in last.tool_calls]}

        # 挂起等待用户确认。resume 传回 {"action": "all|selected|reject", "ids": [...], "remember": bool}
        decision = interrupt({"type": "approval_required", "operations": pending,
                              "message": "以下命令需要确认后才会执行"}) or {}
        action = decision.get("action", "reject")
        if action == "all":
            approved = [op["id"] for op in pending]
        elif action == "selected":
            approved = [i for i in decision.get("ids", []) if i in {op["id"] for op in pending}]
        else:  # reject
            approved = []
        # "下次不再确认":把本次批准且为 ssh 命令的指令写入白名单,后续同命令自动放行
        if decision.get("remember"):
            approved_set = set(approved)
            for op in pending:
                if op["id"] in approved_set and op["name"] == "ssh_run" and op.get("command"):
                    remember_auto_approve(op["command"])
        return {"approved_ids": auto_ids + approved}

    # -- 节点:执行工具 + 审计(只执行已批准的,其余跳过)--
    async def execute_tools_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        approved = set(state.get("approved_ids") or [])
        results = []
        for tc in last.tool_calls:
            level, summary = classify_tool_call(tc["name"], tc["args"])
            if tc["id"] not in approved:
                results.append(ToolMessage(content="[已跳过] 用户本轮未批准执行该命令。",
                                           tool_call_id=tc["id"], name=tc["name"]))
                continue
            tool = tools_by_name.get(tc["name"])
            if tool is None:
                output, success = f"[错误] 未知工具 {tc['name']}", False
            else:
                try:
                    output = str(await tool.ainvoke(tc["args"]))
                    success = not output.startswith("[错误]")
                except Exception as e:  # noqa: BLE001
                    output, success = f"[错误] 工具执行异常:{e}", False
            results.append(ToolMessage(content=output, tool_call_id=tc["id"], name=tc["name"]))
            _audit(tc, level, summary, success, output)
        return {"messages": results, "approved_ids": []}

    # -- 节点:澄清(任务不明确时 interrupt 让用户选)--
    async def clarify_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        results = []
        for tc in last.tool_calls:
            if tc["name"] != CLARIFY_TOOL_NAME:
                # 与 clarify 同批的其它工具:本轮先不执行,提示模型澄清后再调用
                results.append(ToolMessage(content="[已跳过] 请先完成澄清(clarify)后再调用其它工具。",
                                           tool_call_id=tc["id"], name=tc["name"]))
                continue
            decision = interrupt({
                "type": "clarify_required", "tool_call_id": tc["id"],
                "question": tc["args"].get("question", ""),
                "options": tc["args"].get("options", []) or [],
            }) or {}
            answer = (decision.get("answer") or "").strip()
            results.append(ToolMessage(
                content=(f"用户的选择/补充:{answer}" if answer else "用户未作答,请基于最安全的默认方案继续或再次澄清。"),
                tool_call_id=tc["id"], name=CLARIFY_TOOL_NAME))
        return {"messages": results}

    # -- 路由 --
    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if not getattr(last, "tool_calls", None):
            return END
        if any(tc["name"] == CLARIFY_TOOL_NAME for tc in last.tool_calls):
            return "clarify"
        return "guardrail"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("execute_tools", execute_tools_node)
    graph.add_node("clarify", clarify_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, ["guardrail", "clarify", END])
    graph.add_edge("guardrail", "execute_tools")
    graph.add_edge("execute_tools", "agent")
    graph.add_edge("clarify", "agent")

    return graph.compile(checkpointer=checkpointer or MemorySaver())


# ----------------------------------------------------------------------------
# 消息压缩 / 记忆注入辅助
# ----------------------------------------------------------------------------
def _estimate_tokens(messages: list) -> int:
    """廉价 token 估算:总字符数 / 3(中英混合够用,无需 tiktoken)。"""
    total = 0
    for m in messages:
        c = m.content if isinstance(m.content, str) else str(m.content)
        total += len(c)
        for tc in (getattr(m, "tool_calls", None) or []):
            total += len(str(tc.get("args", "")))
    return total // 3


def _last_human_text(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def _build_context(notes: str, memory_block: str) -> str:
    parts = []
    if notes:
        parts.append("## 对话历史摘要(较早内容已压缩,以下为要点)\n" + notes)
    if memory_block:
        parts.append("## 相关长期记忆(跨会话)\n" + memory_block)
    return "\n\n".join(parts)


async def _compact(messages: list, existing_notes: str, model: BaseChatModel):
    """把较早的消息摘要进 notes。返回 (新摘要, [RemoveMessage...], [被删消息...])。

    切点保证工具调用配对安全:保留窗口不以 ToolMessage 开头,
    被删段内部的 AIMessage(tool_calls) 与其 ToolMessage 成对一起删除。
    """
    keep = settings.compress_keep_recent
    if len(messages) <= keep:
        return existing_notes, [], []
    cut = len(messages) - keep
    # 向后挪到干净边界:保留窗口首条不能是 ToolMessage(否则其 tool_call 成孤儿)
    while cut < len(messages) and isinstance(messages[cut], ToolMessage):
        cut += 1
    if cut <= 0 or cut >= len(messages):
        return existing_notes, [], []      # 没有可安全压缩的边界

    dropped = messages[:cut]
    transcript = _render_for_extract(dropped)
    prompt = SUMMARY_PROMPT.format(existing=existing_notes or "(无)", transcript=transcript[:12000])
    try:
        resp = await model.ainvoke([SystemMessage(content=prompt)])
        summary = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception:  # noqa: BLE001  摘要失败:保持原状,不丢消息
        return existing_notes, [], []
    if not summary.strip():
        return existing_notes, [], []
    remove_msgs = [RemoveMessage(id=m.id) for m in dropped if getattr(m, "id", None)]
    if not remove_msgs:                    # 消息无 id 无法删除,放弃压缩
        return existing_notes, [], []
    return summary, remove_msgs, dropped


def _render_prompt(messages: list) -> list[dict]:
    """把发给模型的消息序列化成可读结构(供前端调试查看)。"""
    out = []
    for m in messages:
        role = getattr(m, "type", m.__class__.__name__)
        content = m.content if isinstance(m.content, str) else str(m.content)
        item = {"role": role, "content": content[:6000]}
        if getattr(m, "tool_calls", None):
            item["tool_calls"] = [{"name": tc["name"], "args": tc["args"]} for tc in m.tool_calls]
        out.append(item)
    return out


def _render_response(response) -> dict:
    return {"content": response.content if isinstance(response.content, str) else str(response.content),
            "tool_calls": [{"name": tc["name"], "args": tc["args"]}
                           for tc in (getattr(response, "tool_calls", None) or [])]}


def _audit(tool_call: dict, level: CommandLevel, summary: str, success: bool, output: str) -> None:
    args = tool_call.get("args", {})
    target = args.get("server_name") or tool_call["name"].split("__")[0]
    command = args.get("command") or summary
    with SessionLocal() as db:
        db.add(AuditLog(
            tool_name=tool_call["name"], target=target, command=command, level=level,
            approved=None if level != CommandLevel.dangerous else True,
            success=success, output=output[:5000],
        ))
        db.commit()
