"""Agent 运行时:装配图 + 流式运行 + 审批续跑 + 消息持久化。

每次请求按 DB 当前配置重建图(模型/工具可能变),但 checkpointer 全局共享,
因此同一个 thread_id 的对话状态、以及被 interrupt 挂起的审批,能跨请求续上。

对外主要暴露异步生成器 astream_turn / astream_resume,产出如下事件(供 SSE):
  {"type":"thread","thread_id":...}
  {"type":"token","text":...}                  LLM 实时输出
  {"type":"tool_call","name","args","level"}   Agent 决定调用某命令
  {"type":"approval_required","operations":[]}  需用户逐条确认(流暂停)
  {"type":"tool_result","name","output",...}    命令执行结果
  {"type":"done","reply":...}                   本轮结束
  {"type":"error","error":...}
"""
from collections.abc import AsyncIterator

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agent.graph import build_agent
from app.agent.guardrails import classify_tool_call
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import CloudAccount, Conversation, Message, ModelProvider
from app.llm.registry import build_chat_model
from app.tools.mcp_manager import load_cloud_tools
from app.tools.ssh import make_scoped_ssh_tools

settings = get_settings()

# 全局 checkpointer(进程内持久化对话与中断)。生产可换 PostgresSaver。
_checkpointer = MemorySaver()


# ----------------------------------------------------------------------------
# 装配
# ----------------------------------------------------------------------------
def _get_default_model() -> ModelProvider:
    with SessionLocal() as db:
        cfg = (db.query(ModelProvider).filter(ModelProvider.is_default).first()
               or db.query(ModelProvider).first())
        if cfg is None:
            raise RuntimeError("尚未配置任何模型供应商,请先在后台添加。")
        db.expunge(cfg)
        return cfg


async def _assemble(servers: list[str] | None = None, clouds: list[str] | None = None):
    """按"当前操作对象"装配图。servers/clouds 为空表示不限制(可操作全部)。"""
    model = build_chat_model(_get_default_model())

    allowed = set(servers) if servers else None
    tools = make_scoped_ssh_tools(allowed)

    with SessionLocal() as db:
        q = db.query(CloudAccount).filter(CloudAccount.enabled)
        if clouds:
            q = q.filter(CloudAccount.name.in_(clouds))
        accounts = q.all()
        for a in accounts:
            db.expunge(a)
    tools += await load_cloud_tools(accounts)

    suffix = _scope_suffix(servers, clouds)
    return build_agent(model, tools, checkpointer=_checkpointer, system_suffix=suffix)


def _scope_suffix(servers: list[str] | None, clouds: list[str] | None) -> str:
    if not servers and not clouds:
        return ""
    parts = []
    if servers:
        parts.append(f"服务器 [{', '.join(servers)}]")
    if clouds:
        parts.append(f"云账号 [{', '.join(clouds)}]")
    return ("## 当前操作对象限定\n本次会话只允许操作:" + ";".join(parts)
            + "。不要操作未列出的对象。")


# ----------------------------------------------------------------------------
# 会话 / 消息持久化(用于历史记录)
# ----------------------------------------------------------------------------
def _ensure_conversation(thread_id: str, title: str | None = None) -> None:
    with SessionLocal() as db:
        c = db.query(Conversation).filter_by(thread_id=thread_id).first()
        if c is None:
            db.add(Conversation(thread_id=thread_id, title=(title or "新会话")[:60], status="active"))
            db.commit()


def _save_message(thread_id: str, role: str, content: str, tool_name: str | None = None) -> None:
    with SessionLocal() as db:
        c = db.query(Conversation).filter_by(thread_id=thread_id).first()
        db.add(Message(conversation_id=(c.id if c else None), role=role,
                       content=content or "", tool_name=tool_name))
        db.commit()


def _set_status(thread_id: str, status: str) -> None:
    with SessionLocal() as db:
        db.query(Conversation).filter_by(thread_id=thread_id).update({"status": status})
        db.commit()


# ----------------------------------------------------------------------------
# 流式运行
# ----------------------------------------------------------------------------
async def _run_stream(agent, graph_input, config, thread_id: str) -> AsyncIterator[dict]:
    assistant_buf: list[str] = []
    finished = False           # 是否正常跑完(用于区分"客户端中断")
    try:
        async for mode, payload in agent.astream(
            graph_input, config, stream_mode=["updates", "messages"]
        ):
            if mode == "messages":
                chunk, meta = payload
                text = chunk.content if isinstance(chunk.content, str) else ""
                if text and meta.get("langgraph_node") == "agent":
                    assistant_buf.append(text)
                    yield {"type": "token", "text": text}

            elif mode == "updates":
                if "__interrupt__" in payload:
                    if assistant_buf:
                        _save_message(thread_id, "assistant", "".join(assistant_buf))
                        assistant_buf = []
                    _set_status(thread_id, "waiting_approval")
                    finished = True  # 正常暂停,等待 /chat/approve(不算中断)
                    yield {"type": "approval_required", **payload["__interrupt__"][0].value}
                    return

                for node, upd in payload.items():
                    if node == "agent":
                        if (upd or {}).get("last_io"):
                            yield {"type": "llm_io", **upd["last_io"]}
                        msgs = (upd or {}).get("messages") or []
                        if msgs and getattr(msgs[-1], "tool_calls", None):
                            for tc in msgs[-1].tool_calls:
                                level, _ = classify_tool_call(tc["name"], tc["args"])
                                yield {"type": "tool_call", "name": tc["name"],
                                       "args": tc["args"], "level": level.value}
                    elif node == "execute_tools":
                        for m in (upd or {}).get("messages") or []:
                            _save_message(thread_id, "tool", m.content, getattr(m, "name", None))
                            yield {"type": "tool_result", "name": getattr(m, "name", None),
                                   "output": m.content, "tool_call_id": m.tool_call_id}

        final = "".join(assistant_buf)
        assistant_buf = []
        if final:
            _save_message(thread_id, "assistant", final)
        _set_status(thread_id, "done")
        finished = True
        yield {"type": "done", "reply": final}
    except Exception as e:  # noqa: BLE001
        finished = True
        yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        # 客户端中断(fetch abort → 服务端生成器被关闭)时,保存已生成的部分并标记中断
        if not finished and assistant_buf:
            _save_message(thread_id, "assistant", "".join(assistant_buf) + "\n…(已中断)")
        if not finished:
            _set_status(thread_id, "interrupted")


async def astream_turn(thread_id: str, user_message: str,
                       servers: list[str] | None = None,
                       clouds: list[str] | None = None) -> AsyncIterator[dict]:
    _ensure_conversation(thread_id, title=user_message)
    _save_message(thread_id, "user", user_message)
    agent = await _assemble(servers, clouds)
    config = {"configurable": {"thread_id": thread_id}}
    graph_input = {"messages": [HumanMessage(content=user_message)], "plan": [],
                   "current_step": 0, "dry_run": settings.default_dry_run, "notes": "",
                   "approved_ids": [], "last_io": {}}
    async for ev in _run_stream(agent, graph_input, config, thread_id):
        yield ev


async def astream_resume(thread_id: str, action: str, ids: list[str],
                         servers: list[str] | None = None,
                         clouds: list[str] | None = None) -> AsyncIterator[dict]:
    agent = await _assemble(servers, clouds)
    config = {"configurable": {"thread_id": thread_id}}
    cmd = Command(resume={"action": action, "ids": ids or []})
    async for ev in _run_stream(agent, cmd, config, thread_id):
        yield ev
