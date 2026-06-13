"""大模型 token 用量采集与统计。

采集:给每个 ChatModel 挂一个回调(见 registry.build_chat_model),每次调用结束后
读取用量并落库。一个回调实例对应一个模型配置,模型名/供应商名在构造时就绑定,
因此主推理、命令分级、摘要、记忆抽取等所有走该模型的调用都会被计入。

统计:query_stats(period) 在 Python 端按小时/天/月分桶,输入/输出分开汇总,
避免 SQLite / Postgres 的日期函数差异。
"""
import datetime as dt

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.db.base import SessionLocal
from app.db.models import TokenUsage

# period -> (回看时间窗, strftime 分桶格式)
_PERIODS: dict[str, tuple[dt.timedelta, str]] = {
    "hour": (dt.timedelta(hours=24), "%Y-%m-%d %H:00"),
    "day": (dt.timedelta(days=30), "%Y-%m-%d"),
    "month": (dt.timedelta(days=366), "%Y-%m"),
}


def record_usage(model_name: str, provider: str | None,
                 input_tokens: int, output_tokens: int) -> None:
    """落一条 token 用量;0/0 不记。best-effort,异常静默。"""
    if not input_tokens and not output_tokens:
        return
    try:
        with SessionLocal() as db:
            db.add(TokenUsage(model_name=model_name or "unknown", provider=provider,
                              input_tokens=int(input_tokens or 0),
                              output_tokens=int(output_tokens or 0)))
            db.commit()
    except Exception:  # noqa: BLE001
        pass


def _extract_usage(result: LLMResult) -> tuple[int, int]:
    """从 LLMResult 里抽 (input, output) token 数,兼容多家返回结构。"""
    # 1) 优先用消息上的 usage_metadata(各家统一字段,流式也会聚合)
    try:
        for gen_list in result.generations:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                um = getattr(msg, "usage_metadata", None) if msg is not None else None
                if um:
                    return int(um.get("input_tokens") or 0), int(um.get("output_tokens") or 0)
    except Exception:  # noqa: BLE001
        pass
    # 2) 回退到 llm_output.token_usage(OpenAI 兼容非流式)
    tu = (result.llm_output or {}).get("token_usage") or {} if result.llm_output else {}
    return (int(tu.get("prompt_tokens") or 0), int(tu.get("completion_tokens") or 0))


class TokenUsageCallback(BaseCallbackHandler):
    """挂在模型上的回调:每次调用结束记一条用量。"""

    def __init__(self, model_name: str, provider: str | None = None):
        self.model_name = model_name
        self.provider = provider

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:  # noqa: ARG002
        inp, out = _extract_usage(response)
        record_usage(self.model_name, self.provider, inp, out)


def query_stats(period: str = "day") -> dict:
    """按时段统计 token 消耗。返回 {period, buckets:[{bucket,input,output}], by_model:[...]}。

    buckets 按时间倒序;by_model 为该时间窗内各模型的输入/输出合计。
    """
    window, fmt = _PERIODS.get(period, _PERIODS["day"])
    since = dt.datetime.now(dt.UTC) - window
    buckets: dict[str, list[int]] = {}     # label -> [input, output]
    by_model: dict[str, list[int]] = {}    # model_name -> [input, output]
    with SessionLocal() as db:
        rows = db.query(TokenUsage).filter(TokenUsage.created_at >= since).all()
    for r in rows:
        label = r.created_at.strftime(fmt)
        b = buckets.setdefault(label, [0, 0])
        b[0] += r.input_tokens or 0
        b[1] += r.output_tokens or 0
        m = by_model.setdefault(r.model_name, [0, 0])
        m[0] += r.input_tokens or 0
        m[1] += r.output_tokens or 0
    bucket_rows = [{"bucket": k, "input": v[0], "output": v[1], "total": v[0] + v[1]}
                   for k, v in sorted(buckets.items(), reverse=True)]
    model_rows = [{"model_name": k, "input": v[0], "output": v[1], "total": v[0] + v[1]}
                  for k, v in sorted(by_model.items(), key=lambda x: -(x[1][0] + x[1][1]))]
    return {"period": period, "buckets": bucket_rows, "by_model": model_rows}
