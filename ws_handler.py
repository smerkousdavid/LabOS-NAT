"""WebSocket session handler for the NAT server.

Manages session lifecycle: connection, message dispatch, frame
request/response futures, and cleanup on disconnect.
"""

from __future__ import annotations

import asyncio
import collections
import json
import re
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

# ---------------------------------------------------------------------------
# Fast-path patterns: bypass LLM for simple step navigation
# ---------------------------------------------------------------------------

_WAKE_PREFIX_RE = re.compile(
    r"^(hey\s+)?stell?a[,\s]*", re.IGNORECASE
)
_NEXT_PATTERNS = re.compile(
    r"\b(next\s*step|next\s*up|next|advance|skip|move\s*on|go\s*next|continue)\b",
    re.IGNORECASE,
)
_PREV_PATTERNS = re.compile(
    r"\b(previous\s*step|prev\s*step|go\s*back|previous|back\s*up|step\s*back|last\s*step|back)\b",
    re.IGNORECASE,
)
_QUESTION_INDICATORS = re.compile(
    r"\b(what|when|how|why|where|which|tell\s*me|explain|describe|detail)\b|\?",
    re.IGNORECASE,
)
_HAS_STEP_NUMBER = re.compile(r"\b(step\s*\d|\d+\s*step|go\s*to|skip\s*to|jump\s*to|\bstep\s+\w+\b.*\d)\b", re.IGNORECASE)
_STEP_ANNOUNCE_RE = re.compile(r"^Step\s+\d+:", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Direct step navigation helpers (bypass @function_tool wrappers)
# ---------------------------------------------------------------------------

async def _do_next_step(session_id: str) -> Optional[str]:
    """Advance the protocol by calling the VSOP provider directly."""
    from tools.vsop_providers import get_vsop_provider_for_session
    provider = get_vsop_provider_for_session(session_id)
    if provider is None or not provider.is_active:
        return None
    return await provider.manual_advance()


async def _do_previous_step(session_id: str) -> Optional[str]:
    """Retreat the protocol by calling the VSOP provider directly."""
    from tools.vsop_providers import get_vsop_provider_for_session
    provider = get_vsop_provider_for_session(session_id)
    if provider is None or not provider.is_active:
        return None
    return await provider.manual_retreat()


# Pending frame futures keyed by request_id
_pending_frames: Dict[str, asyncio.Future] = {}
# Map request_id -> session_id for cleanup scoping
_frame_request_sessions: Dict[str, str] = {}

# Per-session stream info (RTSP paths, camera index)
_session_stream_info: Dict[str, Dict[str, Any]] = {}

# Ring buffer for video_stream frames (per session, stores base64-encoded JPEGs)
_MAX_FRAME_BUFFER = 200
_session_frame_buffers: Dict[str, collections.deque] = {}

# Per-session PushFrameBuffer instances (receive frames via WS push)
from frame_source import PushFrameBuffer
_session_bg_buffers: Dict[str, PushFrameBuffer] = {}


def get_stream_info(session_id: str) -> Optional[Dict[str, Any]]:
    return _session_stream_info.get(session_id)


def get_frame_buffer(session_id: str) -> Optional[PushFrameBuffer]:
    """Return the PushFrameBuffer for a session, or None."""
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


async def _try_fast_path(session_id: str, text: str) -> Optional[str]:
    """Return a response string if text matches a fast-path command, else None.

    Only fires when a protocol is active and the text is a simple
    navigation command without question qualifiers.
    """
    from tools.protocols.state import get_protocol_state
    state = get_protocol_state()
    if state.mode != "running" or not state.is_active:
        return None

    cleaned = text.strip()
    if _QUESTION_INDICATORS.search(cleaned):
        return None
    if _HAS_STEP_NUMBER.search(cleaned):
        return None

    if _NEXT_PATTERNS.search(cleaned):
        logger.info(f"[FastPath] next_step triggered by: {cleaned}")
        try:
            return await _do_next_step(session_id)
        except Exception as exc:
            logger.error(f"[FastPath] next_step failed: {exc}")
            return None

    if _PREV_PATTERNS.search(cleaned):
        logger.info(f"[FastPath] previous_step triggered by: {cleaned}")
        try:
            return await _do_previous_step(session_id)
        except Exception as exc:
            logger.error(f"[FastPath] previous_step failed: {exc}")
            return None

    return None


async def _dispatch(
    session_id: str, msg_type: str, msg: dict, ws: WebSocket
):
    """Route an inbound message to the appropriate handler."""
    if msg_type == "user_message":
        await _handle_user_message(session_id, msg, ws)

    elif msg_type == "fast_command":
        await _handle_fast_command(session_id, msg, ws)

    elif msg_type == "frame_response":
        request_id = msg.get("request_id", "")
        future = _pending_frames.get(request_id)
        if future and not future.done():
            future.set_result(msg.get("frames", []))
        else:
            logger.warning(f"[WS] Unexpected frame_response for request {request_id}")

    elif msg_type == "stream_info":
        _current_session_id.set(session_id)
        _session_stream_info[session_id] = msg
        logger.info(
            f"[WS] Stream info for session {session_id[:8]}: "
            f"camera {msg.get('camera_index')}"
        )
        _start_bg_buffer(session_id, msg)
        try:
            from tools.display.ui import render_greeting
            await render_greeting()
        except Exception as exc:
            logger.warning(f"[WS] Failed to push greeting for {session_id[:8]}: {exc}")

    elif msg_type == "video_stream":
        data = msg.get("data", "")
        if data:
            push_buf = _session_bg_buffers.get(session_id)
            if push_buf is not None:
                push_buf.push(data)
            else:
                buf = _session_frame_buffers.setdefault(
                    session_id, collections.deque(maxlen=_MAX_FRAME_BUFFER)
                )
                buf.append(data)

    elif msg_type == "protocol_push":
        await _handle_protocol_push(session_id, msg)

    elif msg_type == "qr_payload":
        await _handle_qr_payload(session_id, msg, ws)

    elif msg_type == "ping":
        await ws.send_json({"type": "pong"})


def _start_bg_buffer(session_id: str, stream_info: dict) -> None:
    """Create a PushFrameBuffer and eagerly init the VSOP provider."""
    if session_id not in _session_bg_buffers:
        buf = PushFrameBuffer()
        _session_bg_buffers[session_id] = buf
        logger.info(f"[WS] PushFrameBuffer created for {session_id[:8]}")

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
    """Store runtime-pushed protocols in per-session memory (not disk)."""
    from tools.protocols.state import get_protocol_state
    from tools.protocols.store import build_protocol_entry, _parse_steps

    protocols = msg.get("protocols", [])
    if not protocols:
        return

    state = get_protocol_state(session_id)
    count = 0
    for proto in protocols:
        name = proto.get("name", "").strip()
        content = proto.get("content", "").strip()
        if not name or not content:
            continue
        steps = _parse_steps(content)
        safe_key = name.lower().replace(" ", "_")
        state.session_protocols[safe_key] = build_protocol_entry(name, steps, content)
        count += 1

    if count:
        logger.info(f"[WS] Stored {count} session protocol(s) for {session_id[:8]} (in-memory)")


async def _handle_fast_command(session_id: str, msg: dict, ws: WebSocket):
    """Handle a fast_command from the bridge (partial STT fast path)."""
    _current_session_id.set(session_id)
    command = msg.get("command", "")
    logger.info(f"[FastPath] fast_command received: {command}")

    from tools.protocols.state import get_protocol_state
    state = get_protocol_state()
    if state.mode != "running" or not state.is_active:
        logger.info(f"[FastPath] Ignored {command} -- protocol not active (mode={state.mode})")
        await ws.send_json({
            "type": "agent_response",
            "text": "No protocol running yet.",
            "tts": False,
        })
        return

    try:
        response_text = None
        if command == "next_step":
            response_text = await _do_next_step(session_id)
        elif command == "previous_step":
            response_text = await _do_previous_step(session_id)

        if response_text:
            await ws.send_json({
                "type": "agent_response",
                "text": response_text,
                "tts": False,
            })
        else:
            logger.warning(f"[FastPath] {command} returned no result for session {session_id[:8]}")
    except Exception as exc:
        logger.error(f"[FastPath] {command} failed for session {session_id[:8]}: {exc}")
        await ws.send_json({
            "type": "agent_response",
            "text": "Step navigation failed. Please try again.",
            "tts": True,
        })


async def _handle_user_message(session_id: str, msg: dict, ws: WebSocket):
    """Process a user_message by running the agent (or Gemini Live in full mode)."""
    _current_session_id.set(session_id)
    text = msg.get("text", "").strip()
    if not text:
        return

    # --- Fast path: strip wake-word prefix and check for simple nav commands ---
    normalized = _WAKE_PREFIX_RE.sub("", text).strip()
    fast_result = await _try_fast_path(session_id, normalized or text)
    if fast_result:
        tts_ok = not bool(_STEP_ANNOUNCE_RE.match(fast_result.strip()))
        await ws.send_json({
            "type": "agent_response",
            "text": fast_result,
            "tts": tts_ok,
        })
        await _emit_labos_chat(session_id, "user", text)
        await _emit_labos_chat(session_id, "assistant", fast_result)
        return

    from config import get_gemini_mode

    try:
        if get_gemini_mode() == "full":
            response_text = await _handle_gemini_message(session_id, text)
        else:
            from server import handle_chat_for_ws
            response_text = await handle_chat_for_ws(session_id, text)

        tts_enabled = not bool(_STEP_ANNOUNCE_RE.match(response_text.strip()))
        await ws.send_json({
            "type": "agent_response",
            "text": response_text,
            "tts": tts_enabled,
        })

        await _emit_labos_chat(session_id, "user", text)
        await _emit_labos_chat(session_id, "assistant", response_text)
    except Exception as exc:
        logger.error(f"[WS] Agent error for session {session_id[:8]}: {exc}")
        await ws.send_json({
            "type": "agent_response",
            "text": "Sorry, something went wrong. Please try again.",
            "tts": True,
        })


async def _handle_gemini_message(session_id: str, text: str) -> str:
    """Route a user message through the Gemini VLM provider (full mode)."""
    from tools.vsop_providers import get_vsop_provider_for_session
    from tools.vsop_providers.gemini_vlm import GeminiVLMProvider

    provider = get_vsop_provider_for_session(session_id)
    if isinstance(provider, GeminiVLMProvider):
        return await provider.handle_user_message(text)

    from server import handle_chat_for_ws
    return await handle_chat_for_ws(session_id, text)


async def _emit_labos_chat(session_id: str, source: str, message: str):
    """Send a chat event to the LabOS Live client (no-op if not connected)."""
    try:
        from labos_live_client import get_labos_client
        client = get_labos_client(session_id)
        if client and client.connected:
            await client.send_chat(source, message)
    except Exception:
        pass


async def _emit_labos_monitoring(session_id: str, message: str):
    """Send a monitoring event to the LabOS Live client."""
    try:
        from labos_live_client import get_labos_client
        client = get_labos_client(session_id)
        if client and client.connected:
            await client.send_monitoring(message)
    except Exception:
        pass


async def _handle_qr_payload(session_id: str, msg: dict, ws):
    """Process a QR code payload from the runtime to start a LabOS Live session."""
    payload = msg.get("payload", {})
    if isinstance(payload, str):
        payload = json.loads(payload.strip())

    if not isinstance(payload, dict):
        logger.warning(f"[WS] Invalid QR payload for {session_id[:8]}")
        return

    try:
        payload_text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    except Exception:
        payload_text = str(payload)
    logger.info(f"[WS] Raw QR payload for {session_id[:8]}: {payload_text}")
    print(f"[WS] QR payload ({session_id[:8]}): {payload_text}", flush=True)

    def _normalize_session_id(value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        try:
            # Accept both compact 32-char hex and canonical UUID forms.
            return str(uuid.UUID(raw))
        except Exception:
            return raw

    is_compact = payload.get("t") == "ll"
    join_error: Optional[str] = None
    if is_compact:
        host = str(payload.get("h", "")).strip()
        rtsp_host = str(payload.get("r", "")).strip()
        raw_session_id = str(payload.get("s", "")).strip()
        join_key = str(payload.get("k", "")).strip()

        if not host or not raw_session_id:
            logger.warning(f"[WS] Invalid compact QR payload for {session_id[:8]}")
            return

        labos_session_id = _normalize_session_id(raw_session_id)
        ws_endpoint = f"ws://{host}/ws/vlm/{labos_session_id}"
        publish_rtsp = f"rtsp://{rtsp_host}/live/{labos_session_id}" if rtsp_host else ""
        token = join_key

        join_url = f"http://{host}/api/v2/live/sessions/{raw_session_id}/join"
        try:
            import httpx
            headers = {"Content-Type": "application/json"}
            # if join_key:
            #     headers["Authorization"] = f"Bearer {join_key}"
            #     headers["X-Session-Key"] = join_key
            async with httpx.AsyncClient(timeout=8.0) as client_http:
                response = await client_http.post(
                    join_url,
                    headers=headers,
                    json={"token": join_key} if join_key else {},
                )
            if response.status_code >= 400:
                join_error = f"Live join failed ({response.status_code})"
                logger.warning(
                    f"[WS] Live join failed ({response.status_code}) for {session_id[:8]}: {response.text[:200]}"
                )
            else:
                logger.info(f"[WS] Live join succeeded for {session_id[:8]}")
        except Exception as exc:
            join_error = f"Live join request error: {exc}"
            logger.warning(f"[WS] Live join request error for {session_id[:8]}: {exc}")
    else:
        if payload.get("type") != "labos_live":
            logger.warning(f"[WS] Invalid QR payload for {session_id[:8]}")
            return
        ws_endpoint = payload.get("ws_endpoint", "")
        labos_session_id = _normalize_session_id(str(payload.get("session_id", "")))
        token = payload.get("token", "")
        publish_rtsp = payload.get("publish_rtsp", "")

    if not ws_endpoint:
        logger.error(f"[WS] QR payload missing ws_endpoint for {session_id[:8]}")
        return

    if join_error:
        err_text = (
            "Live session inactive: failed to join. "
            "Please rescan QR or restart the live session."
        )
        await ws.send_json({
            "type": "notification",
            "text": err_text,
            "tts": True,
        })
        await ws.send_json({
            "type": "session_connect_failed",
            "session_id": labos_session_id,
            "error": join_error,
        })
        from tools.display.ui import render_qr_scanning
        await render_qr_scanning()
        return

    logger.info(f"[WS] QR payload received: session={labos_session_id}, endpoint={ws_endpoint}")

    from labos_live_client import LabOSLiveClient, set_labos_client, remove_labos_client

    old = get_labos_client_for_session(session_id)
    if old:
        await old.disconnect()
        remove_labos_client(session_id)

    from tools.display.ui import render_connecting
    _current_session_id.set(session_id)
    await render_connecting(labos_session_id)

    client = LabOSLiveClient(
        ws_endpoint=ws_endpoint,
        session_id=labos_session_id,
        token=token,
    )
    await client.connect()

    if not client.connected:
        from config import get_labos_live_config
        fallback_base = get_labos_live_config().get("website_base_url", "")
        if fallback_base and labos_session_id:
            fallback_ws = f"{fallback_base.rstrip('/')}/ws/vlm/{labos_session_id}"
            logger.info(f"[WS] QR ws_endpoint failed, trying fallback: {fallback_ws}")
            client = LabOSLiveClient(
                ws_endpoint=fallback_ws,
                session_id=labos_session_id,
                token=token,
            )
            await client.connect()

    if client.connected:
        set_labos_client(session_id, client)
        await client.send_stream_started()

        await ws.send_json({
            "type": "session_connected",
            "session_id": labos_session_id,
            "publish_rtsp": publish_rtsp,
        })

        from tools.display.ui import render_greeting
        await render_greeting()

        logger.info(f"[WS] LabOS Live session connected: {labos_session_id}")
    else:
        await ws.send_json({
            "type": "session_connect_failed",
            "session_id": labos_session_id,
            "error": "Failed to connect to LabOS server (tried QR endpoint and fallback)",
        })
        from tools.display.ui import render_qr_scanning
        await render_qr_scanning()


def get_labos_client_for_session(session_id: str):
    """Return the LabOS Live client for a session, or None."""
    try:
        from labos_live_client import get_labos_client
        return get_labos_client(session_id)
    except Exception:
        return None


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

    # Stop and remove VSOP provider
    try:
        from tools.vsop_providers import get_vsop_provider_for_session, _vsop_providers
        provider = get_vsop_provider_for_session(session_id)
        if provider and provider.is_active:
            await provider.stop()
        _vsop_providers.pop(session_id, None)
    except Exception as exc:
        logger.warning(f"[WS] VSOP cleanup failed for {session_id[:8]}: {exc}")

    # Reset protocol state (including session-scoped protocols)
    try:
        from tools.protocols.state import _protocol_states
        if session_id in _protocol_states:
            _protocol_states[session_id].reset(clear_session_protocols=True)
    except Exception as exc:
        logger.warning(f"[WS] Protocol state cleanup failed for {session_id[:8]}: {exc}")

    # Reset context manager to main_menu
    try:
        from context.manager import _context_managers
        if session_id in _context_managers:
            _context_managers[session_id].set_context("main_menu")
    except Exception as exc:
        logger.warning(f"[WS] Context cleanup failed for {session_id[:8]}: {exc}")

    # Cancel completion linger timer if active
    try:
        from tools.protocols.events import _completion_reset_task
        if _completion_reset_task and not _completion_reset_task.done():
            _completion_reset_task.cancel()
    except Exception as exc:
        logger.warning(f"[WS] Completion timer cleanup failed for {session_id[:8]}: {exc}")

    # Cancel and remove running agent tasks
    try:
        from server import _running_tasks
        task = _running_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
    except Exception as exc:
        logger.warning(f"[WS] Task cleanup failed for {session_id[:8]}: {exc}")

    # Clear chat history for a clean session on reconnect
    try:
        from context.session import clear_session
        clear_session(session_id)
    except Exception as exc:
        logger.warning(f"[WS] Session history cleanup failed for {session_id[:8]}: {exc}")

    # Disconnect and remove LabOS Live client
    try:
        from labos_live_client import remove_labos_client
        labos_client = get_labos_client_for_session(session_id)
        if labos_client:
            await labos_client.send_end_stream()
            await labos_client.disconnect()
            remove_labos_client(session_id)
    except Exception as exc:
        logger.warning(f"[WS] LabOS Live cleanup failed for {session_id[:8]}: {exc}")

    logger.info(f"[WS] Full cleanup completed for session {session_id[:8]}")
