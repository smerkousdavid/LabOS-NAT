"""TTS push and update_user tool.

Sends TTS messages to the XR runtime via WebSocket instead of HTTP callbacks.
"""

import asyncio
from agents import function_tool
from loguru import logger
from tools.common.toggle import toggle_dashboard

from config import _current_session_id


async def push_tts(message: str, session_id: str | None = None):
    """Send a TTS message through the WebSocket to the XR runtime."""
    from ws_handler import send_to_session
    sid = session_id or _current_session_id.get("default-xr-session")
    await send_to_session(sid, {
        "type": "tts_only",
        "text": message,
        "priority": "normal",
    })


@function_tool
@toggle_dashboard("update_user")
async def update_user(message: str) -> str:
    """Send a progress update to the user while you continue working.
    The message is spoken aloud via TTS. Does NOT end your turn."""
    from ws_handler import send_to_session
    sid = _current_session_id.get("default-xr-session")
    success = await send_to_session(sid, {
        "type": "agent_response",
        "text": message,
        "tts": True,
    })
    if not success:
        return "Failed to send update to user."
    return f"Update sent to user: {message}"
