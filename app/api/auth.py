"""控制台登录(单用户,凭证来自 .env)。

启用条件:AUTH_PASSWORD 非空。登录成功后下发签名 Cookie(oa_session),
后续请求由 main.py 的中间件校验;WebSocket 在各自端点里校验同一 Cookie。
"""
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import get_settings
from app.db.crypto import sign_token, verify_token

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

COOKIE_NAME = "oa_session"


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, response: Response):
    if not settings.auth_enabled:
        return {"ok": True, "auth_enabled": False}
    ok = (secrets.compare_digest(body.username, settings.auth_username)
          and secrets.compare_digest(body.password, settings.auth_password))
    if not ok:
        raise HTTPException(401, "用户名或密码错误")
    token = sign_token({"u": body.username, "t": int(time.time())})
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        max_age=settings.session_max_age, path="/")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    if not settings.auth_enabled:
        return {"authenticated": True, "auth_enabled": False, "username": None}
    data = verify_token(request.cookies.get(COOKIE_NAME), settings.session_max_age)
    authed = bool(data and data.get("u") == settings.auth_username)
    return {"authenticated": authed, "auth_enabled": True,
            "username": settings.auth_username if authed else None}
