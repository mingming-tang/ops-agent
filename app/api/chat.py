"""Agent 交互 API:流式发起任务、逐条审批续跑、历史记录查看。"""
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.runtime import astream_resume, astream_turn
from app.db.base import get_db
from app.db.models import Conversation, Message

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatIn(BaseModel):
    message: str
    thread_id: str | None = None   # 不传则新建会话
    servers: list[str] = []        # 限定操作的服务器名;空=不限制
    clouds: list[str] = []         # 限定操作的云账号名;空=不限制


class ApproveIn(BaseModel):
    thread_id: str
    action: str = "all"            # all | selected | reject
    ids: list[str] = []
    servers: list[str] = []
    clouds: list[str] = []


def _sse(gen: AsyncIterator[dict]) -> StreamingResponse:
    async def event_stream():
        async for ev in gen:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/stream")
async def chat_stream(body: ChatIn):
    thread_id = body.thread_id or uuid.uuid4().hex

    async def gen():
        yield {"type": "thread", "thread_id": thread_id}
        async for ev in astream_turn(thread_id, body.message, body.servers, body.clouds):
            yield ev

    return _sse(gen())


@router.post("/approve")
async def approve(body: ApproveIn):
    return _sse(astream_resume(body.thread_id, body.action, body.ids, body.servers, body.clouds))


# ---------------- 历史记录 ----------------
@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db)):
    rows = db.query(Conversation).order_by(Conversation.id.desc()).all()
    return [{"thread_id": c.thread_id, "title": c.title, "status": c.status,
             "created_at": c.created_at.isoformat()} for c in rows]


@router.get("/conversations/{thread_id}")
def get_conversation(thread_id: str, db: Session = Depends(get_db)):
    c = db.query(Conversation).filter_by(thread_id=thread_id).first()
    if c is None:
        raise HTTPException(404, "会话不存在")
    msgs = db.query(Message).filter_by(conversation_id=c.id).order_by(Message.id).all()
    return {"thread_id": thread_id, "title": c.title, "status": c.status,
            "messages": [{"role": m.role, "content": m.content, "tool_name": m.tool_name,
                          "created_at": m.created_at.isoformat()} for m in msgs]}


@router.post("/conversations/{thread_id}/end")
def end_conversation(thread_id: str, db: Session = Depends(get_db)):
    """结束任务:把会话标记为已结束(不再续聊)。"""
    c = db.query(Conversation).filter_by(thread_id=thread_id).first()
    if c is not None:
        c.status = "ended"
        db.commit()
    return {"ok": True}


@router.delete("/conversations/{thread_id}")
def delete_conversation(thread_id: str, db: Session = Depends(get_db)):
    c = db.query(Conversation).filter_by(thread_id=thread_id).first()
    if c is not None:
        db.query(Message).filter_by(conversation_id=c.id).delete()
        db.delete(c)
        db.commit()
    return {"ok": True}
