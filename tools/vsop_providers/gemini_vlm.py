"""Gemini VLM VSOP Provider (polling-based).

Uses standard ``google.genai`` ``generate_content`` calls to monitor
laboratory protocols and handle user chat -- NO persistent Live API
WebSocket.  Each monitoring poll and chat message is an independent API
call with frames sampled from the ``PushFrameBuffer`` at call time.

Two modes:
  - **full** -- Gemini handles user chat + video monitoring.
  - **vision_only** -- Gemini handles video monitoring; Agents SDK handles chat.
"""

import asyncio
import base64
import collections
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from tools.vsop_providers import StepEvent, StepState, VSOPProvider
from context.manager import build_windowed_steps_block


# ---------------------------------------------------------------------------
# Tool declarations (standard generate_content format)
# ---------------------------------------------------------------------------

def _build_tool_declarations():
    """Build Gemini FunctionDeclaration list for protocol tools.

    Lazy so ``google.genai`` is only imported when the provider is active.
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
    ]


# ---------------------------------------------------------------------------
# Tool dispatcher (identical to gemini_live.py -- no Live API dependency)
# ---------------------------------------------------------------------------

async def _dispatch_tool_call(name: str, args: dict) -> str:
    """Execute a tool by name and return the string result."""
    from tools.vsop_providers import get_vsop_provider

    provider = get_vsop_provider()
    sid = provider.get_bound_session_id() if provider else None

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

    if name == "list_protocols":
        from tools.protocols.store import get_protocol_store
        from tools.display.ui import render_protocol_list
        from tools.protocols.state import get_protocol_state
        from context.manager import get_context_manager
        store = get_protocol_store()
        protocols = store.list_protocols()
        if not protocols:
            return "No protocols are currently available in the database."
        await render_protocol_list(store)
        state = get_protocol_state(sid)
        state.mode = "listing"
        get_context_manager().set_context("protocol_listing")
        names = ", ".join(p.get("pretty_name", p.get("name", "unknown")) for p in protocols)
        return (
            f"Here are the available protocols: {names}. "
            "Say the number or name of the protocol you'd like to run."
        )

    if name == "start_protocol":
        from tools.protocols.store import get_protocol_store
        from tools.protocols.state import get_protocol_state, StepDetail
        from context.manager import get_context_manager
        from tools.display import ui as viture_ui

        store = get_protocol_store()
        proto_name = args.get("protocol_name", "")
        proto = store.find_protocol(proto_name)
        if not proto:
            try:
                idx = int(proto_name)
                protocols = store.list_protocols()
                if 1 <= idx <= len(protocols):
                    proto = protocols[idx - 1]
            except (ValueError, TypeError):
                pass
        if proto:
            state = get_protocol_state(sid)
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

    if name == "log_observation":
        from tools.protocols.state import get_protocol_state
        from datetime import datetime
        state = get_protocol_state(sid)
        observation = args.get("observation", "")
        section = args.get("section", "notes")
        if state.is_active and observation:
            if "sections" not in state.experiment_data:
                state.experiment_data["sections"] = {}
            sec = state.experiment_data["sections"].setdefault(section, {"rows": []})
            sec["rows"].append({
                "note": observation,
                "_step": str(state.current_step),
                "_timestamp": datetime.utcnow().strftime("%H:%M:%S"),
            })
            return f"Logged to '{section}': {observation}"
        return "No active protocol to log data."

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

    logger.warning(f"[GeminiVLM] Unknown tool call: {name}")
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

MONITORING_PROMPT = """\
You are STELLA, a lab protocol monitor. Your ONLY job: detect errors and verify step completion. You do NOT advance the protocol.

Protocol: {protocol_name} ({total_steps} steps)
{all_steps_block}

Current step ({current_step_num}/{total_steps}): {current_step_text}
Description: {step_description}
Context: {protocol_context_block}

{recent_context}

{frame_count} frames from last {window_secs}s (oldest first). Final frames = current state.

Reply EXACTLY (3 lines, no extra text):
STATUS: <SAME|STEP_COMPLETE|ERROR>
DETAIL: <2 sentences max: what you see>
ERROR: <if ERROR, describe mistake. Otherwise: none>\
"""


# ---------------------------------------------------------------------------
# Response parsing
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
# GeminiVLMProvider
# ---------------------------------------------------------------------------

class GeminiVLMProvider(VSOPProvider):
    """Polling-based Gemini VSOP provider.

    Uses standard ``generate_content`` calls for monitoring and chat.
    Frames are sampled from the ``PushFrameBuffer`` per-request, not
    streamed via a persistent WebSocket.
    """

    _STALE_PUSH_INTERVAL = 45.0
    _MAX_HISTORY_TURNS = 30
    _MAX_TOOL_ROUNDS = 8

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._gemini_cfg = config.get("gemini_custom_manage", {})
        self._client = None
        self._model: str = self._gemini_cfg.get("model", "gemini-3.1-flash-lite-preview")
        self._monitor_task: Optional[asyncio.Task] = None
        self._polling_interval = self._gemini_cfg.get("monitoring_interval", 10)
        self._monitoring_frames = self._gemini_cfg.get("monitoring_frames", 20)
        self._monitoring_window_s = self._gemini_cfg.get("monitoring_window_seconds", 20)
        self._chat_frames = self._gemini_cfg.get("chat_frames", 5)
        self._protocol_context: Optional[str] = None

        self._conversation_history: List[Dict[str, Any]] = []

        self._last_pushed_detail = ""
        self._last_push_time = 0.0
        self._step_complete_announced = False

        self._granular_observations: collections.deque[Tuple[float, str]] = collections.deque(maxlen=24)
        self._medium_observations: collections.deque[Tuple[float, str]] = collections.deque(maxlen=5)
        self._high_observations: List[Tuple[float, str]] = []
        self._polls_since_medium_summary: int = 0
        self._medium_since_high_summary: int = 0

    # -- client init ---------------------------------------------------------

    def _ensure_client(self):
        if self._client is not None:
            return
        from google import genai
        api_key = self._gemini_cfg.get("api_key", "")
        from config import _resolve_env_vars
        api_key = _resolve_env_vars(api_key)
        self._client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1beta"},
        )
        logger.info(f"[GeminiVLM] Client initialized (model={self._model})")

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
        self._conversation_history.clear()
        self._granular_observations.clear()
        self._medium_observations.clear()
        self._high_observations.clear()
        self._polls_since_medium_summary = 0
        self._medium_since_high_summary = 0
        self._last_pushed_detail = ""
        self._last_push_time = 0.0

        self._ensure_client()
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
        self._monitor_task = None
        self._conversation_history.clear()
        self._protocol_name = None
        self._steps = []
        return "Protocol stopped."

    async def get_status(self) -> Dict[str, Any]:
        return {
            "active": self._active,
            "provider": "gemini_custom_manage",
            "protocol_name": self._protocol_name,
            "current_step": self._current_step,
            "total_steps": len(self._steps),
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
        self._ensure_client()
        if frames is None:
            frames = self._grab_frames(self._chat_frames)
        parts = self._build_content_parts(question, frames)
        try:
            resp = await self._generate(parts)
            return resp or "Could not get a response."
        except Exception as exc:
            logger.error(f"[GeminiVLM] query failed: {exc}")
            return "I couldn't analyze that right now. Please try again."

    async def query_standalone(self, question: str) -> str:
        return await self.query(question)

    # -- user message handling (full mode) -----------------------------------

    async def handle_user_message(self, text: str) -> str:
        """Process a user message with conversation history, frames, and tools."""
        self._ensure_client()
        from google.genai import types

        self._conversation_history.append({"role": "user", "text": text})

        system_instruction = self._build_system_instruction()
        protocol_ctx = self._build_protocol_context_message()

        frames = self._grab_frames(self._chat_frames)
        user_parts = self._build_content_parts(text, frames)

        contents = []
        contents.append(types.Content(
            parts=[types.Part(text=f"{system_instruction}\n\n{protocol_ctx}")],
            role="user",
        ))
        contents.append(types.Content(
            parts=[types.Part(text="Understood. I'm ready to assist with the protocol.")],
            role="model",
        ))

        for msg in self._conversation_history[:-1]:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(
                parts=[types.Part(text=msg["text"])],
                role=role,
            ))

        contents.append(types.Content(parts=user_parts, role="user"))

        tool_decls = _build_tool_declarations()
        tools = [types.Tool(function_declarations=tool_decls)]

        try:
            response_text = await self._generate_with_tools(contents, tools)
        except Exception as exc:
            logger.error(f"[GeminiVLM] handle_user_message failed: {exc}")
            response_text = "Sorry, something went wrong. Please try again."

        self._conversation_history.append({"role": "assistant", "text": response_text})
        if len(self._conversation_history) > self._MAX_HISTORY_TURNS * 2:
            self._conversation_history = self._conversation_history[-(self._MAX_HISTORY_TURNS * 2):]

        return response_text

    # -- monitoring loop -----------------------------------------------------

    async def _monitor_loop(self):
        logger.info(f"[GeminiVLM] Monitor loop started (interval={self._polling_interval}s)")
        try:
            while self._active:
                await asyncio.sleep(self._polling_interval)
                if not self._active:
                    break
                try:
                    await self._poll_once()
                except Exception as exc:
                    logger.error(f"[GeminiVLM] Poll error: {exc}")
        except asyncio.CancelledError:
            pass
        logger.info("[GeminiVLM] Monitor loop ended")

    async def _poll_once(self):
        if not self._active:
            return

        self._ensure_client()
        interval_ms = int(self._monitoring_window_s * 1000 / max(self._monitoring_frames - 1, 1))
        frames = self._grab_frames(self._monitoring_frames, interval_ms)

        if not frames:
            logger.warning("[GeminiVLM] Monitor: no frames available")
            try:
                from tools.protocols.state import get_protocol_state
                state = get_protocol_state(self.get_bound_session_id())
                if state.is_active:
                    state.stella_vision_text = "Camera stream unavailable"
                    from tools.display import ui as viture_ui
                    await viture_ui.render_step_panel(state, session_id=self.get_bound_session_id())
            except Exception:
                pass
            return

        idx = self._current_step - 1
        if idx >= len(self._steps):
            return

        all_steps = build_windowed_steps_block(
            self._steps, self._current_step, self._completed_steps, window=3,
        )

        step_description = ""
        try:
            from tools.protocols.state import get_protocol_state
            state = get_protocol_state(self.get_bound_session_id())
            if state.is_active and 0 <= idx < len(state.steps):
                sd = state.steps[idx]
                if sd.description:
                    step_description = sd.description
        except Exception:
            pass

        ctx_parts: list[str] = []
        if self._high_observations:
            ctx_parts.append(f"Long-term: {self._high_observations[-1][1]}")
        if self._medium_observations:
            ctx_parts.append(f"Recent: {self._medium_observations[-1][1]}")
        recent_context = ""
        if ctx_parts:
            recent_context = (
                "<previous_two_minutes_context>\n"
                + "\n".join(ctx_parts)
                + "\n</previous_two_minutes_context>"
            )

        prompt = MONITORING_PROMPT.format(
            protocol_name=self._protocol_name,
            total_steps=len(self._steps),
            all_steps_block=all_steps,
            protocol_context_block=self._protocol_context or "No additional protocol notes provided.",
            current_step_num=self._current_step,
            current_step_text=self._steps[idx],
            step_description=step_description or "No additional description available.",
            frame_count=len(frames),
            window_secs=self._monitoring_window_s,
            recent_context=recent_context,
        )

        parts = self._build_content_parts(prompt, frames)
        raw = await self._generate(parts)
        if not raw:
            return

        parsed = _parse_monitoring_response(raw)
        detail = parsed.get("detail", "")

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
                    await viture_ui.render_step_panel(state, session_id=self.get_bound_session_id())
                    try:
                        from labos_live_client import get_labos_client
                        lc = get_labos_client(self.get_bound_session_id())
                        if lc and lc.connected:
                            await lc.send_monitoring(detail)
                    except Exception:
                        pass
        except Exception:
            pass

        await self._handle_monitoring_response(parsed)

        if detail:
            self._granular_observations.append((time.monotonic(), detail))

        self._polls_since_medium_summary += 1
        if self._polls_since_medium_summary >= 24:
            self._polls_since_medium_summary = 0
            try:
                summary = await self._summarize_granular()
                if summary:
                    self._medium_observations.append((time.monotonic(), summary))
                    self._medium_since_high_summary += 1
            except Exception as exc:
                logger.warning(f"[GeminiVLM] Granular summary failed: {exc}")

            if self._medium_since_high_summary >= 5:
                self._medium_since_high_summary = 0
                try:
                    high_summary = await self._summarize_medium()
                    if high_summary:
                        self._high_observations.append((time.monotonic(), high_summary))
                except Exception as exc:
                    logger.warning(f"[GeminiVLM] Medium summary failed: {exc}")

        try:
            from tools.protocols.state import get_protocol_state
            pstate = get_protocol_state(self.get_bound_session_id())
            pstate.monitoring_granular = [obs for _, obs in self._granular_observations]
            pstate.monitoring_medium = [obs for _, obs in self._medium_observations]
            pstate.monitoring_high = [obs for _, obs in self._high_observations]
        except Exception:
            pass

    async def _handle_monitoring_response(self, parsed: Dict[str, Any]):
        status = parsed["status"]
        error_msg = parsed.get("error")

        if status == "error" and error_msg:
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
                    await viture_ui.render_step_panel(state, session_id=self.get_bound_session_id())
                    from tools.display.tts import push_tts
                    await push_tts(
                        f"Step {self._current_step} looks done. Say next step to continue.",
                        session_id=self.get_bound_session_id(),
                    )
            except Exception as exc:
                logger.debug(f"[GeminiVLM] Step complete notification error: {exc}")

    # -- hierarchical memory -------------------------------------------------

    async def _summarize_granular(self) -> str:
        entries = [obs for _, obs in self._granular_observations]
        if not entries:
            return ""
        self._ensure_client()
        step_text = self._steps[self._current_step - 1] if self._steps else "N/A"
        prompt = (
            f"Summarize these lab observations from the last 2 minutes into 1-2 sentences.\n"
            f"Protocol: {self._protocol_name}, Step {self._current_step}: {step_text}\n\n"
            + "\n".join(f"- {e}" for e in entries)
        )
        text = await self._generate([self._text_part(prompt)])
        return text.strip() if text else ""

    async def _summarize_medium(self) -> str:
        entries = [obs for _, obs in self._medium_observations]
        if not entries:
            return ""
        self._ensure_client()
        step_text = self._steps[self._current_step - 1] if self._steps else "N/A"
        prompt = (
            f"Summarize these lab monitoring summaries from the last ~10 minutes "
            f"into 3-4 concise bullet points.\n"
            f"Protocol: {self._protocol_name}, Current step {self._current_step}: {step_text}\n\n"
            + "\n".join(f"- {e}" for e in entries)
        )
        text = await self._generate([self._text_part(prompt)])
        return text.strip() if text else ""

    # -- step navigation overrides -------------------------------------------

    async def manual_advance(self) -> str:
        self._step_complete_announced = False
        self._last_pushed_detail = ""
        return await super().manual_advance()

    async def manual_retreat(self) -> str:
        self._step_complete_announced = False
        self._last_pushed_detail = ""
        return await super().manual_retreat()

    async def manual_goto(self, step_num: int) -> str:
        self._step_complete_announced = False
        self._last_pushed_detail = ""
        return await super().manual_goto(step_num)

    # -- prompt building -----------------------------------------------------

    def _build_system_instruction(self) -> str:
        from context.manager import _load_mode_template
        try:
            template = _load_mode_template("protocol_running_gemini")
        except Exception:
            template = _load_mode_template("protocol_running")
        return template

    def _build_protocol_context_message(self) -> str:
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
            "", all_steps, "",
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

    # -- Gemini API helpers --------------------------------------------------

    @staticmethod
    def _text_part(text: str):
        from google.genai import types
        return types.Part(text=text)

    @staticmethod
    def _image_part(frame_b64: str):
        from google.genai import types
        jpeg_bytes = base64.b64decode(frame_b64)
        return types.Part(inline_data=types.Blob(data=jpeg_bytes, mime_type="image/jpeg"))

    def _build_content_parts(self, text: str, frames: List[str]) -> list:
        parts = [self._text_part(text)]
        for f in frames:
            parts.append(self._image_part(f))
        return parts

    def _grab_frames(self, count: int, interval_ms: int = 1000) -> List[str]:
        from ws_handler import get_frame_buffer
        sid = self.get_bound_session_id()
        buf = get_frame_buffer(sid)
        if buf is None:
            return []
        return buf.get_frames(count=count, interval_ms=interval_ms)

    async def _generate(self, parts: list) -> Optional[str]:
        from google.genai import types
        try:
            resp = await self._client.aio.models.generate_content(
                model=self._model,
                contents=types.Content(parts=parts, role="user"),
                config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=512),
            )
            return resp.text.strip() if resp.text else ""
        except Exception as exc:
            logger.error(f"[GeminiVLM] generate_content failed: {exc}")
            return None

    async def _generate_with_tools(self, contents: list, tools: list) -> str:
        from google.genai import types

        for _ in range(self._MAX_TOOL_ROUNDS):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0.5,
                        max_output_tokens=1024,
                        tools=tools,
                    ),
                )
            except Exception as exc:
                logger.error(f"[GeminiVLM] generate_with_tools failed: {exc}")
                return "Sorry, something went wrong. Please try again."

            if not resp.candidates:
                return resp.text.strip() if resp.text else "No response from model."

            candidate = resp.candidates[0]
            has_function_call = False
            text_parts = []

            for part in candidate.content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    has_function_call = True
                    fc = part.function_call
                    fn_name = fc.name
                    fn_args = dict(fc.args) if fc.args else {}

                    logger.info(f"[GeminiVLM] Tool call: {fn_name}({fn_args})")
                    try:
                        result = await _dispatch_tool_call(fn_name, fn_args)
                    except Exception as exc:
                        result = f"Tool error: {exc}"
                        logger.error(f"[GeminiVLM] Tool dispatch error: {exc}")

                    contents.append(candidate.content)
                    contents.append(types.Content(
                        parts=[types.Part(function_response=types.FunctionResponse(
                            name=fn_name,
                            response={"result": str(result)},
                        ))],
                        role="function",
                    ))
                elif hasattr(part, "text") and part.text:
                    text_parts.append(part.text)

            if not has_function_call:
                return " ".join(text_parts).strip() or "Done."

        return " ".join(text_parts).strip() if text_parts else "Action completed."
