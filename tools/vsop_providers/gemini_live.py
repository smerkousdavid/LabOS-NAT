"""Gemini Live API VSOP Provider.

Uses Gemini's Live API to maintain a persistent WebSocket session with
continuous video streaming and tool calling.  Replaces both the STELLA-VLM
monitoring loop and (in ``full`` mode) the Agents SDK chat agent.

Two modes:
  - **full** -- Gemini Live handles user chat + video monitoring in one session.
  - **vision_only** -- Gemini Live handles video monitoring; Agents SDK handles chat.
"""

import asyncio
import base64
import re
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

from loguru import logger

from tools.vsop_providers import StepEvent, StepState, VSOPProvider

# ---------------------------------------------------------------------------
# Tool declarations for the Gemini Live session (lazy-imported types)
# ---------------------------------------------------------------------------

def _build_tool_declarations():
    """Build Gemini FunctionDeclaration list for protocol tools.

    Lazy so ``google.genai`` is only imported when Gemini Live is enabled.
    """
    from google.genai import types

    return [
        types.FunctionDeclaration(
            name="next_step",
            description="Advance the protocol to the next step.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="previous_step",
            description="Return to the previous protocol step.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="go_to_step",
            description="Jump to a specific protocol step by number.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "step_num": types.Schema(type="INTEGER", description="1-based step number"),
                },
                required=["step_num"],
            ),
        ),
        types.FunctionDeclaration(
            name="stop_protocol",
            description="Stop the currently running protocol.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="restart_protocol",
            description="Restart the active protocol from step one.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="list_protocols",
            description="List available laboratory protocols.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="start_protocol",
            description="Start a protocol by name or number.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "protocol_name": types.Schema(type="STRING", description="Protocol name or number"),
                },
                required=["protocol_name"],
            ),
        ),
        types.FunctionDeclaration(
            name="log_observation",
            description="Log a user observation or data point to the experiment record.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "observation": types.Schema(type="STRING", description="What the user observed"),
                    "section": types.Schema(type="STRING", description="Data section name (default: notes)"),
                },
                required=["observation"],
            ),
        ),
        types.FunctionDeclaration(
            name="send_to_display",
            description="Show rich text content on the AR display panel.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "content": types.Schema(type="STRING", description="Rich-text content for the AR panel"),
                    "title": types.Schema(type="STRING", description="Optional panel title"),
                },
                required=["content"],
            ),
        ),
        types.FunctionDeclaration(
            name="show_protocol_panel",
            description="Restore the protocol step view on the AR panel.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="update_user",
            description="Send a spoken progress update to the user via TTS.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "message": types.Schema(type="STRING", description="Message to speak aloud"),
                },
                required=["message"],
            ),
        ),
        types.FunctionDeclaration(
            name="detailed_step",
            description="Show expanded step details with image on the AR display.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "step_num": types.Schema(type="INTEGER", description="Step number (default: current)"),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="get_protocol_status",
            description="Get current protocol status summary.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="show_experiment_data",
            description="Show details from captured experiment data.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "section": types.Schema(type="STRING", description="Section name to show"),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="query_completed_protocol_data",
            description="Query errors, observations, and logged data from the current or completed protocol run.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "question": types.Schema(type="STRING", description="Question about the data, errors, or observations"),
                },
                required=["question"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_errors",
            description="Get all errors recorded during the current protocol run.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="reset_session",
            description="Reset the entire session back to the main menu. Clears protocol state, session protocols, and context. Use when user says 'reset', 'restart session', 'go home', 'main menu', 'start over', or 'clear session'.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

async def _dispatch_tool_call(name: str, args: dict) -> str:
    """Execute a tool by name and return the string result.

    Routes directly to provider methods or module-level functions,
    bypassing the @function_tool wrapper from the Agents SDK.
    """
    from tools.vsop_providers import get_vsop_provider

    provider = get_vsop_provider()
    sid = provider.get_bound_session_id() if provider else None

    # -- Navigation (direct provider calls) ----------------------------------
    if name == "next_step":
        if provider and provider.is_active:
            result = await provider.manual_advance()
            _sync_state_from_provider(provider)
            return result
        return "No active protocol."

    if name == "previous_step":
        if provider and provider.is_active:
            result = await provider.manual_retreat()
            _sync_state_from_provider(provider)
            return result
        return "No active protocol."

    if name == "go_to_step":
        if provider and provider.is_active:
            result = await provider.manual_goto(int(args.get("step_num", 1)))
            _sync_state_from_provider(provider)
            return result
        return "No active protocol."

    if name == "restart_protocol":
        if provider and provider.is_active:
            result = await provider.manual_restart()
            _sync_state_from_provider(provider)
            return result
        return "No active protocol."

    # -- Protocol lifecycle ---------------------------------------------------
    if name == "stop_protocol":
        if provider and provider.is_active:
            result = await provider.stop()
            from tools.protocols.state import get_protocol_state
            state = get_protocol_state(sid)
            state.reset()
            from context.manager import get_context_manager
            get_context_manager().set_context("main_menu")
            from tools.display.ui import render_greeting
            await render_greeting()
            return result
        return "No active protocol."

    if name == "reset_session":
        from tools.protocols.state import get_protocol_state
        from context.manager import get_context_manager
        from tools.display.ui import render_greeting
        state = get_protocol_state(sid)
        if provider and provider.is_active:
            try:
                await provider.stop()
            except Exception:
                pass
        state.reset(clear_session_protocols=True)
        get_context_manager().set_context("main_menu")
        try:
            await render_greeting()
        except Exception:
            pass
        return "Session reset. Back at main menu."

    if name == "list_protocols":
        from tools.protocols.store import get_protocol_store, list_available_protocols
        from tools.display.ui import render_protocol_list
        from tools.protocols.state import get_protocol_state
        from context.manager import get_context_manager
        store = get_protocol_store()
        state = get_protocol_state(sid)
        protocols = list_available_protocols(store, state)
        if not protocols:
            return "No protocols are currently available in the database."
        await render_protocol_list(store, state=state)
        state.mode = "listing"
        get_context_manager().set_context("protocol_listing")
        names = ", ".join(p.get("pretty_name", p.get("name", "unknown")) for p in protocols)
        return (
            f"Here are the available protocols: {names}. "
            "Say the number or name of the protocol you'd like to run."
        )

    if name == "start_protocol":
        from tools.protocols.store import (
            find_available_protocol,
            get_protocol_store,
            list_available_protocols,
        )
        from tools.protocols.state import get_protocol_state, StepDetail
        from context.manager import get_context_manager
        from tools.display import ui as viture_ui

        store = get_protocol_store()
        state = get_protocol_state(sid)
        proto_name = args.get("protocol_name", "")
        proto = find_available_protocol(proto_name, store, state)
        if not proto:
            try:
                idx = int(proto_name)
                protocols = list_available_protocols(store, state)
                if 1 <= idx <= len(protocols):
                    proto = protocols[idx - 1]
            except (ValueError, TypeError):
                pass
        if proto:
            if provider and provider.is_active:
                await provider.stop()
                state.reset()

            fallback_steps = list(proto.get("steps", []))
            step_texts = [" ".join(s.split()).strip() for s in fallback_steps if s.strip()]
            if not step_texts:
                step_texts = ["Follow protocol instructions."]

            step_details = [StepDetail(text=t, description=t) for t in step_texts]

            state.is_active = True
            state.mode = "running"
            state.protocol_name = proto["pretty_name"]
            state.steps = step_details
            state.current_step = 1
            state.completed_steps = []
            state.error_history = []
            state.start_time = time.time()
            state.stella_vision_text = ""
            state.extra_context = ""
            state.experiment_data = {
                "protocol_name": proto["pretty_name"],
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                "sections": {},
            }
            state.data_capture_hashes = []
            state.error_display_until = 0.0
            state.error_cooldown_until = 0.0

            get_context_manager().set_context("protocol_running")

            result = await provider.start(
                protocol_name=proto["pretty_name"],
                protocol_steps=step_texts,
                protocol_context=state.extra_context,
            )

            await viture_ui.render_step_panel(state)

            import asyncio as _aio
            from tools.protocols.tools import _refine_steps_background
            try:
                _aio.create_task(
                    _refine_steps_background(
                        protocol_name=proto["pretty_name"],
                        raw_protocol=proto.get("raw", ""),
                        fallback_steps=fallback_steps,
                        state=state,
                        provider=provider,
                    )
                )
            except Exception:
                pass

            return result
        return f"Protocol '{proto_name}' not found."

    # -- Data logging ---------------------------------------------------------
    if name == "log_observation":
        observation = args.get("observation", "")
        section = args.get("section", "observations")
        from tools.protocols.tools import _log_observation_impl
        return await _log_observation_impl(
            observation=observation,
            section=section,
            session_id=sid,
        )

    # -- Display / TTS --------------------------------------------------------
    if name == "send_to_display":
        from tools.display.ui import render_rich_panel
        content = args.get("content", "")
        title = args.get("title", "")
        if title:
            content = f"<size=22><b>{title}</b></size><br><br>{content}"
        await render_rich_panel([{"type": "rich-text", "content": content}])
        return "Content displayed on XR panel."

    if name == "show_protocol_panel":
        from tools.protocols.state import get_protocol_state
        from tools.display.ui import render_step_panel, render_greeting
        state = get_protocol_state(sid)
        if state.mode == "running" and state.steps:
            await render_step_panel(state)
            return "Protocol step panel restored."
        await render_greeting()
        return "No active protocol. Showing greeting panel."

    if name == "update_user":
        from tools.display.tts import push_tts
        msg = args.get("message", "")
        if msg:
            await push_tts(msg)
        return f"Spoken: {msg}" if msg else "Nothing to say."

    # -- Protocol queries -----------------------------------------------------
    if name == "get_protocol_status":
        from tools.protocols.state import get_protocol_state
        state = get_protocol_state(sid)
        if not state.is_active and state.mode != "completed":
            return "No active protocol."
        return (
            f"Protocol: {state.protocol_name}, "
            f"Step {state.current_step}/{len(state.steps)}, "
            f"Elapsed: {state.elapsed_str()}"
        )

    if name == "get_errors":
        from tools.protocols.state import get_protocol_state
        state = get_protocol_state(sid)
        errors = state.error_history
        if not errors:
            return "No errors recorded in this session."
        lines = []
        for e in errors:
            lines.append(f"Step {e.get('step', '?')}: {e.get('detail', 'unknown')}")
        return f"{len(errors)} error(s):\n" + "\n".join(lines)

    if name == "detailed_step":
        step_num = args.get("step_num")
        from tools.protocols.state import get_protocol_state
        state = get_protocol_state(sid)
        if not state.is_active:
            return "No active protocol."
        idx = (step_num or state.current_step) - 1
        if 0 <= idx < len(state.steps):
            step = state.steps[idx]
            return f"Step {idx + 1}: {step.text}\nDetails: {step.description or 'No additional details.'}"
        return "Invalid step number."

    if name == "show_experiment_data":
        from tools.protocols.state import get_protocol_state
        state = get_protocol_state(sid)
        section = args.get("section", "")
        data = state.experiment_data.get("sections", {})
        if not data:
            return "No experiment data recorded yet."
        if section and section in data:
            rows = data[section].get("rows", [])
            lines = []
            for r in rows[-15:]:
                lines.append(f"  Step {r.get('_step','?')} ({r.get('_timestamp','?')}): {r.get('note', r)}")
            return f"Section '{section}': {len(rows)} entries.\n" + "\n".join(lines)
        all_entries = []
        for sec_name, sec_data in data.items():
            for r in sec_data.get("rows", []):
                all_entries.append(f"  [{sec_name}] Step {r.get('_step','?')} ({r.get('_timestamp','?')}): {r.get('note', r)}")
        if all_entries:
            return f"All experiment data ({len(all_entries)} entries):\n" + "\n".join(all_entries[-15:])
        return f"Available sections: {', '.join(data.keys()) or 'none'} (all empty)"

    if name == "query_completed_protocol_data":
        from tools.protocols.state import get_protocol_state
        state = get_protocol_state(sid)
        question = args.get("question", "")
        data = state.experiment_data.get("sections", {})
        errors = state.error_history

        context_parts = []
        if errors:
            context_parts.append(f"Errors ({len(errors)}):")
            for e in errors:
                context_parts.append(f"  Step {e.get('step','?')}: {e.get('detail','unknown')}")
        if data:
            for sec_name, sec_data in data.items():
                rows = sec_data.get("rows", [])
                if rows:
                    context_parts.append(f"Observations ({sec_name}, {len(rows)} entries):")
                    for r in rows:
                        context_parts.append(f"  Step {r.get('_step','?')} ({r.get('_timestamp','?')}): {r.get('note', r)}")

        if not context_parts:
            return "No experiment data or errors found for this session."
        return "\n".join(context_parts)

    logger.warning(f"[GeminiLive] Unknown tool call: {name}")
    return f"Unknown tool: {name}"


def _sync_state_from_provider(provider):
    """Keep ProtocolState in sync with the provider after navigation."""
    from tools.protocols.state import get_protocol_state
    state = get_protocol_state(provider.get_bound_session_id())
    if not state.is_active:
        return
    state.current_step = provider._current_step
    state.completed_steps = list(provider._completed_steps)
    for i, step in enumerate(state.steps):
        if (i + 1) in state.completed_steps:
            step.status = "completed"
        elif (i + 1) == state.current_step:
            step.status = "in_progress"


# ---------------------------------------------------------------------------
# Monitoring prompt
# ---------------------------------------------------------------------------

MONITORING_PROMPT = (
    "Assess the current protocol step from the live video. "
    "Only flag ERROR for concrete protocol execution mistakes (wrong reagent, wrong count, skipped sub-step). "
    "Do NOT flag ERROR for: user distracted, on phone, idle, paused, not started, or away. Those are SAME.\n"
    "Reply in EXACTLY 3 lines:\n"
    "STATUS: <SAME|STEP_COMPLETE|ERROR>\n"
    "DETAIL: <1-2 sentences: what you see the user doing right now>\n"
    "ERROR: <if ERROR, describe the specific protocol mistake with expected vs actual. Otherwise: none>"
)


# ---------------------------------------------------------------------------
# GeminiLiveSession
# ---------------------------------------------------------------------------

class GeminiLiveSession:
    """Manages a persistent Gemini Live API WebSocket session.

    Streams video frames, accepts text/audio, handles tool calls,
    and supports periodic monitoring prompts.
    """

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._gemini_cfg = config.get("gemini_live", {})
        self._client = None
        self._session_cm = None
        self._session = None
        self._frame_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._connected = False
        self._frame_buffer = None
        self._reconnect_delay = 1.0
        self._MAX_RECONNECT_DELAY = 30.0
        self._lock = asyncio.Lock()
        self._pending_responses: asyncio.Queue = asyncio.Queue()

    async def connect(self, system_instruction: str, frame_buffer=None):
        """Open the Gemini Live session with sliding window compression."""
        from google import genai
        from google.genai import types

        api_key = self._gemini_cfg.get("api_key", "")
        from config import _resolve_env_vars
        api_key = _resolve_env_vars(api_key)

        self._frame_buffer = frame_buffer

        model = self._gemini_cfg.get("model", "gemini-3.1-flash-lite-preview")
        cw_cfg = self._gemini_cfg.get("context_window", {})
        trigger_tokens = cw_cfg.get("trigger_tokens", 250000)
        target_tokens = cw_cfg.get("target_tokens", 125000)

        tool_decls = _build_tool_declarations()

        api_versions = [self._gemini_cfg.get("api_version", "v1beta"), "v1alpha"]
        api_versions = list(dict.fromkeys([v for v in api_versions if v]))
        model_candidates = [model]
        if "native-audio" not in model.lower():
            model_candidates.append("gemini-3.1-flash-lite-preview")
        model_candidates = list(dict.fromkeys(model_candidates))

        last_exc = None
        for api_version in api_versions:
            self._client = genai.Client(
                api_key=api_key,
                http_options={"api_version": api_version},
            )
            for candidate in model_candidates:
                modality_options = (
                    [["AUDIO", "TEXT"], ["AUDIO"]]
                    if "native-audio" in str(candidate).lower()
                    else [["TEXT"]]
                )
                candidate_name = (
                    candidate if str(candidate).startswith("models/") else f"models/{candidate}"
                )
                for response_modalities in modality_options:
                    live_config = types.LiveConnectConfig(
                        response_modalities=response_modalities,
                        system_instruction=types.Content(
                            parts=[types.Part(text=system_instruction)],
                        ),
                        tools=[types.Tool(function_declarations=tool_decls)],
                        context_window_compression=types.ContextWindowCompressionConfig(
                            trigger_tokens=trigger_tokens,
                            sliding_window=types.SlidingWindow(target_tokens=target_tokens),
                        ),
                    )
                    try:
                        # google-genai live.connect() returns an async context manager.
                        # Keep the context manager instance so we can close cleanly later.
                        self._session_cm = self._client.aio.live.connect(
                            model=candidate_name,
                            config=live_config,
                        )
                        self._session = await self._session_cm.__aenter__()
                        self._connected = True
                        self._reconnect_delay = 1.0
                        logger.info(
                            f"[GeminiLive] Connected to {candidate_name} "
                            f"(api={api_version}, modalities={response_modalities}, "
                            f"trigger={trigger_tokens}, target={target_tokens})"
                        )
                        break
                    except Exception as exc:
                        last_exc = exc
                        self._connected = False
                        self._session = None
                        self._session_cm = None
                        logger.warning(
                            f"[GeminiLive] connect failed for model={candidate_name} "
                            f"api={api_version} modalities={response_modalities}: {exc}"
                        )
                if self._connected:
                    break
            if self._connected:
                break

        if not self._connected:
            logger.error(f"[GeminiLive] Connection failed: {last_exc}")
            raise last_exc or RuntimeError("Gemini Live connection failed")

        if self._frame_buffer is not None:
            self._frame_task = asyncio.create_task(self._stream_frames_task())

    async def _stream_frames_task(self):
        """Continuously stream video frames from PushFrameBuffer to the Live session."""
        from google.genai import types

        fps = self._gemini_cfg.get("frame_fps", 2)
        interval = 1.0 / max(fps, 0.5)
        logger.info(f"[GeminiLive] Frame streaming started at {fps} FPS")

        try:
            while self._connected and self._session:
                if self._frame_buffer is None:
                    await asyncio.sleep(interval)
                    continue

                frames = self._frame_buffer.get_frames(count=1, interval_ms=int(interval * 1000))
                if frames:
                    try:
                        jpeg_bytes = base64.b64decode(frames[0])
                        await self._session.send_realtime_input(
                            media=types.Blob(data=jpeg_bytes, mime_type="image/jpeg")
                        )
                    except Exception as exc:
                        if "closed" in str(exc).lower() or "disconnect" in str(exc).lower():
                            logger.warning("[GeminiLive] Session closed during frame send")
                            self._connected = False
                            break
                        logger.debug(f"[GeminiLive] Frame send error: {exc}")

                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        logger.info("[GeminiLive] Frame streaming stopped")

    async def send_text(self, text: str) -> str:
        """Send user text and return the model's response, handling tool calls."""
        if not self._connected or not self._session:
            return "Gemini Live session is not connected."

        from google.genai import types

        try:
            await self._session.send_client_content(
                turns=types.Content(
                    parts=[types.Part(text=text)],
                    role="user",
                ),
                turn_complete=True,
            )
            return await self._receive_turn()
        except Exception as exc:
            logger.error(f"[GeminiLive] send_text failed: {exc}")
            self._connected = False
            return "I lost connection. Please try again."

    async def send_audio(self, pcm_data: bytes, mime_type: str = "audio/pcm"):
        """Forward raw PCM audio to the Live session for Gemini STT."""
        if not self._connected or not self._session:
            return
        try:
            await self._session.send_realtime_input(
                audio={"data": pcm_data, "mime_type": mime_type}
            )
        except Exception as exc:
            logger.debug(f"[GeminiLive] Audio send error: {exc}")

    async def send_monitoring_prompt(self) -> Optional[Dict[str, Any]]:
        """Send a monitoring assessment prompt and parse the structured response."""
        if not self._connected or not self._session:
            return None

        try:
            raw = await self.send_text(MONITORING_PROMPT)
            if not raw:
                return None
            return _parse_monitoring_response(raw)
        except Exception as exc:
            logger.warning(f"[GeminiLive] Monitoring prompt failed: {exc}")
            return None

    async def update_protocol_context(self, context_text: str):
        """Send a protocol state update as a text message so the model tracks changes."""
        if not self._connected or not self._session:
            return
        from google.genai import types
        try:
            await self._session.send_client_content(
                turns=types.Content(
                    parts=[types.Part(text=f"[SYSTEM CONTEXT UPDATE]\n{context_text}")],
                    role="user",
                ),
                turn_complete=False,
            )
        except Exception as exc:
            logger.debug(f"[GeminiLive] Context update failed: {exc}")

    async def _receive_turn(self) -> str:
        """Receive a complete turn from the model, handling tool calls inline."""
        from google.genai import types

        response_text = ""
        try:
            turn = self._session.receive()
            async for response in turn:
                if hasattr(response, "text") and response.text:
                    response_text += response.text

                if hasattr(response, "tool_call") and response.tool_call:
                    tool_call = response.tool_call
                    function_calls = list(getattr(tool_call, "function_calls", []) or [])
                    for fc in function_calls:
                        fn_name = getattr(fc, "name", None)
                        fn_args = dict(getattr(fc, "args", {}) or {})
                        fn_id = getattr(fc, "id", None) or getattr(fc, "call_id", None)

                        if not fn_name:
                            continue

                        logger.info(f"[GeminiLive] Tool call: {fn_name}({fn_args})")
                        try:
                            result = await _dispatch_tool_call(fn_name, fn_args)
                        except Exception as exc:
                            result = f"Tool error: {exc}"
                            logger.error(f"[GeminiLive] Tool dispatch error: {exc}")

                        # Google Live API requires the original function call id.
                        response_payload = types.FunctionResponse(
                            name=fn_name,
                            response={"result": str(result)},
                        )
                        if fn_id:
                            response_payload.id = fn_id
                        else:
                            logger.warning(
                                f"[GeminiLive] Missing function call id for {fn_name}; "
                                "sending response without id."
                            )

                        await self._session.send_tool_response(
                            function_responses=[response_payload]
                        )
        except Exception as exc:
            logger.error(f"[GeminiLive] Receive error: {exc}")
            if "closed" in str(exc).lower():
                self._connected = False

        return response_text.strip()

    async def disconnect(self):
        """Gracefully close the session."""
        self._connected = False
        if self._frame_task and not self._frame_task.done():
            self._frame_task.cancel()
            try:
                await self._frame_task
            except asyncio.CancelledError:
                pass
        if self._session:
            try:
                if self._session_cm is not None:
                    await self._session_cm.__aexit__(None, None, None)
                else:
                    await self._session.close()
            except Exception:
                pass
            self._session = None
            self._session_cm = None
        logger.info("[GeminiLive] Session disconnected")

    @property
    def connected(self) -> bool:
        return self._connected


# ---------------------------------------------------------------------------
# Response parsing (simple -- no Qwen fallback needed)
# ---------------------------------------------------------------------------

def _parse_monitoring_response(raw: str) -> Dict[str, Any]:
    """Parse Gemini's structured monitoring response."""
    result: Dict[str, Any] = {"status": "same", "detail": raw, "error": None}

    status_match = re.search(
        r"STATUS:\s*(SAME|STEP_COMPLETE|ERROR)", raw, re.IGNORECASE
    )
    if status_match:
        result["status"] = status_match.group(1).lower()

    detail_match = re.search(r"DETAIL:\s*(.+)", raw, re.IGNORECASE)
    if detail_match:
        result["detail"] = detail_match.group(1).strip()

    error_match = re.search(r"ERROR:\s*(.+)", raw, re.IGNORECASE)
    if error_match:
        err_text = error_match.group(1).strip()
        if err_text.lower() not in ("", "n/a", "none", "blank", "-", "no", "null"):
            result["error"] = err_text

    return result


# ---------------------------------------------------------------------------
# GeminiLiveProvider
# ---------------------------------------------------------------------------

class GeminiLiveProvider(VSOPProvider):
    """VSOP provider backed by a Gemini Live API session.

    Streams video continuously, runs periodic monitoring, and (in ``full``
    mode) handles user chat through the same session.
    """

    _STALE_PUSH_INTERVAL = 45.0

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._gemini_cfg = config.get("gemini_live", {})
        self._session: Optional[GeminiLiveSession] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._polling_interval = self._gemini_cfg.get("monitoring_interval", 10)
        self._protocol_context: Optional[str] = None

        self._last_pushed_detail = ""
        self._last_push_time = 0.0
        self._step_complete_announced = False
        self._reconnect_delay = 1.0
        self._MAX_RECONNECT_DELAY = 30.0

    # -- lifecycle -----------------------------------------------------------

    async def start(
        self,
        protocol_name: Optional[str] = None,
        protocol_steps: Optional[List[str]] = None,
        protocol_context: Optional[str] = None,
    ) -> str:
        self._protocol_name = protocol_name
        self._steps = list(protocol_steps or [])
        self._current_step = 1
        self._completed_steps = []
        self._active = True
        self._protocol_context = protocol_context or ""
        self._step_complete_announced = False
        ok = await self._ensure_session_connected()
        if not ok:
            self._active = False
            return "Failed to connect to Gemini Live."

        initial_context = self._build_protocol_context_message()
        if initial_context:
            await self._session.update_protocol_context(initial_context)

        self._monitor_task = asyncio.create_task(self._monitor_loop())

        step_text = self._steps[0] if self._steps else "Begin protocol"
        await self._emit(StepEvent(
            step_num=1,
            total_steps=len(self._steps),
            state=StepState.STARTED,
            step_text=step_text,
            message=f"Step 1: {step_text}",
        ))

        return f"Started protocol: {protocol_name}"

    async def stop(self) -> str:
        self._active = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.disconnect()
            self._session = None
        self._protocol_name = None
        self._steps = []
        return "Protocol stopped."

    async def get_status(self) -> Dict[str, Any]:
        return {
            "active": self._active,
            "provider": "gemini_live",
            "protocol_name": self._protocol_name,
            "current_step": self._current_step,
            "total_steps": len(self._steps),
            "connected": self._session.connected if self._session else False,
        }

    async def get_current_step(self) -> str:
        if not self._active:
            return "No active protocol."
        idx = self._current_step - 1
        if 0 <= idx < len(self._steps):
            return f"Step {self._current_step}: {self._steps[idx]}"
        return f"Step {self._current_step}"

    # -- ad-hoc query --------------------------------------------------------

    async def query(self, question: str, frames: Optional[List[str]] = None) -> str:
        if not self._session or not self._session.connected:
            return "Gemini Live session is not connected."
        return await self._session.send_text(question)

    async def query_standalone(self, question: str) -> str:
        return await self.query(question)

    # -- user message handling (full mode) -----------------------------------

    async def handle_user_message(self, text: str) -> str:
        """Route a user message through the Gemini Live session (full mode)."""
        ok = await self._ensure_session_connected()
        if not ok:
            return "Gemini Live session is not connected. Please try again."
        return await self._session.send_text(text)

    # -- monitoring loop -----------------------------------------------------

    async def _monitor_loop(self):
        """Periodic monitoring + proactive error detection."""
        logger.info(f"[GeminiLive] Monitor loop started (interval={self._polling_interval}s)")
        try:
            while self._active:
                await asyncio.sleep(self._polling_interval)
                if not self._active:
                    break
                if not self._session or not self._session.connected:
                    await self._try_reconnect()
                    continue
                try:
                    await self._poll_once()
                except Exception as exc:
                    logger.error(f"[GeminiLive] Poll error: {exc}")
        except asyncio.CancelledError:
            pass
        logger.info("[GeminiLive] Monitor loop ended")

    async def _poll_once(self):
        """Run one monitoring assessment cycle."""
        parsed = await self._session.send_monitoring_prompt()
        if not parsed:
            return

        detail = parsed.get("detail", "")
        status = parsed.get("status", "same")

        try:
            from tools.protocols.state import get_protocol_state
            state = get_protocol_state(self.get_bound_session_id())
            if state.is_active and detail:
                old_vision = state.stella_vision_text
                state.stella_vision_text = detail
                now = time.monotonic()
                is_new = (detail != old_vision)
                is_stale = (now - self._last_push_time) >= self._STALE_PUSH_INTERVAL

                if is_new or is_stale:
                    self._last_push_time = now
                    from tools.display import ui as viture_ui
                    await viture_ui.render_step_panel(state)
                    try:
                        from config import _current_session_id
                        from labos_live_client import get_labos_client
                        lc = get_labos_client(_current_session_id.get("default-xr-session"))
                        if lc and lc.connected:
                            await lc.send_monitoring(detail)
                    except Exception:
                        pass

                if detail:
                    state.monitoring_granular.append(detail)
                    if len(state.monitoring_granular) > 60:
                        state.monitoring_granular = state.monitoring_granular[-60:]
        except Exception:
            pass

        await self._handle_monitoring_response(parsed)

    async def _handle_monitoring_response(self, parsed: Dict[str, Any]):
        """Emit StepEvents based on the monitoring assessment."""
        status = parsed["status"]
        detail = parsed.get("detail", "")
        error_msg = parsed.get("error")

        if status == "error" and error_msg:
            from tools.vsop_providers import is_non_protocol_error
            if is_non_protocol_error(error_msg):
                logger.debug(
                    f"[GeminiLive] ERROR suppressed (non-protocol: distraction/idle) "
                    f"step={self._current_step}: {error_msg}"
                )
                return

            from tools.protocols.state import get_protocol_state
            state = get_protocol_state(self.get_bound_session_id())
            if state.is_active and not state.is_error_on_cooldown():
                idx = self._current_step - 1
                step_text = self._steps[idx] if idx < len(self._steps) else ""
                await self._emit(StepEvent(
                    step_num=self._current_step,
                    total_steps=len(self._steps),
                    state=StepState.ERROR,
                    step_text=step_text,
                    message=f"Error on step {self._current_step}",
                    error_detail=error_msg,
                ))

        elif status == "step_complete" and not self._step_complete_announced:
            self._step_complete_announced = True
            try:
                from tools.protocols.state import get_protocol_state
                state = get_protocol_state(self.get_bound_session_id())
                if state.is_active:
                    state.stella_vision_text = "Step appears complete. Say 'next step' to continue."
                    from tools.display import ui as viture_ui
                    await viture_ui.render_step_panel(state)
                    from tools.display.tts import push_tts
                    await push_tts(f"Step {self._current_step} looks done. Say next step to continue.")
            except Exception as exc:
                logger.debug(f"[GeminiLive] Step complete notification error: {exc}")

    # -- reconnection --------------------------------------------------------

    async def _try_reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        if not self._active:
            return

        logger.info(f"[GeminiLive] Attempting reconnect (delay={self._reconnect_delay:.1f}s)")
        await asyncio.sleep(self._reconnect_delay)

        if not self._active:
            return

        system_instruction = self._build_system_instruction()
        from ws_handler import get_frame_buffer
        from config import _current_session_id
        sid = _current_session_id.get("default-xr-session")
        frame_buffer = get_frame_buffer(sid)

        try:
            self._session = GeminiLiveSession(self._config)
            await self._session.connect(system_instruction, frame_buffer)
            context_msg = self._build_protocol_context_message()
            if context_msg:
                await self._session.update_protocol_context(context_msg)
            self._reconnect_delay = 1.0
            logger.info("[GeminiLive] Reconnected successfully")
        except Exception as exc:
            logger.warning(f"[GeminiLive] Reconnect failed: {exc}")
            self._reconnect_delay = min(self._reconnect_delay * 2, self._MAX_RECONNECT_DELAY)

    # -- step navigation overrides -------------------------------------------

    async def manual_advance(self) -> str:
        self._step_complete_announced = False
        result = await super().manual_advance()
        if self._session and self._session.connected:
            ctx = self._build_protocol_context_message()
            if ctx:
                await self._session.update_protocol_context(ctx)
        return result

    async def manual_retreat(self) -> str:
        self._step_complete_announced = False
        result = await super().manual_retreat()
        if self._session and self._session.connected:
            ctx = self._build_protocol_context_message()
            if ctx:
                await self._session.update_protocol_context(ctx)
        return result

    async def manual_goto(self, step_num: int) -> str:
        self._step_complete_announced = False
        result = await super().manual_goto(step_num)
        if self._session and self._session.connected:
            ctx = self._build_protocol_context_message()
            if ctx:
                await self._session.update_protocol_context(ctx)
        return result

    # -- prompt building -----------------------------------------------------

    def _build_system_instruction(self) -> str:
        """Build the static system instruction for the Gemini Live session."""
        from context.manager import _load_mode_template
        try:
            template = _load_mode_template("protocol_running_gemini")
        except Exception:
            template = _load_mode_template("protocol_running")
        return template

    async def _ensure_session_connected(self) -> bool:
        """Create/connect a Live session on demand and reuse it across commands."""
        if self._session and self._session.connected:
            return True

        system_instruction = self._build_system_instruction()
        from ws_handler import get_frame_buffer
        from config import _current_session_id
        sid = _current_session_id.get("default-xr-session")
        frame_buffer = get_frame_buffer(sid)

        self._session = GeminiLiveSession(self._config)
        try:
            await self._session.connect(system_instruction, frame_buffer)
            if self._active:
                context_msg = self._build_protocol_context_message()
                if context_msg:
                    await self._session.update_protocol_context(context_msg)
            return True
        except Exception as exc:
            logger.error(f"[GeminiLive] Failed to connect session: {exc}")
            self._session = None
            return False

    def _build_protocol_context_message(self) -> str:
        """Build a protocol state update message including errors and data."""
        if not self._active or not self._steps:
            return ""
        from context.manager import build_all_steps_block
        from tools.protocols.state import get_protocol_state

        state = get_protocol_state(self.get_bound_session_id())
        all_steps = build_all_steps_block(
            self._steps, self._current_step, self._completed_steps
        )
        idx = self._current_step - 1
        step_text = self._steps[idx] if 0 <= idx < len(self._steps) else ""

        parts = [
            f"Protocol: {self._protocol_name} | Step {self._current_step}/{len(self._steps)}",
            "",
            all_steps,
            "",
            f"Current step: {step_text}",
        ]
        if self._protocol_context:
            parts.append(f"\nContext: {self._protocol_context}")

        if state.error_history:
            parts.append("\nErrors so far:")
            for e in state.error_history:
                parts.append(f"  Step {e.get('step', '?')}: {e.get('detail', 'unknown')}")

        data = state.experiment_data.get("sections", {})
        if data:
            parts.append("\nLogged observations/data:")
            for sec_name, sec_data in data.items():
                for r in sec_data.get("rows", []):
                    parts.append(
                        f"  [{sec_name}] Step {r.get('_step','?')} "
                        f"({r.get('_timestamp','?')}): {r.get('note', r)}"
                    )

        return "\n".join(parts)
