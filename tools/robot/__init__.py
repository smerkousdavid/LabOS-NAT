"""Robot connection manager.

Tracks connected robot WebSocket sessions, their registered tools,
and provides an async request/response bridge for calling robot tools.
Automatically enables/disables LLM-facing robot tools based on
connection state.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import WebSocket
from loguru import logger

from config import set_tool_enabled_many

# Tool names that get toggled when a robot connects/disconnects
ROBOT_LLM_TOOL_NAMES = [
    "robot_get_status",
    "robot_list_objects",
    "robot_start_protocol",
    "robot_stop",
    "robot_gripper",
    "robot_go_home",
]


class _RobotSession:
    """State for a single connected robot."""

    __slots__ = ("session_id", "ws", "tools")

    def __init__(self, session_id: str, ws: WebSocket, tools: Dict[str, dict]):
        self.session_id = session_id
        self.ws = ws
        self.tools = tools


class RobotConnectionManager:
    """Singleton that manages robot WebSocket connections."""

    def __init__(self):
        self._sessions: Dict[str, _RobotSession] = {}
        self._pending: Dict[str, asyncio.Future] = {}
        self._pending_sessions: Dict[str, str] = {}

    # -- lifecycle -----------------------------------------------------------

    def on_register(self, session_id: str, ws: WebSocket, tools: List[dict]):
        tool_map = {t["name"]: t for t in tools}
        self._sessions[session_id] = _RobotSession(session_id, ws, tool_map)
        logger.info(
            f"[Robot] Registered session {session_id} with "
            f"{len(tool_map)} tools: {', '.join(tool_map.keys())}"
        )
        set_tool_enabled_many({name: True for name in ROBOT_LLM_TOOL_NAMES})

    def on_disconnect(self, session_id: str):
        self._sessions.pop(session_id, None)
        for rid, sid in list(self._pending_sessions.items()):
            if sid == session_id:
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_exception(RuntimeError("Robot disconnected"))
                self._pending_sessions.pop(rid, None)

        if not self._sessions:
            set_tool_enabled_many({name: False for name in ROBOT_LLM_TOOL_NAMES})
            logger.info("[Robot] No robots connected; disabled robot LLM tools")
        else:
            logger.info(f"[Robot] Session {session_id} disconnected; {len(self._sessions)} remaining")

    # -- queries -------------------------------------------------------------

    def is_connected(self) -> bool:
        return bool(self._sessions)

    def get_robot_tools(self) -> Dict[str, dict]:
        """Return the merged tool catalog from all connected robots."""
        merged: Dict[str, dict] = {}
        for sess in self._sessions.values():
            merged.update(sess.tools)
        return merged

    # -- tool execution ------------------------------------------------------

    async def call_tool(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Send robot_execute and await robot_result."""
        if not self._sessions:
            return {"success": False, "result": "No robot connected."}

        session = next(iter(self._sessions.values()))

        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        self._pending_sessions[request_id] = session.session_id

        try:
            await session.ws.send_json({
                "type": "robot_execute",
                "request_id": request_id,
                "tool_name": tool_name,
                "arguments": arguments or {},
            })
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return {"success": False, "result": f"Robot tool '{tool_name}' timed out after {timeout}s."}
        except Exception as exc:
            return {"success": False, "result": f"Robot tool call failed: {exc}"}
        finally:
            self._pending.pop(request_id, None)
            self._pending_sessions.pop(request_id, None)

    def resolve_result(self, request_id: str, result: Dict[str, Any]):
        """Called by the WS handler when a robot_result arrives."""
        future = self._pending.get(request_id)
        if future and not future.done():
            future.set_result(result)
        else:
            logger.warning(f"[Robot] Unexpected robot_result for request {request_id}")


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_manager: Optional[RobotConnectionManager] = None


def get_robot_manager() -> RobotConnectionManager:
    global _manager
    if _manager is None:
        _manager = RobotConnectionManager()
    return _manager
