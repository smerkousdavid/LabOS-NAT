"""LabOS Live Session WebSocket client.

Maintains a per-session WebSocket connection to the LabOS web server,
streaming protocol events, chat messages, and VLM monitoring data in
real-time.  All methods are fire-and-forget; failures are logged but
never block the main NAT pipeline.

Enabled only when a QR code payload is received (``labos_live.enabled``
in config) and a session is active.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

from loguru import logger

try:
    import websockets
    import websockets.client
except ImportError:
    websockets = None  # type: ignore

# ---------------------------------------------------------------------------
# Per-session registry
# ---------------------------------------------------------------------------

_labos_clients: Dict[str, "LabOSLiveClient"] = {}


def get_labos_client(session_id: Optional[str] = None) -> Optional["LabOSLiveClient"]:
    if session_id is None:
        from config import _current_session_id
        session_id = _current_session_id.get("default-xr-session")
    return _labos_clients.get(session_id)


def set_labos_client(session_id: str, client: "LabOSLiveClient"):
    _labos_clients[session_id] = client


def remove_labos_client(session_id: str):
    _labos_clients.pop(session_id, None)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LabOSLiveClient:
    """WebSocket client for streaming events to the LabOS web frontend."""

    def __init__(
        self,
        ws_endpoint: str,
        session_id: str,
        token: str = "",
        on_start_protocol: Optional[Callable[..., Coroutine]] = None,
    ):
        self._ws_endpoint = ws_endpoint
        self._session_id = session_id
        self._token = token
        self._ws = None
        self._connected = False
        self._receive_task: Optional[asyncio.Task] = None
        self._on_start_protocol = on_start_protocol

    # -- lifecycle -----------------------------------------------------------

    async def connect(self):
        if websockets is None:
            logger.error("[LabOSLive] websockets package not installed")
            return

        try:
            headers = {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._ws = await websockets.client.connect(
                self._ws_endpoint,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self._connected = True
            self._receive_task = asyncio.create_task(self._receive_loop())
            logger.info(f"[LabOSLive] Connected to {self._ws_endpoint}")
        except Exception as exc:
            logger.error(f"[LabOSLive] Connection failed: {exc}")
            self._connected = False

    async def disconnect(self):
        self._connected = False
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info("[LabOSLive] Disconnected")

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    # -- send helpers --------------------------------------------------------

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def send_event(self, event: dict):
        if not self.connected:
            return
        event.setdefault("timestamp", self._timestamp())
        try:
            await self._ws.send(json.dumps(event))
        except Exception as exc:
            logger.debug(f"[LabOSLive] Send failed: {exc}")
            self._connected = False

    async def send_chat(self, source: str, message: str):
        await self.send_event({
            "type": "chat",
            "source": source,
            "message": message,
        })

    async def send_monitoring(self, message: str):
        await self.send_event({
            "type": "monitoring",
            "message": message,
        })

    async def send_protocol_start(self, name: str, steps: List[Dict[str, Any]]):
        await self.send_event({
            "type": "protocol_start",
            "name": name,
            "steps": steps,
        })

    async def send_protocol_change_step(self, name: str, previous_step: int, step: int):
        await self.send_event({
            "type": "protocol_change_step",
            "name": name,
            "previous_step": previous_step,
            "step": step,
        })

    async def send_protocol_error(self, name: str, error: str):
        await self.send_event({
            "type": "protocol_error",
            "name": name,
            "error": error,
        })

    async def send_protocol_data(self, name: str, data: dict):
        await self.send_event({
            "type": "protocol_data",
            "name": name,
            "data": data,
        })

    async def send_protocol_stop(self):
        await self.send_event({"type": "protocol_stop"})

    async def send_stream_started(self):
        await self.send_event({"type": "stream_started"})

    async def send_end_stream(self):
        await self.send_event({"type": "end_stream"})

    async def send_ping(self):
        await self.send_event({"type": "ping"})

    # -- receive loop (handles inbound commands from LabOS) ------------------

    async def _receive_loop(self):
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "pong":
                    continue

                if msg_type == "error":
                    logger.warning(f"[LabOSLive] Server error: {msg.get('detail', '')}")
                    continue

                if msg_type == "start_protocol_by_text":
                    await self._handle_start_protocol_by_text(msg)
                    continue

                if msg_type in ("clear_session", "stop_session", "end_session"):
                    await self._handle_clear_session(msg)
                    continue

                logger.debug(f"[LabOSLive] Unknown inbound: {msg_type}")

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"[LabOSLive] Receive loop error: {exc}")
            self._connected = False

    async def _handle_start_protocol_by_text(self, msg: dict):
        """Handle a web-initiated protocol start."""
        name = msg.get("name", "Lab Protocol")
        text = msg.get("text", "")
        if not text:
            return

        logger.info(f"[LabOSLive] Received start_protocol_by_text: {name}")

        if self._on_start_protocol:
            try:
                await self._on_start_protocol(name, text)
            except Exception as exc:
                logger.error(f"[LabOSLive] start_protocol_by_text handler failed: {exc}")
        else:
            from tools.protocols.store import get_protocol_store
            store = get_protocol_store()
            steps = [line.strip() for line in text.strip().split("\n") if line.strip()]
            store.add_protocol({
                "pretty_name": name,
                "steps": steps,
                "raw": text,
            })
            logger.info(f"[LabOSLive] Added protocol '{name}' with {len(steps)} steps from web")

    async def _handle_clear_session(self, msg: dict):
        """Handle a web-initiated session clear/stop."""
        logger.info("[LabOSLive] Received clear_session from server")

        try:
            from tools.vsop_providers import get_vsop_provider_for_session
            provider = get_vsop_provider_for_session(self._session_id)
            if provider and provider.is_active:
                await provider.stop()
        except Exception as exc:
            logger.warning(f"[LabOSLive] Provider stop failed: {exc}")

        try:
            from tools.protocols.state import get_protocol_state
            state = get_protocol_state(self._session_id)
            state.reset()
        except Exception:
            pass

        try:
            from context.manager import _context_managers, ContextManager
            cm = _context_managers.get(self._session_id)
            if cm is None:
                cm = ContextManager()
                _context_managers[self._session_id] = cm
            cm.set_context("main_menu")
        except Exception:
            pass

        try:
            from ws_handler import send_to_session
            await send_to_session(self._session_id, {"type": "session_cleared"})
        except Exception as exc:
            logger.warning(f"[LabOSLive] Failed to send session_cleared to runtime: {exc}")

        await self.send_end_stream()
        await self.disconnect()
        try:
            remove_labos_client(self._session_id)
        except Exception:
            pass

        try:
            from tools.display.ui import render_qr_scanning
            await render_qr_scanning()
        except Exception:
            pass
