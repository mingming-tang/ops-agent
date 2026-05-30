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
    db.add(m); db.commit()
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
    db.add(s); db.commit()
    return {"id": s.id, "name": s.name}


@router.get("/servers")
def list_servers(db: Session = _D(get_db)):
    return [{"id": s.id, "name": s.name, "host": s.host, "port": s.port,
             "username": s.username, "auth_type": s.auth_type, "tags": s.tags}
            for s in db.query(Server).all()]


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
    db.add(acc); db.commit()
    return {"id": acc.id, "name": acc.name}


@router.get("/cloud-accounts")
def list_cloud(db: Session = _D(get_db)):
    return [{"id": a.id, "name": a.name, "cloud_type": a.cloud_type,
             "transport": a.transport, "enabled": a.enabled,
             "secret_keys": list((a.secrets_enc or {}).keys())}
            for a in db.query(CloudAccount).all()]


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
