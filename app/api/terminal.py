"""交互式 SSH 终端(WebSocket)。

在所选服务器上开一个带 PTY 的持久 shell,把浏览器(xterm.js)与远端 shell
双向桥接:cd/环境变量/sudo 会话都保持,像真终端一样手动操作。

协议:
  - 客户端 → 服务端:文本 JSON 帧
      {"type":"input","data":"<按键/文本>"}
      {"type":"resize","cols":N,"rows":M}
  - 服务端 → 客户端:
      二进制帧 = 终端原始输出(直接喂给 xterm)
      文本 JSON 帧 = 控制信息 {"type":"ready|exit|error", ...}
"""
import asyncio

import asyncssh
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.auth import COOKIE_NAME
from app.config import get_settings
from app.db.crypto import verify_token
from app.tools.ssh import build_conn_kwargs

router = APIRouter(prefix="/chat", tags=["terminal"])
settings = get_settings()


@router.websocket("/terminal")
async def terminal_ws(ws: WebSocket, server: str) -> None:
    await ws.accept()
    # 与 HTTP 一致的登录校验(浏览器握手会带上 oa_session Cookie)
    if settings.auth_enabled:
        data = verify_token(ws.cookies.get(COOKIE_NAME), settings.session_max_age)
        if not (data and data.get("u") == settings.auth_username):
            await ws.send_json({"type": "error", "error": "未认证,请先登录"})
            await ws.close()
            return
    conn_kwargs, err = build_conn_kwargs(server)
    if err:
        await ws.send_json({"type": "error", "error": err})
        await ws.close()
        return

    try:
        async with asyncssh.connect(**conn_kwargs) as conn:
            # command=None + term_type → 在远端开一个交互式登录 shell(带 PTY)
            async with conn.create_process(
                term_type="xterm-256color", term_size=(80, 24), encoding=None,
            ) as proc:
                await ws.send_json({"type": "ready", "server": server})

                async def to_client() -> None:
                    while True:
                        data = await proc.stdout.read(4096)
                        if not data:            # shell 退出(EOF)
                            break
                        await ws.send_bytes(data)

                async def to_proc() -> None:
                    while True:
                        msg = await ws.receive_json()
                        kind = msg.get("type")
                        if kind == "input":
                            proc.stdin.write(msg.get("data", "").encode())
                        elif kind == "resize":
                            proc.change_terminal_size(int(msg.get("cols", 80)),
                                                      int(msg.get("rows", 24)))

                tasks = {asyncio.create_task(to_client()), asyncio.create_task(to_proc())}
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    t.exception()   # 取出异常(如客户端断开),避免 "never retrieved" 告警
                # 若是 shell 主动退出(to_client 结束),告知前端
                try:
                    await ws.send_json({"type": "exit"})
                except Exception:  # noqa: BLE001  连接可能已关闭
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001  连接/认证/PTY 等失败
        try:
            await ws.send_json({"type": "error", "error": f"{type(e).__name__}: {e}"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
