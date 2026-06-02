"""后台配置 API:模型供应商、服务器、云账号(MCP)、审计日志。

所有密钥/密码在写入时加密(crypto.encrypt),读取列表时一律不回传明文。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.db.base import get_db
from app.db.crypto import encrypt
from app.db.models import (AuditLog, CloudAccount, CloudType, ModelProvider, ProviderType,
                           Server)
from fastapi import Depends as _D

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _commit(db: Session) -> None:
    """提交;把唯一约束冲突(重名)转成友好的 409,而不是 500。"""
    from sqlalchemy.exc import IntegrityError

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "名称已存在,请换一个唯一名称") from None


# ---------- 模型供应商 ----------
class ModelIn(BaseModel):
    name: str
    provider_type: ProviderType
    model_name: str
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.0
    is_default: bool = False
    extra: dict = Field(default_factory=dict)


@router.post("/models")
def create_model(body: ModelIn, db: Session = _D(get_db)):
    if body.is_default:
        db.query(ModelProvider).update({ModelProvider.is_default: False})
    m = ModelProvider(
        name=body.name, provider_type=body.provider_type, model_name=body.model_name,
        api_key_enc=encrypt(body.api_key), base_url=body.base_url,
        temperature=body.temperature, is_default=body.is_default, extra=body.extra,
    )
    db.add(m); _commit(db)
    return {"id": m.id, "name": m.name}


@router.get("/models")
def list_models(db: Session = _D(get_db)):
    return [{"id": m.id, "name": m.name, "provider_type": m.provider_type,
             "model_name": m.model_name, "base_url": m.base_url,
             "is_default": m.is_default} for m in db.query(ModelProvider).all()]


@router.delete("/models/{model_id}")
def delete_model(model_id: int, db: Session = _D(get_db)):
    db.query(ModelProvider).filter(ModelProvider.id == model_id).delete()
    db.commit(); return {"ok": True}


# ---------- 服务器 ----------
class ServerIn(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str
    auth_type: str = "password"          # password | key
    password: str | None = None
    private_key: str | None = None
    passphrase: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None


@router.post("/servers")
def create_server(body: ServerIn, db: Session = _D(get_db)):
    s = Server(
        name=body.name, host=body.host, port=body.port, username=body.username,
        auth_type=body.auth_type, password_enc=encrypt(body.password),
        private_key_enc=encrypt(body.private_key), passphrase_enc=encrypt(body.passphrase),
        tags=body.tags, description=body.description,
    )
    db.add(s); _commit(db)
    return {"id": s.id, "name": s.name}


@router.get("/servers")
def list_servers(db: Session = _D(get_db)):
    return [{"id": s.id, "name": s.name, "host": s.host, "port": s.port,
             "username": s.username, "auth_type": s.auth_type, "tags": s.tags}
            for s in db.query(Server).all()]


@router.get("/servers/{server_id}")
def get_server(server_id: int, db: Session = _D(get_db)):
    s = db.get(Server, server_id)
    if s is None:
        raise HTTPException(404, "服务器不存在")
    return {"id": s.id, "name": s.name, "host": s.host, "port": s.port,
            "username": s.username, "auth_type": s.auth_type, "tags": s.tags,
            "description": s.description,
            "has_password": bool(s.password_enc), "has_key": bool(s.private_key_enc)}


@router.put("/servers/{server_id}")
def update_server(server_id: int, body: ServerIn, db: Session = _D(get_db)):
    s = db.get(Server, server_id)
    if s is None:
        raise HTTPException(404, "服务器不存在")
    s.name, s.host, s.port, s.username = body.name, body.host, body.port, body.username
    s.auth_type, s.tags, s.description = body.auth_type, body.tags, body.description
    # 密钥类字段:留空表示"保持不变",只有传了新值才覆盖
    if body.password:
        s.password_enc = encrypt(body.password)
    if body.private_key:
        s.private_key_enc = encrypt(body.private_key)
    if body.passphrase:
        s.passphrase_enc = encrypt(body.passphrase)
    _commit(db)
    return {"id": s.id, "name": s.name}


@router.post("/servers/{server_id}/test")
async def test_server(server_id: int, db: Session = _D(get_db)):
    from app.tools.ssh import test_server_connection

    s = db.get(Server, server_id)
    if s is None:
        raise HTTPException(404, "服务器不存在")
    return await test_server_connection(s.name)


@router.delete("/servers/{server_id}")
def delete_server(server_id: int, db: Session = _D(get_db)):
    db.query(Server).filter(Server.id == server_id).delete()
    db.commit(); return {"ok": True}


# ---------- 云账号(MCP)----------
class CloudIn(BaseModel):
    name: str
    cloud_type: CloudType
    transport: str = "stdio"             # stdio | streamable_http
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)   # 明文传入,落库加密
    enabled: bool = True


@router.post("/cloud-accounts")
def create_cloud(body: CloudIn, db: Session = _D(get_db)):
    acc = CloudAccount(
        name=body.name, cloud_type=body.cloud_type, transport=body.transport,
        command=body.command, args=body.args, url=body.url,
        secrets_enc={k: encrypt(v) for k, v in body.secrets.items()},
        enabled=body.enabled,
    )
    db.add(acc); _commit(db)
    return {"id": acc.id, "name": acc.name}


@router.get("/cloud-accounts")
def list_cloud(db: Session = _D(get_db)):
    return [{"id": a.id, "name": a.name, "cloud_type": a.cloud_type,
             "transport": a.transport, "enabled": a.enabled,
             "secret_keys": list((a.secrets_enc or {}).keys())}
            for a in db.query(CloudAccount).all()]


@router.get("/cloud-accounts/{acc_id}")
def get_cloud(acc_id: int, db: Session = _D(get_db)):
    a = db.get(CloudAccount, acc_id)
    if a is None:
        raise HTTPException(404, "云账号不存在")
    return {"id": a.id, "name": a.name, "cloud_type": a.cloud_type, "transport": a.transport,
            "command": a.command, "args": a.args, "url": a.url, "enabled": a.enabled,
            "secret_keys": list((a.secrets_enc or {}).keys())}  # 不回传密钥明文


@router.put("/cloud-accounts/{acc_id}")
def update_cloud(acc_id: int, body: CloudIn, db: Session = _D(get_db)):
    a = db.get(CloudAccount, acc_id)
    if a is None:
        raise HTTPException(404, "云账号不存在")
    a.name, a.cloud_type, a.transport = body.name, body.cloud_type, body.transport
    a.command, a.args, a.url, a.enabled = body.command, body.args, body.url, body.enabled
    # secrets 留空表示"保持不变";传了则整体覆盖并加密
    if body.secrets:
        a.secrets_enc = {k: encrypt(v) for k, v in body.secrets.items()}
    _commit(db)
    return {"id": a.id, "name": a.name}


@router.post("/cloud-accounts/{acc_id}/test")
async def test_cloud(acc_id: int, db: Session = _D(get_db)):
    from app.tools.mcp_manager import test_cloud_account

    a = db.get(CloudAccount, acc_id)
    if a is None:
        raise HTTPException(404, "云账号不存在")
    db.expunge(a)
    return await test_cloud_account(a)


@router.delete("/cloud-accounts/{acc_id}")
def delete_cloud(acc_id: int, db: Session = _D(get_db)):
    db.query(CloudAccount).filter(CloudAccount.id == acc_id).delete()
    db.commit(); return {"ok": True}


# ---------- 审计 ----------
@router.get("/audits")
def list_audits(limit: int = 100, db: Session = _D(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
    return [{"id": r.id, "tool": r.tool_name, "target": r.target, "command": r.command,
             "level": r.level, "success": r.success, "approved": r.approved,
             "created_at": r.created_at.isoformat()} for r in rows]
