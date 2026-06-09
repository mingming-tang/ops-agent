"""交互式 SSH 终端(WebSocket),会话常驻、可跨刷新重连。

每个终端是一个服务端常驻会话(TermSession):asyncssh 连接 + PTY shell 一直存活,
与浏览器的 WebSocket 解耦。页面刷新只是断开 ws,shell 不死;重新 attach 时回放
历史输出(scrollback),cwd/环境/运行中的程序都还在。断开超过空闲阈值才回收。

接口:
  POST   /chat/terminal/open?server=<名>   新建会话 → {session_id}
  GET    /chat/terminal/sessions           列出存活会话(供刷新后恢复 Tab)
  DELETE /chat/terminal/{sid}              显式关闭会话
  WS     /chat/terminal?session=<sid>      attach;断开自动 detach(会话保留)

ws 协议同前:客户端发文本 JSON(input/resize),服务端回二进制(终端输出)
与文本 JSON 控制帧(ready/exit/error)。
"""
import asyncio
import time
import uuid

import asyncssh
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.api.auth import COOKIE_NAME
from app.config import get_settings
from app.db.crypto import verify_token
from app.tools.ssh import build_conn_kwargs

router = APIRouter(prefix="/chat", tags=["terminal"])
settings = get_settings()

_BUF_CAP = 256 * 1024        # 每会话回放缓冲上限(字节)
_IDLE_TIMEOUT = 30 * 60      # 断开后保留时长(秒),超时回收
_SESSIONS: dict[str, "TermSession"] = {}
_sweeper_started = False


class TermSession:
    def __init__(self, sid: str, server: str, conn, proc) -> None:
        self.id = sid
        self.server = server
        self.conn = conn
        self.proc = proc
        self.buffer = bytearray()
        self.attached: WebSocket | None = None
        self.last_active = time.time()
        self.closed = False
        self.lock = asyncio.Lock()             # 串行化对 ws 的写,保证回放与新输出有序
        self.reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """常驻读取 PTY 输出:写入回放缓冲,并转发给当前 attach 的 ws。"""
        try:
            while True:
                data = await self.proc.stdout.read(4096)
                if not data:                   # shell 退出
                    break
                async with self.lock:
                    self.buffer.extend(data)
                    if len(self.buffer) > _BUF_CAP:
                        del self.buffer[:len(self.buffer) - _BUF_CAP]
                    if self.attached is not None:
                        try:
                            await self.attached.send_bytes(data)
                        except Exception:      # noqa: BLE001  ws 已坏,等 detach
                            pass
        except Exception:                      # noqa: BLE001
            pass
        finally:
            await self._on_shell_exit()

    async def _on_shell_exit(self) -> None:
        if self.attached is not None:
            try:
                await self.attached.send_json({"type": "exit"})
            except Exception:                  # noqa: BLE001
                pass
        await self.close()

    async def attach(self, ws: WebSocket) -> None:
        async with self.lock:
            if self.attached is not None and self.attached is not ws:
                try:
                    await self.attached.close()    # 同一会话只保留最新连接
                except Exception:              # noqa: BLE001
                    pass
            if self.buffer:
                await ws.send_bytes(bytes(self.buffer))   # 回放历史输出
            self.attached = ws
            self.last_active = time.time()

    async def detach(self, ws: WebSocket) -> None:
        async with self.lock:
            if self.attached is ws:
                self.attached = None
                self.last_active = time.time()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        _SESSIONS.pop(self.id, None)
        cur = asyncio.current_task()
        if self.reader is not None and self.reader is not cur:
            self.reader.cancel()
        try:
            self.proc.close()
        except Exception:                      # noqa: BLE001
            pass
        try:
            self.conn.close()
        except Exception:                      # noqa: BLE001
            pass


def _ensure_sweeper() -> None:
    global _sweeper_started
    if not _sweeper_started:
        _sweeper_started = True
        asyncio.create_task(_sweep_loop())


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for s in list(_SESSIONS.values()):
            if s.attached is None and now - s.last_active > _IDLE_TIMEOUT:
                await s.close()


def _ws_authed(ws: WebSocket) -> bool:
    if not settings.auth_enabled:
        return True
    data = verify_token(ws.cookies.get(COOKIE_NAME), settings.session_max_age)
    return bool(data and data.get("u") == settings.auth_username)


@router.post("/terminal/open")
async def terminal_open(server: str):
    conn_kwargs, err = build_conn_kwargs(server)
    if err:
        raise HTTPException(400, err)
    try:
        conn = await asyncssh.connect(**conn_kwargs)
        proc = await conn.create_process(term_type="xterm-256color", term_size=(80, 24), encoding=None)
    except Exception as e:                     # noqa: BLE001  连接/认证/PTY 失败
        raise HTTPException(400, f"连接失败:{type(e).__name__}: {e}") from None
    sid = uuid.uuid4().hex
    _SESSIONS[sid] = TermSession(sid, server, conn, proc)
    _ensure_sweeper()
    return {"session_id": sid, "server": server}


@router.get("/terminal/sessions")
async def terminal_sessions():
    return [{"id": s.id, "server": s.server} for s in _SESSIONS.values() if not s.closed]


@router.delete("/terminal/{sid}")
async def terminal_close(sid: str):
    s = _SESSIONS.get(sid)
    if s is not None:
        await s.close()
    return {"ok": True}


@router.websocket("/terminal")
async def terminal_ws(ws: WebSocket, session: str) -> None:
    await ws.accept()
    if not _ws_authed(ws):
        await ws.send_json({"type": "error", "error": "未认证,请先登录"})
        await ws.close()
        return
    sess = _SESSIONS.get(session)
    if sess is None or sess.closed:
        await ws.send_json({"type": "error", "error": "会话不存在或已结束"})
        await ws.close()
        return

    await sess.attach(ws)
    await ws.send_json({"type": "ready", "server": sess.server})
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "input":
                sess.proc.stdin.write(msg.get("data", "").encode())
                sess.last_active = time.time()
            elif kind == "resize":
                try:
                    sess.proc.change_terminal_size(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                except Exception:              # noqa: BLE001
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:                          # noqa: BLE001
        pass
    finally:
        await sess.detach(ws)
        try:
            await ws.close()
        except Exception:                      # noqa: BLE001
            pass
