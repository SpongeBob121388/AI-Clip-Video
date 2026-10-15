"""WebSocket 进度推送端点。"""

from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import websocket_authenticated
from app.utils.logging import get_logger

logger = get_logger("api.websocket")

router = APIRouter()

# job_id -> 订阅该任务的连接列表（多个客户端可同时订阅同一任务）
_connections: dict[str, list[WebSocket]] = {}

_MAX_MESSAGE_BYTES = 4096


@router.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket) -> None:
    if not await websocket_authenticated(websocket):
        return

    await websocket.accept()
    subscribed: list[str] = []
    try:
        while True:
            data = await websocket.receive_text()
            if len(data) > _MAX_MESSAGE_BYTES:
                continue
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"action": "error", "detail": "非法 JSON"})
                continue

            action = msg.get("action")
            job_id = msg.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                continue

            if action == "subscribe":
                _connections.setdefault(job_id, []).append(websocket)
                subscribed.append(job_id)
                await websocket.send_json({"action": "subscribed", "job_id": job_id})
            elif action == "unsubscribe":
                _detach(job_id, websocket)
                if job_id in subscribed:
                    subscribed.remove(job_id)
                await websocket.send_json({"action": "unsubscribed", "job_id": job_id})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("websocket_error", error_type=type(exc).__name__)
    finally:
        # 断线必须清理全部订阅，否则连接对象无限累积
        for job_id in subscribed:
            _detach(job_id, websocket)


def _detach(job_id: str, websocket: WebSocket) -> None:
    conns = _connections.get(job_id)
    if not conns:
        return
    if websocket in conns:
        conns.remove(websocket)
    if not conns:
        _connections.pop(job_id, None)


async def broadcast_progress(job_id: str, progress: float, message: str) -> None:
    """向某任务的所有订阅者推送进度；失败的连接摘除而不影响其余连接。"""
    payload = {"job_id": job_id, "progress": progress, "message": message}
    for ws in list(_connections.get(job_id, [])):
        try:
            await ws.send_json(payload)
        except Exception:
            _detach(job_id, ws)
