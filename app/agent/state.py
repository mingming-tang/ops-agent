"""LangGraph 状态定义。

采用 2026 的"结构化状态 + 显式计划"风格:除了对话消息,还显式持有
任务计划(plan)与当前步,便于 plan-and-execute 与反思重规划。
"""
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    plan: list[str]            # 任务分解的步骤
    current_step: int          # 当前执行到第几步
    dry_run: bool              # 是否只演练不真改
    notes: str                 # 反思/上下文摘要(context engineering)
    approved_ids: list[str]    # 本轮用户批准执行的 tool_call id
