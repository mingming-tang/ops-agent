"""长期记忆工具:让 Agent 主动存取跨会话的运维知识。

- save_memory(content, kind):把一条稳定、可复用的事实写入长期记忆。
- recall_memory(query):按语义/关键词召回最相关的若干条记忆。

真正的存取/向量逻辑在 app/agent/memory.py,这里只把它包装成 LangChain 工具。
"""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agent.memory import add_memory, format_memories, search_memories
from app.config import get_settings

settings = get_settings()


class SaveMemoryInput(BaseModel):
    content: str = Field(description="要长期记住的一句话事实(稳定、可复用的结论)")
    kind: str = Field(default="fact",
                      description="类别:fact(事实)/preference(偏好)/runbook(操作约定)/env(环境)")


class RecallMemoryInput(BaseModel):
    query: str = Field(description="要查询的主题/关键词(如某服务器名、某项配置)")


async def _save_memory(content: str, kind: str = "fact") -> str:
    mid = add_memory(content, kind=kind)
    if mid is None:
        return "[错误] 记忆内容为空,未保存。"
    return f"已记住:{content}"


async def _recall_memory(query: str) -> str:
    items = search_memories(query, k=settings.memory_top_k)
    if not items:
        return "未找到相关的长期记忆。"
    return "相关长期记忆:\n" + format_memories(items)


save_memory_tool = StructuredTool.from_function(
    coroutine=_save_memory, name="save_memory",
    description=("把一条稳定、可复用的运维事实写入长期记忆(跨会话保留),"
                "如服务器路径/拓扑、用户偏好、运维约定、既定结论。不要存一次性临时输出或会过期的实时状态。"),
    args_schema=SaveMemoryInput,
)

recall_memory_tool = StructuredTool.from_function(
    coroutine=_recall_memory, name="recall_memory",
    description="按主题/关键词从长期记忆中召回此前记住的相关事实。遇到可能此前记过的信息时先调用它。",
    args_schema=RecallMemoryInput,
)


def memory_tools() -> list[StructuredTool]:
    return [save_memory_tool, recall_memory_tool]
