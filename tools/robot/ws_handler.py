"""Dedicated WebSocket endpoint for robot runtime connections.

Handles the robot lifecycle: registration, tool execution dispatch,
result resolution, and cleanup on disconnect.
"""

from __future__ import annotations

import json

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from tools.robot import get_robot_manager
from ws_protocol import ROBOT_INBOUND_TYPES


async def robot_websocket_endpoint(websocket: WebSocket):
    """Main handler mounted at /ws/robot."""
    session_id = websocket.query_params.get("session_id", "")
    if not session_id:
        await websocket.close(code=4001, reason="session_id required")
        return

    await websocket.accept()
    manager = get_robot_manager()
    registered = False

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"[Robot WS] Invalid JSON from {session_id[:8]}")
                continue

            msg_type = msg.get("type", "")

            if msg_type == "robot_register":
                tools = msg.get("tools", [])
                manager.on_register(session_id, websocket, tools)
                registered = True
                logger.info(
                    f"[Robot WS] Session {session_id} registered "
                    f"{len(tools)} tools"
                )

            elif msg_type == "robot_result":
                request_id = msg.get("request_id", "")
                manager.resolve_result(request_id, {
                    "success": msg.get("success", False),
                    "result": msg.get("result", ""),
                    "tool_name": msg.get("tool_name", ""),
                })

            elif msg_type == "pong":
                pass

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            else:
                logger.debug(f"[Robot WS] Unhandled message type: {msg_type}")

    except WebSocketDisconnect:
        logger.info(f"[Robot WS] Session {session_id} disconnected")
    except Exception as exc:
        logger.error(f"[Robot WS] Error in session {session_id}: {exc}")
    finally:
        if registered:
            manager.on_disconnect(session_id)
