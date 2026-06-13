"""长期记忆管理 API:列出 / 手动新增 / 删除记忆条目。

新增走 add_memory 以便顺带生成语义向量;列表不回传 embedding(体积大且无展示意义)。
"""
from fastapi import APIRouter
from fastapi import Depends as _D
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.memory import add_memory, recent_memories
from app.db.base import get_db
from app.db.models import Memory

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryIn(BaseModel):
    content: str
    kind: str = "fact"


@router.get("")
def list_memory():
    return recent_memories(500)


@router.post("")
def create_memory(body: MemoryIn):
    mid = add_memory(body.content, body.kind)
    if mid is None:
        return {"ok": False, "error": "内容为空"}
    return {"id": mid}


@router.delete("/{mid}")
def delete_memory(mid: int, db: Session = _D(get_db)):
    obj = db.get(Memory, mid)
    if obj is not None:
        db.delete(obj)
        db.commit()
    return {"ok": True}
