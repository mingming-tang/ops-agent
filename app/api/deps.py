"""API 依赖:简单的后台鉴权(起步用 token,生产请换正式鉴权/RBAC)。"""
from fastapi import Header, HTTPException

from app.config import get_settings


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if x_admin_token != get_settings().admin_token:
        raise HTTPException(status_code=401, detail="无效的 admin token")
