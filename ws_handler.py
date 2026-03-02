"""WebSocket session handler for the NAT server.

Manages session lifecycle: connection, message dispatch, frame
request/response futures, and cleanup on disconnect.
"""

from __future__ import annotations

import asyncio
import collections
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from config import (
    _current_session_id,
    register_ws_connection,
    unregister_ws_connection,
    get_ws_connection,
)

from ws_protocol import INBOUND_TYPES

# Pending frame futures keyed by request_id
_pending_frames: Dict[str, asyncio.Future] = {}
# Map request_id -> session_id for cleanup scoping
_frame_request_sessions: Dict[str, str] = {}

# Per-session stream info (RTSP paths, camera index)
_session_stream_info: Dict[str, Dict[str, Any]] = {}

# Ring buffer for video_stream frames (per session, stores base64-encoded JPEGs)
_MAX_FRAME_BUFFER = 200
_session_frame_buffers: Dict[str, collections.deque] = {}

# Per-session BackgroundFrameBuffer instances (continuous RTSP ingest)
from frame_source import BackgroundFrameBuffer, _build_rtsp_url
_session_bg_buffers: Dict[str, BackgroundFrameBuffer] = {}


def get_stream_info(session_id: str) -> Optional[Dict[str, Any]]:
    return _session_stream_info.get(session_id)


def get_frame_buffer(session_id: str) -> Optional[BackgroundFrameBuffer]:
    """Return the running BackgroundFrameBuffer for a session, or None."""
    return _session_bg_buffers.get(session_id)


def get_latest_ws_frames(session_id: str, count: int = 8) -> List[str]:
    """Return the *count* most recent base64-encoded JPEG frames from the
    video_stream ring buffer for *session_id*.  Returns an empty list when
    no frames have been received yet."""
    buf = _session_frame_buffers.get(session_id)
    if not buf:
        return []
    frames = list(buf)
    return frames[-count:]


async def request_frames_from_runtime(
    session_id: str,
    count: int = 8,
    interval_ms: int = 1250,
    timeout: float = 30.0,
) -> list[bytes]:
    """Send a request_frames message and await the frame_response.

    Returns a list of base64-encoded JPEG strings.
    """
    ws = get_ws_connection(session_id)
    if ws is None:
        raise RuntimeError(f"No WebSocket connection for session {session_id}")

    request_id = uuid.uuid4().hex[:12]
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_frames[request_id] = future
    _frame_request_sessions[request_id] = session_id

    try:
        await ws.send_json({
            "type": "request_frames",
            "request_id": request_id,
            "count": count,
            "interval_ms": interval_ms,
        })
        frames = await asyncio.wait_for(future, timeout=timeout)
        return frames
    finally:
        _pending_frames.pop(request_id, None)
        _frame_request_sessions.pop(request_id, None)


async def send_to_session(session_id: str, message: dict) -> bool:
    """Send a JSON message to a session's WebSocket. Returns False on failure."""
    ws = get_ws_connection(session_id)
    if ws is None:
        logger.warning(f"[WS] No connection for session {session_id[:8]}")
        return False
    try:
        await ws.send_json(message)
        return True
    except Exception as exc:
        logger.warning(f"[WS] Send failed for session {session_id[:8]}: {exc}")
        return False


async def websocket_endpoint(websocket: WebSocket):
    """Main WebSocket handler. Mounted at /ws by the FastAPI app."""
    session_id = websocket.query_params.get("session_id", "")
    if not session_id:
        await websocket.close(code=4001, reason="session_id required")
        return

    await websocket.accept()
    register_ws_connection(session_id, websocket)
    logger.info(f"[WS] Session {session_id} connected")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"[WS] Invalid JSON from {session_id[:8]}")
                continue

            msg_type = msg.get("type", "")
            if msg_type not in INBOUND_TYPES:
                logger.warning(f"[WS] Unknown message type: {msg_type}")
                continue

            await _dispatch(session_id, msg_type, msg, websocket)

    except WebSocketDisconnect:
        logger.info(f"[WS] Session {session_id} disconnected")
    except Exception as exc:
        logger.error(f"[WS] Error in session {session_id}: {exc}")
    finally:
        await _cleanup(session_id)
        unregister_ws_connection(session_id)


async def _dispatch(
    session_id: str, msg_type: str, msg: dict, ws: WebSocket
):
    """Route an inbound message to the appropriate handler."""
    if msg_type == "user_message":
        await _handle_user_message(session_id, msg, ws)

    elif msg_type == "frame_response":
        request_id = msg.get("request_id", "")
        future = _pending_frames.get(request_id)
        if future and not future.done():
            future.set_result(msg.get("frames", []))
        else:
            logger.warning(f"[WS] Unexpected frame_response for request {request_id}")

    elif msg_type == "stream_info":
        _session_stream_info[session_id] = msg
        logger.info(
            f"[WS] Stream info for session {session_id[:8]}: "
            f"camera {msg.get('camera_index')}"
        )
        _start_bg_buffer(session_id, msg)

    elif msg_type == "video_stream":
        buf = _session_frame_buffers.setdefault(
            session_id, collections.deque(maxlen=_MAX_FRAME_BUFFER)
        )
        data = msg.get("data", "")
        if data:
            buf.append(data)

    elif msg_type == "protocol_push":
        await _handle_protocol_push(session_id, msg)

    elif msg_type == "audio_stream":
        pass  # reserved for future audio processing

    elif msg_type == "ping":
        await ws.send_json({"type": "pong"})


def _start_bg_buffer(session_id: str, stream_info: dict) -> None:
    """Start a BackgroundFrameBuffer and eagerly init the VSOP provider."""
    if session_id in _session_bg_buffers:
        return
    try:
        from config import get_config
        cfg = get_config()
        video_cfg = cfg.get("video", {})
        rtsp_url = _build_rtsp_url(video_cfg, session_id)
        buf = BackgroundFrameBuffer(rtsp_url)
        _session_bg_buffers[session_id] = buf
        buf.start()
        logger.info(f"[WS] BackgroundFrameBuffer started for {session_id[:8]} -> {rtsp_url}")
    except Exception as exc:
        logger.warning(f"[WS] Failed to start BackgroundFrameBuffer: {exc}")

    try:
        from tools.vsop_providers import get_vsop_provider_for_session, init_vsop_provider_for_session
        from tools.protocols.events import on_step_event
        from config import get_config
        cfg = get_config()
        provider = get_vsop_provider_for_session(session_id)
        if provider is None:
            provider = init_vsop_provider_for_session(session_id, cfg)
            if provider._on_step_event is None:
                provider.set_on_step_event(on_step_event)
            logger.info(f"[WS] VSOP provider eagerly initialized for {session_id[:8]}")
    except Exception as exc:
        logger.warning(f"[WS] Early VSOP init failed: {exc}")


async def _handle_protocol_push(session_id: str, msg: dict) -> None:
    """Write runtime-pushed protocols to the protocols/ directory."""
    from pathlib import Path

    protocols = msg.get("protocols", [])
    if not protocols:
        return

    proto_dir = Path("protocols")
    proto_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for proto in protocols:
        name = proto.get("name", "").strip()
        content = proto.get("content", "").strip()
        if not name or not content:
            continue
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
        if not safe_name.endswith((".txt", ".md", ".yaml", ".json", ".csv")):
            safe_name += ".txt"
        dest = proto_dir / safe_name
        dest.write_text(content, encoding="utf-8")
        count += 1

    if count:
        logger.info(f"[WS] Received {count} protocol(s) from runtime session {session_id[:8]}")


async def _handle_user_message(session_id: str, msg: dict, ws: WebSocket):
    """Process a user_message by running the agent."""
    _current_session_id.set(session_id)
    text = msg.get("text", "").strip()
    if not text:
        return

    # Lazy import to avoid circular deps at module load
    from server import handle_chat_for_ws
    try:
        response_text = await handle_chat_for_ws(session_id, text)
        await ws.send_json({
            "type": "agent_response",
            "text": response_text,
            "tts": True,
        })
    except Exception as exc:
        logger.error(f"[WS] Agent error for session {session_id[:8]}: {exc}")
        await ws.send_json({
            "type": "agent_response",
            "text": "Sorry, something went wrong. Please try again.",
            "tts": True,
        })


async def _cleanup(session_id: str):
    """Clean up session resources on disconnect."""
    _session_stream_info.pop(session_id, None)
    _session_frame_buffers.pop(session_id, None)

    bg_buf = _session_bg_buffers.pop(session_id, None)
    if bg_buf is not None:
        await bg_buf.stop()

    # Cancel pending frame futures belonging to this session only
    for rid, sid in list(_frame_request_sessions.items()):
        if sid == session_id:
            future = _pending_frames.get(rid)
            if future and not future.done():
                future.cancel()
            _pending_frames.pop(rid, None)
            _frame_request_sessions.pop(rid, None)

    # Stop VSOP provider if running
    try:
        from tools.vsop_providers import get_vsop_provider_for_session
        provider = get_vsop_provider_for_session(session_id)
        if provider and provider.is_active:
            await provider.stop()
    except Exception as exc:
        logger.warning(f"[WS] VSOP cleanup failed for {session_id[:8]}: {exc}")
