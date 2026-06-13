"""FastAPI 入口。"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin, auth, chat, memory, terminal
from app.config import get_settings
from app.db.base import init_db
from app.db.crypto import verify_token

settings = get_settings()
app = FastAPI(title="运维 Agent", version="0.1.0")

# 无需登录即可访问的路径(登录接口、健康检查、首页与静态资源)
_OPEN_EXACT = {"/", "/health", "/favicon.ico"}


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """未登录则拦截所有 API;首页/登录/静态资源放行(前端据此弹登录框)。"""
    if settings.auth_enabled:
        p = request.url.path
        if not (p in _OPEN_EXACT or p.startswith("/auth") or p.startswith("/static")):
            data = verify_token(request.cookies.get(auth.COOKIE_NAME), settings.session_max_age)
            if not (data and data.get("u") == settings.auth_username):
                return JSONResponse({"detail": "未认证,请先登录"}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(chat.router)
app.include_router(memory.router)
app.include_router(terminal.router)

# 后台静态页(简单的配置 + 对话 UI)
_web_dir = Path(__file__).parent / "web"
if _web_dir.exists():
    app.mount("/static", StaticFiles(directory=_web_dir), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_web_dir / "index.html")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=True)


if __name__ == "__main__":
    run()
