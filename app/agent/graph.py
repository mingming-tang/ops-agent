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

from app.agent.guardrails import classify_tool_call
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.state import AgentState
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import AuditLog, CommandLevel

settings = get_settings()


def build_agent(model: BaseChatModel, tools: list[BaseTool], checkpointer=None):
    tools_by_name = {t.name: t for t in tools}
    model_with_tools = model.bind_tools(tools)

    # -- 节点:推理 --
    async def agent_node(state: AgentState) -> dict:
        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]
        response = await model_with_tools.ainvoke(messages)
        return {"messages": [response]}

    # -- 节点:护栏 + 人工审批 --
    def guardrail_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        dangerous = []
        for tc in last.tool_calls:
            level, summary = classify_tool_call(tc["name"], tc["args"])
            if level == CommandLevel.dangerous and settings.require_approval_for_dangerous:
                dangerous.append(summary)

        if not dangerous:
            return {}  # 全部放行,路由到 execute_tools

        # 挂起等待人工审批。前端通过 resume 传回 {"approved": bool, "by": "用户名"}
        decision = interrupt({
            "type": "approval_required",
            "operations": dangerous,
            "message": "以下高危操作需要人工审批,批准请回传 approved=true。",
        })
        if decision and decision.get("approved"):
            return {"notes": f"高危操作已被 {decision.get('by', '人工')} 批准"}

        # 被拒绝:为所有待执行 tool_call 生成拒绝回执,保证消息配对合法,再回到 agent
        rejections = [
            ToolMessage(content="[已拒绝] 用户未批准该操作,请改用更安全的方案。",
                        tool_call_id=tc["id"])
            for tc in last.tool_calls
        ]
        return {"messages": rejections, "notes": "上一步高危操作被拒绝"}

    # -- 节点:执行工具 + 审计 --
    async def execute_tools_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        results = []
        for tc in last.tool_calls:
            tool = tools_by_name.get(tc["name"])
            level, summary = classify_tool_call(tc["name"], tc["args"])
            if tool is None:
                output = f"[错误] 未知工具 {tc['name']}"
                success = False
            else:
                try:
                    output = str(await tool.ainvoke(tc["args"]))
                    success = not output.startswith("[错误]")
                except Exception as e:  # noqa: BLE001
                    output, success = f"[错误] 工具执行异常:{e}", False
            results.append(ToolMessage(content=output, tool_call_id=tc["id"]))
            _audit(tc, level, summary, success, output)
        return {"messages": results}

    # -- 路由 --
    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        return "guardrail" if getattr(last, "tool_calls", None) else END

    def route_after_guardrail(state: AgentState) -> str:
        # 若 guardrail 注入了 ToolMessage(被拒绝),回到 agent;否则执行工具
        last = state["messages"][-1]
        return "agent" if isinstance(last, ToolMessage) else "execute_tools"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("execute_tools", execute_tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, ["guardrail", END])
    graph.add_conditional_edges("guardrail", route_after_guardrail, ["agent", "execute_tools"])
    graph.add_edge("execute_tools", "agent")

    return graph.compile(checkpointer=checkpointer or MemorySaver())


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
