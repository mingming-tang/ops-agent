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
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
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
from app.agent.prompts import SYSTEM_PROMPT
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

    # -- 节点:推理 --
    async def agent_node(state: AgentState) -> dict:
        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=system_text), *messages]
        response = await model_with_tools.ainvoke(messages)
        usage = getattr(response, "usage_metadata", None) or {}
        return {"messages": [response],
                "last_io": {"prompt": _render_prompt(messages),
                            "response": _render_response(response),
                            "usage": {"input": usage.get("input_tokens"),
                                      "output": usage.get("output_tokens")}}}

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
