"""Agent 交互 API:发起任务、审批续跑。"""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.agent.runtime import resume_turn, run_turn
from app.api.deps import require_admin

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(require_admin)])


class ChatIn(BaseModel):
    message: str
    thread_id: str | None = None   # 不传则新建会话


class ApproveIn(BaseModel):
    thread_id: str
    approved: bool
    by: str = "admin"


@router.post("")
async def chat(body: ChatIn):
    thread_id = body.thread_id or uuid.uuid4().hex
    result = await run_turn(thread_id, body.message)
    return {"thread_id": thread_id, **result}


@router.post("/approve")
async def approve(body: ApproveIn):
    result = await resume_turn(body.thread_id, body.approved, body.by)
    return {"thread_id": body.thread_id, **result}
