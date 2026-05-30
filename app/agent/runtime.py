"""Agent 运行时:把 DB 配置装配成可运行的图,并处理"运行 / 续跑(审批后)"。

每次请求按 DB 当前配置重建图(模型/工具可能变),但 checkpointer 全局共享,
因此同一个 thread_id 的对话状态、以及被 interrupt 挂起的审批,能跨请求续上。
"""
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agent.graph import build_agent
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import CloudAccount, ModelProvider
from app.llm.registry import build_chat_model
from app.tools.mcp_manager import load_cloud_tools
from app.tools.ssh import list_servers_tool, ssh_run_tool

settings = get_settings()

# 全局 checkpointer(进程内持久化对话与中断)。生产可换 PostgresSaver。
_checkpointer = MemorySaver()


def _get_default_model() -> ModelProvider:
    with SessionLocal() as db:
        cfg = (db.query(ModelProvider).filter(ModelProvider.is_default).first()
               or db.query(ModelProvider).first())
        if cfg is None:
            raise RuntimeError("尚未配置任何模型供应商,请先在后台添加。")
        db.expunge(cfg)
        return cfg


async def _assemble():
    model = build_chat_model(_get_default_model())
    tools = [list_servers_tool, ssh_run_tool]
    with SessionLocal() as db:
        accounts = db.query(CloudAccount).filter(CloudAccount.enabled).all()
        for a in accounts:
            db.expunge(a)
    tools += await load_cloud_tools(accounts)
    return build_agent(model, tools, checkpointer=_checkpointer)


def _summarize(result: dict) -> dict:
    """把图的返回整理成 API 友好结构,识别是否在等审批。"""
    interrupts = result.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value
        return {"status": "waiting_approval", "approval": payload}
    final = result["messages"][-1]
    return {"status": "done", "reply": final.content}


async def run_turn(thread_id: str, user_message: str) -> dict:
    agent = await _assemble()
    config = {"configurable": {"thread_id": thread_id}}
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=user_message)],
         "plan": [], "current_step": 0, "dry_run": settings.default_dry_run, "notes": ""},
        config,
    )
    return _summarize(result)


async def resume_turn(thread_id: str, approved: bool, by: str = "admin") -> dict:
    """审批后续跑被 interrupt 挂起的图。"""
    agent = await _assemble()
    config = {"configurable": {"thread_id": thread_id}}
    result = await agent.ainvoke(
        Command(resume={"approved": approved, "by": by}), config
    )
    return _summarize(result)
