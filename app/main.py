"""FastAPI 入口。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin, chat
from app.config import get_settings
from app.db.base import init_db

settings = get_settings()
app = FastAPI(title="运维 Agent", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(admin.router)
app.include_router(chat.router)

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
