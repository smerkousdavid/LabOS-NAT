"""STELLA-VLM VSOP Provider.

Uses the STELLA-VLM (Qwen fine-tune) served via vLLM to monitor
laboratory protocols in real-time.  Supports two modes:

  - **Database mode** -- a known protocol is loaded from ProtocolStore
    and provided as context to STELLA for step tracking.
  - **Generate mode** -- STELLA extracts the protocol from the live video
    feed, then monitors it.

Frame capture goes through the runtime-connector ``/frames`` (multi-frame)
or ``/frame`` (single) HTTP endpoints.
"""

import asyncio
import base64
import json
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from tools.vsop_providers import StepEvent, StepState, VSOPProvider
from context.manager import build_all_steps_block
from frame_source import create_frame_source

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

MONITORING_PROMPT = """\
You are STELLA, a vision-language model specialized in laboratory protocol analysis.
You are monitoring a live procedure through AR glasses and determining step transitions for this protocol.

<task>
Analyze the provided video frame(s) and determine the STATUS of the current step.
Your ONLY job is state-transition detection. Do not provide general commentary.
</task>

<protocol_context>
Protocol: {protocol_name}
Total steps: {total_steps}

{all_steps_block}
</protocol_context>

<protocol_notes>
{protocol_context_block}
</protocol_notes>

<current_step_detail>
Current step ({current_step_num}/{total_steps}): {current_step_text}

<step_description>
{step_description}
</step_description>

<common_errors>
Known mistakes for this step:
{common_errors_block}
Only report ERROR if you see one of these specific issues, a very obvious mistake, or a clear safety violation.
DO NOT THROW OUT AN ERROR when the user is: walking off to pick up another reagent, chatting with someone else, writing something down, or texting on the phone.
</common_errors>
</current_step_detail>

<transition_criteria>
You must classify the current state as EXACTLY one of:

STATUS: SAME
  The user is still working on the current step [>>>]. The frames show activity
  consistent with "{current_step_text}". No transition has occurred.

STATUS: ADVANCED
  The user has COMPLETED the current step [>>>] and has moved on to the next step.
  You see clear evidence that "{current_step_text}" is finished AND the user has
  begun or is preparing for the next step. Do NOT report ADVANCED unless you see
  concrete visual evidence of completion.

STATUS: ERROR
  The user is making a MISTAKE on the current step [>>>]. You see something
  inconsistent with the correct procedure for "{current_step_text}". Examples:
  wrong reagent, wrong equipment, wrong container, skipped sub-step, incorrect
  technique, safety violation.

STATUS: COMPLETED
  ALL steps in the protocol are finished. The final step has been completed and
  no further action is needed.
</transition_criteria>

<visual_context>
These {frame_count} frames are sampled from the last {window_secs} seconds of the
procedure, ordered chronologically (oldest first). The LATER frames (especially the
last 2-3) represent the CURRENT state and should carry the most weight in your
assessment. Earlier frames provide context for how the user got there.

CRITICAL: If the FINAL few frames show the step outcome is achieved (e.g., object is
in hand, item is placed, reagent is added, equipment is positioned), report ADVANCED
even if earlier frames show the action still in progress. The most recent frame is
the ground truth of the current state. Do NOT report SAME just because earlier frames
show an incomplete action -- always defer to what the latest frames show.
</visual_context>

<output_format>
Reply in EXACTLY this format. Three lines, no extra text:
STATUS: <SAME|ADVANCED|ERROR|COMPLETED>
DETAIL: <1-sentence observation of what you see in the frames>
ERROR: <if STATUS is ERROR, describe the specific mistake. Otherwise write: none>
</output_format>

<examples>
Example 1 -- Step is "Add 5 mL PBS to wash cells":
  Frames show: user holding pipette over flask, liquid being dispensed
  STATUS: SAME
  DETAIL: User is actively pipetting liquid into the flask, consistent with adding PBS wash.
  ERROR: none

Example 2 -- Step is "Add 5 mL PBS to wash cells":
  Frames show: user has set down pipette, picking up next reagent bottle
  STATUS: ADVANCED
  DETAIL: PBS wash appears complete, user has moved on to prepare the next reagent.
  ERROR: none

Example 3 -- Step is "Add 5 mL PBS to wash cells":
  Frames show: user adding liquid from a red-labeled bottle instead of PBS
  STATUS: ERROR
  DETAIL: User appears to be adding liquid from incorrect bottle (red label, not PBS).
  ERROR: Wrong reagent used. Expected PBS buffer but user is dispensing from a different bottle.

Example 4 -- Final step "Place in thermal cycler":
  Frames show: tube placed in cycler, lid closed, user stepping back
  STATUS: COMPLETED
  DETAIL: Tube is in the thermal cycler with lid closed. Protocol complete.
  ERROR: none
</examples>\
"""

GENERATE_PROTOCOL_PROMPT = """\
Analyze this laboratory video and extract the complete experimental protocol.
Provide a detailed step-by-step protocol including:
1. List all reagents, chemicals, and materials used with exact volumes/quantities
2. Describe each procedural step in order
3. Specify all incubation times, temperatures, and speeds
4. Identify all equipment and instruments used
5. Note critical observations and quality control checkpoints
6. Include any safety precautions observed
Format the protocol as numbered steps. Begin your response directly with Step 1:\
"""

ADHOC_QUESTION_PROMPT = """\
You are STELLA, a vision-language model specialized in laboratory protocols. You are
the domain expert with real-time camera access. A user is performing a protocol and
has a question.

<protocol_context>
Protocol: {protocol_name}
Total steps: {total_steps}

{all_steps_block}
</protocol_context>

<protocol_notes>
{protocol_context_block}
</protocol_notes>

<user_question>
{question}
</user_question>

<instructions>
Answer the user's question using:
1. Your domain expertise in biological and chemical laboratory procedures
2. What you observe in the provided frame(s) from the AR glasses camera
3. The protocol context above (which step they're on, what's been done, what's ahead)

Be concise and directly helpful. 2-3 sentences maximum. Your answer will be spoken
aloud to the user. Do not use special characters or formatting.
If you see something concerning in the frames (safety issue, wrong setup), mention it
even if the user didn't ask about it.
</instructions>\
"""

STANDALONE_QUESTION_PROMPT = """\
You are STELLA, a vision-language model with access to a live camera feed from
AR glasses in a laboratory environment. The user has a question about what they
are looking at or their environment. No protocol is currently active.

<user_question>
{question}
</user_question>

Answer based on what you observe in the provided frame(s). Be concise and
directly helpful. 2-3 sentences maximum. Your answer will be spoken aloud to
the user. Do not use special characters or formatting.
If you see something concerning in the frames (safety issue, wrong setup),
mention it even if the user didn't ask about it.\
"""

STEP_DESCRIPTION_PROMPT = """\
You are STELLA, a laboratory protocol expert. The user is about to perform the
following step in their protocol.

Protocol: {protocol_name}
Step {current_num} of {total_steps}: {current_step_text}

In exactly 2 sentences, describe the specific technique and any important details
for performing this step correctly. Focus on practical tips the user needs right now.
Do not use special characters or formatting.\
"""

LLM_FALLBACK_PROMPT = """\
Given this STELLA VLM response about a lab protocol, extract structured info.

Protocol: {protocol_name}
Current step: Step {current_num}. {current_step_text}
STELLA said: "{raw_response}"

Reply with ONLY valid JSON:
{{"status":"same"|"advanced"|"error"|"completed","detail":"<observation>","error":"<description or null>"}}\
"""

# Fix C: single-frame verification when STELLA returns SAME with progress language
SINGLE_FRAME_VERIFY_PROMPT = """\
You are verifying whether a laboratory protocol step has been completed.

Step {current_num}: {current_step_text}

STELLA's multi-frame analysis said: "{stella_detail}"
However, this may be outdated because it was based on frames spanning several seconds.

Look at this SINGLE latest frame from the AR camera. This frame represents the
current state RIGHT NOW. Based ONLY on what you see in this frame:

- Has the step been completed? (Is the outcome achieved?)
- Or is the step still in progress?

Reply in EXACTLY this format (two lines, no extra text):
STATUS: <SAME|ADVANCED>
REASON: <one sentence explaining what you see in the current frame>\
"""

# Fix D: ask STELLA to describe each frame individually
DESCRIBE_FRAMES_PROMPT = """\
You are analyzing {frame_count} chronological video frames from an AR camera.
A user is performing a laboratory protocol.

Current step: {current_step_text}

Describe what you observe in EACH frame separately. Focus on:
- What the user's hands are doing
- What objects are being held, moved, or interacted with
- The position and state of key items relevant to the current step

Reply with one line per frame:
Frame 1: <description>
Frame 2: <description>
...
Frame {frame_count}: <description>\
"""

# Fix D: LLM reasons over per-frame descriptions
REASON_OVER_DESCRIPTIONS_PROMPT = """\
You are evaluating whether a protocol step has been completed based on
frame-by-frame visual descriptions from an AR camera.

Step: "{current_step_text}"

Frame descriptions (chronological, oldest to newest):
{frame_descriptions}

Based on the progression across these frames, has the step been completed?
The FINAL frames represent the current state -- prioritize them heavily.
If the last frame(s) show the step outcome is achieved, the step is ADVANCED.

Reply in EXACTLY this format (two lines, no extra text):
STATUS: <SAME|ADVANCED>
REASON: <one sentence explaining your reasoning>\
"""

# LLM quick-verify: always run on SAME responses to catch STELLA under-reporting
LLM_QUICK_VERIFY_PROMPT = """\
A vision model is monitoring a lab protocol step via AR glasses. It returned STATUS: SAME with this observation:

Step {current_num}/{total_steps}: "{current_step_text}"
Observation: "{stella_detail}"

Based ONLY on the observation text, decide:
- If the observation describes the step outcome as ACHIEVED (e.g. user has arrived, object is held, item is placed, action is done), reply ADVANCED.
- If the observation describes the step as genuinely still in progress or not yet started, reply SAME.
- If you cannot confidently decide, reply UNCERTAIN.

Reply in EXACTLY this format (two lines, no extra text):
STATUS: <SAME|ADVANCED|UNCERTAIN>
REASON: <one sentence>\
"""

# Native ring-buffer passthrough values in runtime_connector `/frames`.
# Using these avoids decode/re-encode in the API handler.
_RC_NATIVE_MAX_SIZE = 800
_RC_NATIVE_JPEG_QUALITY = 70


class StellaVSOPProvider(VSOPProvider):
    """STELLA-VLM based VSOP provider."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._config = config
        self._frame_source = None
        vsop_cfg = config.get("vsop_provider", {})
        stella_cfg = vsop_cfg.get("stella", {})
        multi_cfg = vsop_cfg.get("multi_frame", {})

        vlm_cfg = config.get("llms", {}).get("vlm", {})
        self._base_url = stella_cfg.get("base_url", vlm_cfg.get("base_url", "http://vlm:8500/v1"))
        self._model = stella_cfg.get("model", vlm_cfg.get("model", "Zaixi/STELLA-VLM-32b"))
        self._api_key = stella_cfg.get("api_key", vlm_cfg.get("api_key", "not-needed"))

        self._frame_mode = stella_cfg.get("frame_mode", "multi")
        self._frame_count = multi_cfg.get("count", stella_cfg.get("multi_frame_count", 3))
        self._window_secs = multi_cfg.get("window_seconds", stella_cfg.get("multi_frame_window_secs", 10.0))
        self._frame_resolution = multi_cfg.get("resolution", stella_cfg.get("frame_resolution", 384))
        self._jpeg_quality = multi_cfg.get("jpeg_quality", stella_cfg.get("jpeg_quality", 70))

        self._polling_interval = vsop_cfg.get("polling_interval", stella_cfg.get("polling_interval", 3.0))
        self._temperature = stella_cfg.get("temperature", 0.7)
        self._max_tokens = stella_cfg.get("max_tokens", 1024)
        self._top_p = stella_cfg.get("top_p", 0.95)

        self._llm_fallback_enabled = stella_cfg.get("llm_fallback", True)
        llm_cfg = config.get("llms", {}).get("router", {})
        self._llm_base_url = llm_cfg.get("base_url", "http://llm:8001/v1")
        self._llm_model = llm_cfg.get("model", "Qwen/Qwen2.5-7B-Instruct")
        self._llm_api_key = llm_cfg.get("api_key", "not-needed")
        self._protocol_context: str = ""

        self._monitor_task: Optional[asyncio.Task] = None

        self._last_observation: Optional[str] = None
        self._in_error_state: bool = False
        self._last_error_emit_time: float = 0.0
        self._ERROR_EMIT_COOLDOWN: float = 20.0
        self._POST_CLEAR_GRACE: float = 5.0
        self._ERROR_CONFIRM_POLLS: int = 2
        self._CLEAR_CONFIRM_POLLS: int = 2
        self._pending_error_count: int = 0
        self._pending_clear_count: int = 0

        self._stella_log = logger.bind(stella=True)
        try:
            from pathlib import Path
            log_dir = Path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            logger.add(
                str(log_dir / "stella_vsop.log"),
                filter=lambda record: record["extra"].get("stella"),
                rotation="10 MB",
                retention="3 days",
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
            )
        except Exception as exc:
            logger.warning(f"Could not set up STELLA log file: {exc}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        protocol_name: Optional[str] = None,
        protocol_steps: Optional[List[str]] = None,
        protocol_context: Optional[str] = None,
    ) -> str:
        if self._active:
            await self.stop()

        self._protocol_name = protocol_name or "Custom Protocol"
        self._protocol_context = (protocol_context or "").strip()
        self._current_step = 1
        self._completed_steps = []
        self._last_observation = None
        self._in_error_state = False
        self._last_error_emit_time = 0.0
        self._pending_error_count = 0
        self._pending_clear_count = 0

        if protocol_steps:
            self._steps = list(protocol_steps)
            logger.info(f"STELLA: database mode -- {len(self._steps)} steps for '{self._protocol_name}'")
        else:
            self._steps = await self._generate_protocol()
            logger.info(f"STELLA: generate mode -- extracted {len(self._steps)} steps")

        if not self._steps:
            return "Could not determine protocol steps. Please try again or describe the protocol."

        self._active = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        try:
            # Prime first observation immediately so STELLA text appears on panel.
            await self._poll_once()
        except Exception as exc:
            logger.warning(f"Initial STELLA poll failed: {exc}")

        await self._emit(StepEvent(
            step_num=1,
            total_steps=len(self._steps),
            state=StepState.STARTED,
            step_text=self._steps[0],
            message=f"Starting protocol '{self._protocol_name}' -- Step 1: {self._steps[0]}",
        ))

        return (
            f"Started monitoring protocol '{self._protocol_name}' "
            f"with {len(self._steps)} steps. Currently on step 1: {self._steps[0]}"
        )

    async def stop(self) -> str:
        if not self._active:
            return "No active protocol monitoring to stop."

        self._active = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._monitor_task = None
        self._last_observation = None
        self._in_error_state = False
        self._last_error_emit_time = 0.0
        self._pending_error_count = 0
        self._pending_clear_count = 0

        if self._frame_source is not None:
            await self._frame_source.close()
            self._frame_source = None

        name = self._protocol_name or "protocol"
        logger.info(f"STELLA: stopped monitoring '{name}'")
        return f"Stopped monitoring protocol '{name}'."

    async def get_status(self) -> Dict[str, Any]:
        return {
            "active": self._active,
            "provider": "stella",
            "protocol_name": self._protocol_name,
            "total_steps": len(self._steps),
            "current_step": self._current_step,
            "completed_steps": list(self._completed_steps),
        }

    async def get_current_step(self) -> str:
        if not self._active or not self._steps:
            return "No protocol is currently being monitored."
        idx = self._current_step - 1
        if idx < len(self._steps):
            return f"Step {self._current_step} of {len(self._steps)}: {self._steps[idx]}"
        return "All steps completed."

    # ------------------------------------------------------------------
    # Ad-hoc questions
    # ------------------------------------------------------------------

    async def query(self, question: str, frames: Optional[List[str]] = None) -> str:
        if frames is None:
            frames = await self._capture_frames()

        all_steps = build_all_steps_block(self._steps, self._current_step, self._completed_steps)
        prompt = ADHOC_QUESTION_PROMPT.format(
            protocol_name=self._protocol_name or "Unknown",
            total_steps=len(self._steps),
            all_steps_block=all_steps,
            protocol_context_block=self._protocol_context or "No additional protocol notes provided.",
            question=question,
        )

        response = await self._call_stella(prompt, frames)
        if not response or response.startswith("Error"):
            return "I couldn't analyze the current view. Please try again."
        return response

    async def query_standalone(self, question: str) -> str:
        """Answer a visual question without any protocol context.

        Uses 1-2 recent frames (not the full 8-frame window used for
        protocol monitoring) to stay within the VLM's context limit.
        """
        try:
            self._ensure_frame_source()
            frames = await self._frame_source.get_frames(count=2, interval_ms=500)
        except Exception as exc:
            logger.warning(f"STELLA standalone frame capture failed: {exc}")
            frames = []

        if not frames:
            return "I can't see anything right now -- the camera feed isn't available."

        logger.info(f"STELLA standalone query: {len(frames)} frames")
        prompt = STANDALONE_QUESTION_PROMPT.format(question=question)
        response = await self._call_stella(prompt, frames)
        if not response or response.startswith("Error"):
            return "I couldn't analyze the current view. Please try again."
        return response

    async def get_step_description(self, step_num: int) -> Optional[str]:
        if not self._steps or step_num < 1 or step_num > len(self._steps):
            return None
        step_text = self._steps[step_num - 1]
        prompt = STEP_DESCRIPTION_PROMPT.format(
            protocol_name=self._protocol_name or "Unknown",
            current_num=step_num,
            total_steps=len(self._steps),
            current_step_text=step_text,
        )
        frames = await self._capture_frames()
        response = await self._call_stella(prompt, frames)
        if response and not response.startswith("Error"):
            return response.strip()
        return None

    # ------------------------------------------------------------------
    # Monitoring loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self):
        logger.info(f"STELLA monitor loop started (interval={self._polling_interval}s)")
        try:
            while True:
                if not self._active:
                    break
                await asyncio.sleep(self._polling_interval)
                if not self._active:
                    break
                try:
                    await self._poll_once()
                except Exception as exc:
                    logger.error(f"STELLA poll error: {exc}")
        except asyncio.CancelledError:
            pass
        logger.info("STELLA monitor loop ended")

    async def _poll_once(self):
        if not self._active:
            return

        frames = await self._capture_frames()
        if not frames:
            return

        idx = self._current_step - 1
        if idx >= len(self._steps):
            return

        all_steps = build_all_steps_block(self._steps, self._current_step, self._completed_steps)

        step_description = ""
        common_errors_block = "  (none specified)"
        try:
            from tools.protocols.state import get_protocol_state
            state = get_protocol_state()
            if state.is_active and 0 <= idx < len(state.steps):
                sd = state.steps[idx]
                if sd.description:
                    step_description = sd.description
                if sd.common_errors:
                    common_errors_block = "\n".join(f"  - {e}" for e in sd.common_errors)
        except Exception:
            pass

        prompt = MONITORING_PROMPT.format(
            protocol_name=self._protocol_name,
            total_steps=len(self._steps),
            all_steps_block=all_steps,
            protocol_context_block=self._protocol_context or "No additional protocol notes provided.",
            current_step_num=self._current_step,
            current_step_text=self._steps[idx],
            step_description=step_description or "No additional description available.",
            common_errors_block=common_errors_block,
            frame_count=len(frames),
            window_secs=self._window_secs,
        )

        raw = await self._call_stella(prompt, frames)
        if not raw:
            return

        parsed = self._parse_response(raw)
        self._stella_log.info(
            f"POLL step={self._current_step} | raw={raw[:200]} | parsed={parsed}"
        )

        if parsed["status"] == "unknown" and self._llm_fallback_enabled:
            parsed = await self._llm_fallback_parse(raw)
            self._stella_log.info(f"LLM_FALLBACK parsed={parsed}")

        if parsed["status"] == "unknown":
            parsed["status"] = "same"

        # Always verify SAME responses through the text LLM for quick reasoning
        if parsed["status"] == "same" and not self._in_error_state:
            parsed = await self._llm_quick_verify(parsed, frames)

        detail = parsed.get("detail", "")
        if detail and detail != self._last_observation:
            self._last_observation = detail

        try:
            from tools.protocols.state import get_protocol_state
            state = get_protocol_state()
            if state.is_active and detail:
                old_vision = state.stella_vision_text
                state.stella_vision_text = detail
                if detail != old_vision and parsed["status"] in {"same", "advanced"}:
                    from tools.display import ui as viture_ui
                    await viture_ui.render_step_panel(state)
        except Exception:
            pass

        await self._handle_parsed(parsed)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": "unknown", "detail": raw, "error": None}

        status_match = re.search(r"STATUS:\s*(SAME|ADVANCED|ERROR|COMPLETED)", raw, re.IGNORECASE)
        if status_match:
            result["status"] = status_match.group(1).lower()

        detail_match = re.search(r"DETAIL:\s*(.+)", raw, re.IGNORECASE)
        if detail_match:
            result["detail"] = detail_match.group(1).strip()

        error_match = re.search(r"ERROR:\s*(.+)", raw, re.IGNORECASE)
        if error_match:
            err_text = error_match.group(1).strip()
            if err_text and err_text.lower() not in ("", "n/a", "none", "blank", "-", "no", "null"):
                result["error"] = err_text

        return result

    async def _llm_fallback_parse(self, raw: str) -> Dict[str, Any]:
        idx = self._current_step - 1
        step_text = self._steps[idx] if idx < len(self._steps) else "N/A"
        prompt = LLM_FALLBACK_PROMPT.format(
            protocol_name=self._protocol_name,
            current_num=self._current_step,
            current_step_text=step_text,
            raw_response=raw[:500],
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self._llm_base_url}/chat/completions",
                    json={
                        "model": self._llm_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 256,
                    },
                    headers={"Authorization": f"Bearer {self._llm_api_key}"},
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]

            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return {
                    "status": data.get("status", "same"),
                    "detail": data.get("detail", raw[:200]),
                    "error": data.get("error"),
                }
        except Exception as exc:
            logger.warning(f"LLM fallback parse failed: {exc}")

        return {"status": "same", "detail": raw[:200], "error": None}

    async def _handle_parsed(self, parsed: Dict[str, Any]):
        status = parsed["status"]
        detail = parsed.get("detail", "")
        error_msg = parsed.get("error")

        idx = self._current_step - 1
        step_text = self._steps[idx] if idx < len(self._steps) else ""

        if status == "same":
            if self._in_error_state:
                self._pending_clear_count += 1
                if self._pending_clear_count < self._CLEAR_CONFIRM_POLLS:
                    self._stella_log.info(
                        f"SAME observed during error state; waiting for clear confirmation "
                        f"({self._pending_clear_count}/{self._CLEAR_CONFIRM_POLLS})"
                    )
                    return
                self._in_error_state = False
                self._pending_clear_count = 0
                self._stella_log.info("Auto-cleared error state (STELLA returned SAME)")
                try:
                    from tools.protocols.state import get_protocol_state
                    state = get_protocol_state()
                    state.error_cooldown_until = time.time() + self._POST_CLEAR_GRACE
                except Exception:
                    pass
                await self._emit(StepEvent(
                    step_num=self._current_step,
                    total_steps=len(self._steps),
                    state=StepState.STARTED,
                    step_text=step_text,
                    message=f"Error resolved. Continuing step {self._current_step}: {step_text}",
                ))
            else:
                self._pending_error_count = 0
            return

        if status == "advanced":
            self._in_error_state = False
            self._pending_error_count = 0
            self._pending_clear_count = 0
            self._completed_steps.append(self._current_step)
            await self._emit(StepEvent(
                step_num=self._current_step,
                total_steps=len(self._steps),
                state=StepState.COMPLETED,
                step_text=step_text,
                message=f"Completed step {self._current_step}: {step_text}",
            ))
            self._current_step += 1
            if self._current_step <= len(self._steps):
                new_text = self._steps[self._current_step - 1]
                await self._emit(StepEvent(
                    step_num=self._current_step,
                    total_steps=len(self._steps),
                    state=StepState.STARTED,
                    step_text=new_text,
                    message=f"Starting step {self._current_step}: {new_text}",
                ))
            else:
                await self._emit(StepEvent(
                    step_num=len(self._steps),
                    total_steps=len(self._steps),
                    state=StepState.COMPLETED,
                    step_text=step_text,
                    message="All steps completed! Protocol finished.",
                ))
                self._active = False

        elif status == "error":
            if not self._in_error_state:
                self._pending_error_count += 1
                if self._pending_error_count < self._ERROR_CONFIRM_POLLS:
                    self._stella_log.info(
                        f"ERROR observed; waiting for confirmation "
                        f"({self._pending_error_count}/{self._ERROR_CONFIRM_POLLS})"
                    )
                    return

            now = time.time()

            if (now - self._last_error_emit_time) < self._ERROR_EMIT_COOLDOWN:
                self._stella_log.info(
                    f"ERROR suppressed (20s cooldown) step={self._current_step}: {error_msg or detail}"
                )
                return

            try:
                from tools.protocols.state import get_protocol_state
                state = get_protocol_state()
                if state.is_error_on_cooldown():
                    self._stella_log.info(
                        f"ERROR suppressed (grace period) step={self._current_step}: {error_msg or detail}"
                    )
                    return
            except Exception:
                pass

            self._in_error_state = True
            self._pending_error_count = 0
            self._pending_clear_count = 0
            self._last_error_emit_time = now
            self._stella_log.info(f"ERROR detected on step {self._current_step}: {error_msg or detail}")
            await self._emit(StepEvent(
                step_num=self._current_step,
                total_steps=len(self._steps),
                state=StepState.ERROR,
                step_text=step_text,
                message=detail,
                error_detail=error_msg or detail,
            ))

        elif status == "completed":
            self._in_error_state = False
            self._pending_error_count = 0
            self._pending_clear_count = 0
            for s in range(self._current_step, len(self._steps) + 1):
                if s not in self._completed_steps:
                    self._completed_steps.append(s)
            await self._emit(StepEvent(
                step_num=len(self._steps),
                total_steps=len(self._steps),
                state=StepState.COMPLETED,
                step_text=self._steps[-1] if self._steps else "",
                message="All steps completed! Protocol finished.",
            ))
            self._active = False

    # ------------------------------------------------------------------
    # Verification helpers (Fix C + Fix D)
    # ------------------------------------------------------------------

    # Language patterns that suggest STELLA is hedging about an in-progress action
    _PROGRESS_PHRASES = (
        "in the process", "preparing to", "beginning to", "reaching for",
        "approaching", "about to", "moving toward", "going to",
        "getting ready", "positioning", "still working", "ongoing",
    )

    _HEDGING_PHRASES = (
        "possibly", "likely", "appears to", "seems to", "unclear",
        "may be", "might be", "could be", "not certain", "hard to tell",
    )

    async def _capture_latest_frame(self) -> Optional[str]:
        """Capture a single latest frame via the frame source."""
        try:
            self._ensure_frame_source()
            frames = await self._frame_source.get_frames(1, 0)
            return frames[0] if frames else None
        except Exception as exc:
            logger.warning(f"Latest frame capture failed: {exc}")
            return None

    async def _call_llm_text(self, prompt: str, max_tokens: int = 256) -> Optional[str]:
        """Call the text LLM (Qwen) with a text-only prompt."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self._llm_base_url}/chat/completions",
                    json={
                        "model": self._llm_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": max_tokens,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                    headers={"Authorization": f"Bearer {self._llm_api_key}"},
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning(f"LLM text call failed: {exc}")
            return None

    def _has_progress_language(self, detail: str) -> bool:
        detail_lower = detail.lower()
        return any(phrase in detail_lower for phrase in self._PROGRESS_PHRASES)

    def _has_hedging_language(self, text: str) -> bool:
        text_lower = text.lower()
        return any(phrase in text_lower for phrase in self._HEDGING_PHRASES)

    async def _llm_quick_verify(self, parsed: Dict[str, Any], frames: List[str]) -> Dict[str, Any]:
        """Always verify SAME via text LLM, escalate to VLM/describe if uncertain."""
        detail = parsed.get("detail", "")
        if not detail:
            return parsed

        idx = self._current_step - 1
        step_text = self._steps[idx] if idx < len(self._steps) else "N/A"

        prompt = LLM_QUICK_VERIFY_PROMPT.format(
            current_num=self._current_step,
            total_steps=len(self._steps),
            current_step_text=step_text,
            stella_detail=detail,
        )

        llm_raw = await self._call_llm_text(prompt, max_tokens=128)
        if not llm_raw:
            return parsed

        self._stella_log.info(f"LLM_VERIFY: {llm_raw.strip()[:200]}")

        status_match = re.search(r"STATUS:\s*(SAME|ADVANCED|UNCERTAIN)", llm_raw, re.IGNORECASE)
        if not status_match:
            return parsed

        verdict = status_match.group(1).upper()

        if verdict == "ADVANCED":
            reason_match = re.search(r"REASON:\s*(.+)", llm_raw, re.IGNORECASE)
            reason = reason_match.group(1).strip() if reason_match else detail
            self._stella_log.info(f"LLM_VERIFY: upgraded SAME -> ADVANCED: {reason}")
            return {"status": "advanced", "detail": reason, "error": None}

        if verdict == "UNCERTAIN":
            self._stella_log.info("LLM_VERIFY: uncertain, escalating to single-frame VLM check")
            return await self._vlm_single_frame_verify(parsed, frames)

        return parsed

    async def _vlm_single_frame_verify(self, parsed: Dict[str, Any], frames: List[str]) -> Dict[str, Any]:
        """Escalation: verify with a single latest frame via VLM, then describe-and-reason."""
        detail = parsed.get("detail", "")

        latest_frame = await self._capture_latest_frame()
        if not latest_frame:
            return parsed

        idx = self._current_step - 1
        step_text = self._steps[idx] if idx < len(self._steps) else "N/A"

        prompt = SINGLE_FRAME_VERIFY_PROMPT.format(
            current_num=self._current_step,
            current_step_text=step_text,
            stella_detail=detail,
        )

        raw = await self._call_stella(prompt, [latest_frame])
        if not raw:
            return parsed

        self._stella_log.info(f"VLM_VERIFY response: {raw[:200]}")

        status_match = re.search(r"STATUS:\s*(SAME|ADVANCED)", raw, re.IGNORECASE)
        if status_match and status_match.group(1).upper() == "ADVANCED":
            reason_match = re.search(r"REASON:\s*(.+)", raw, re.IGNORECASE)
            reason = reason_match.group(1).strip() if reason_match else detail
            self._stella_log.info(f"VLM_VERIFY: upgraded SAME -> ADVANCED: {reason}")
            return {"status": "advanced", "detail": reason, "error": None}

        reason_match = re.search(r"REASON:\s*(.+)", raw, re.IGNORECASE)
        reason_text = reason_match.group(1).strip() if reason_match else raw
        if self._has_hedging_language(reason_text):
            self._stella_log.info("VLM_VERIFY: uncertain, escalating to describe-then-reason")
            return await self._describe_and_reason(parsed, frames)

        return parsed

    async def _describe_and_reason(self, parsed: Dict[str, Any], frames: List[str]) -> Dict[str, Any]:
        """Fix D: two-pass fallback -- STELLA describes frames, LLM reasons.

        Pass 1: Ask STELLA (VLM) to describe each frame individually.
        Pass 2: Ask the text LLM to reason over the descriptions.
        """
        idx = self._current_step - 1
        step_text = self._steps[idx] if idx < len(self._steps) else "N/A"

        # Pass 1: STELLA describes
        describe_prompt = DESCRIBE_FRAMES_PROMPT.format(
            frame_count=len(frames),
            current_step_text=step_text,
        )

        descriptions_raw = await self._call_stella(describe_prompt, frames)
        if not descriptions_raw:
            return parsed

        self._stella_log.info(f"VERIFY_D descriptions: {descriptions_raw[:300]}")

        # Pass 2: LLM reasons
        reason_prompt = REASON_OVER_DESCRIPTIONS_PROMPT.format(
            current_step_text=step_text,
            frame_descriptions=descriptions_raw,
        )

        reason_raw = await self._call_llm_text(reason_prompt, max_tokens=256)
        if not reason_raw:
            return parsed

        self._stella_log.info(f"VERIFY_D reasoning: {reason_raw[:200]}")

        status_match = re.search(r"STATUS:\s*(SAME|ADVANCED)", reason_raw, re.IGNORECASE)
        if status_match and status_match.group(1).upper() == "ADVANCED":
            reason_match = re.search(r"REASON:\s*(.+)", reason_raw, re.IGNORECASE)
            reason = reason_match.group(1).strip() if reason_match else parsed.get("detail", "")
            self._stella_log.info(f"VERIFY_D: upgraded SAME -> ADVANCED: {reason}")
            return {"status": "advanced", "detail": reason, "error": None}

        return parsed

    # ------------------------------------------------------------------
    # Frame capture
    # ------------------------------------------------------------------

    def _ensure_frame_source(self):
        if self._frame_source is None:
            from config import _current_session_id
            session_id = _current_session_id.get("default-xr-session")
            self._frame_source = create_frame_source(self._config, session_id)

    async def _capture_frames(self) -> List[str]:
        try:
            self._ensure_frame_source()
            if self._frame_mode == "multi":
                interval_ms = int(self._window_secs * 1000 / max(self._frame_count - 1, 1))
                return await self._frame_source.get_frames(self._frame_count, interval_ms)
            else:
                frames = await self._frame_source.get_frames(1, 0)
                return frames[:1] if frames else []
        except Exception as exc:
            logger.warning(f"STELLA frame capture failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # VLM calls
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_data_uri(frame: str) -> str:
        """Wrap raw base64 JPEG in a data URI if not already one."""
        if frame.startswith("data:"):
            return frame
        return f"data:image/jpeg;base64,{frame}"

    async def _call_stella(self, prompt: str, frames: List[str]) -> Optional[str]:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for frame in frames:
            uri = self._ensure_data_uri(frame)
            content.append({"type": "image_url", "image_url": {"url": uri}})

        messages = [{"role": "user", "content": content}]

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json={
                        "model": self._model,
                        "messages": messages,
                        "temperature": self._temperature,
                        "max_tokens": self._max_tokens,
                        "top_p": self._top_p,
                    },
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if resp.status_code != 200:
                    body = resp.text[:500]
                    logger.error(f"STELLA VLM {resp.status_code}: {body}")
                    return None
            answer = resp.json()["choices"][0]["message"]["content"]
            logger.debug(f"STELLA response ({len(answer)} chars): {answer[:200]}")
            return answer
        except Exception as exc:
            logger.error(f"STELLA VLM call failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Protocol generation
    # ------------------------------------------------------------------

    async def _generate_protocol(self) -> List[str]:
        frames = await self._capture_frames()
        if not frames:
            logger.warning("No frames available for protocol generation")
            return []

        raw = await self._call_stella(GENERATE_PROTOCOL_PROMPT, frames)
        if not raw:
            return []

        steps: List[str] = []
        for line in raw.splitlines():
            line = line.strip()
            m = re.match(r"^\d+[\.\)]\s*", line)
            if m:
                step_text = line[m.end():].strip()
                if step_text:
                    steps.append(step_text)

        if not steps:
            steps = [l.strip() for l in raw.splitlines() if l.strip()]

        return steps
