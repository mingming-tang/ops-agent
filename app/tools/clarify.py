"""clarify 工具:任务不明确/有歧义/有多种方案时,让用户来选。

模型调用 clarify(question, options) 后,图会在 clarify 节点 interrupt 暂停,
把问题与选项抛给前端;用户选定后把答案作为工具结果回灌给模型,继续推进。
真正的"暂停等待"逻辑在 graph 的 clarify_node 里,这里的函数仅作占位/兜底。
"""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

CLARIFY_TOOL_NAME = "clarify"


class ClarifyInput(BaseModel):
    question: str = Field(description="要向用户澄清的问题(一句话说清歧义点)")
    options: list[str] = Field(default_factory=list,
                               description="可选项列表;用户也可自行补充。无明确选项时可留空")


async def _clarify(question: str, options: list[str] | None = None) -> str:
    # 正常情况下不会走到这里(clarify_node 会拦截并 interrupt);兜底返回。
    return "（澄清未被处理)"


clarify_tool = StructuredTool.from_function(
    coroutine=_clarify,
    name=CLARIFY_TOOL_NAME,
    description=(
        "当任务不明确、存在歧义、或有多种可行方案、需要用户在风险/范围上拍板时,"
        "调用本工具向用户提问并给出候选项,等用户选择后再继续。不要在信息已足够时滥用。"
    ),
    args_schema=ClarifyInput,
)
