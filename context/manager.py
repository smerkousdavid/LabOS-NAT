"""Context manager -- controls which LLM system prompt is active.

Keeps track of the current UI context (main_menu, protocol_listing,
protocol_running) and dynamically builds the appropriate system prompt
by loading Markdown templates from ``context/modes/``.

Also pushes context-aware idle timeouts to the runtime connector's
wake word filter whenever the context changes.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

CONTEXT_TIMEOUTS: Dict[str, float] = {
    "main_menu": 60.0,
    "protocol_listing": 120.0,
    "protocol_running": 1200.0,
    "protocol_completed": 120.0,
}


_MODES_DIR = Path(__file__).resolve().parent / "modes"


# ---------------------------------------------------------------------------
# Prompt building helpers
# ---------------------------------------------------------------------------

def build_all_steps_block(
    steps: List[str],
    current_step: int,
    completed_steps: List[int],
) -> str:
    """Build the ``[DONE]/[>>>]/[ ]`` step list shared by LLM and STELLA prompts."""
    lines: List[str] = []
    for i, step_text in enumerate(steps, 1):
        if i in completed_steps:
            lines.append(f"  [DONE] Step {i}: {step_text}")
        elif i == current_step:
            lines.append(f"  [>>>]  Step {i}: {step_text}          <-- CURRENT")
        else:
            lines.append(f"  [ ]    Step {i}: {step_text}")
    return "\n".join(lines)


def build_windowed_steps_block(
    steps: List[str],
    current_step: int,
    completed_steps: List[int],
    window: int = 3,
) -> str:
    """Build a windowed step list (±window around current) for token-limited models."""
    total = len(steps)
    lo = max(1, current_step - window)
    hi = min(total, current_step + window)

    lines: List[str] = []
    if lo > 1:
        lines.append(f"  ... {lo - 1} earlier step(s) ...")
    for i in range(lo, hi + 1):
        step_text = steps[i - 1]
        if i in completed_steps:
            lines.append(f"  [DONE] Step {i}: {step_text}")
        elif i == current_step:
            lines.append(f"  [>>>]  Step {i}: {step_text}          <-- CURRENT")
        else:
            lines.append(f"  [ ]    Step {i}: {step_text}")
    if hi < total:
        lines.append(f"  ... {total - hi} more step(s) ...")
    return "\n".join(lines)


def _load_mode_template(name: str) -> str:
    """Read a prompt ``.md`` file from ``context/modes/``."""
    path = _MODES_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent mode template not found: {path}")
    return path.read_text()


def _build_listing_prompt(protocol_list_text: str) -> str:
    template = _load_mode_template("protocol_listing")
    return template.replace("{protocol_list}", protocol_list_text)


def _build_running_prompt(
    protocol_name: str,
    steps: List[str],
    current_step: int,
    completed_steps: List[int],
    errors: List[Dict],
    elapsed_time: str = "0m 0s",
    current_step_description: str = "",
    current_step_common_errors: str = "",
    protocol_extra_context: str = "",
    experiment_data_block: str = "",
    stella_observation_history: str = "",
) -> str:
    template = _load_mode_template("protocol_running")

    all_steps_block = build_all_steps_block(steps, current_step, completed_steps)

    if errors:
        error_lines = [
            f"  Step {err.get('step', '?')}: {err.get('detail', 'unknown error')}"
            for err in errors
        ]
        error_history_block = "\n".join(error_lines)
    else:
        error_history_block = "  (none)"

    current_step_text = steps[current_step - 1] if current_step <= len(steps) else "N/A"
    next_step_num = current_step + 1
    next_step_text = steps[next_step_num - 1] if next_step_num <= len(steps) else "N/A"
    completed_count = len(completed_steps)
    remaining_count = len(steps) - completed_count

    if not current_step_description:
        current_step_description = "(no additional description)"
    if not current_step_common_errors:
        current_step_common_errors = "  (none known)"
    if not protocol_extra_context:
        protocol_extra_context = "(none)"
    if not experiment_data_block:
        experiment_data_block = "<experiment_data>\n(none)\n</experiment_data>"

    return (
        template
        .replace("{protocol_name}", protocol_name)
        .replace("{total_steps}", str(len(steps)))
        .replace("{elapsed_time}", elapsed_time)
        .replace("{all_steps_block}", all_steps_block)
        .replace("{error_history_block}", error_history_block)
        .replace("{current_step_num}", str(current_step))
        .replace("{current_step_text}", current_step_text)
        .replace("{current_step_description}", current_step_description)
        .replace("{current_step_common_errors}", current_step_common_errors)
        .replace("{protocol_extra_context}", protocol_extra_context)
        .replace("{experiment_data_block}", experiment_data_block)
        .replace("{stella_observation_history}", stella_observation_history or "(no observations yet)")
        .replace("{next_step_num}", str(next_step_num))
        .replace("{next_step_text}", next_step_text)
        .replace("{completed_count}", str(completed_count))
        .replace("{remaining_count}", str(remaining_count))
    )


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------

class ContextManager:
    """Singleton that tracks the current conversational context."""

    def __init__(self):
        self._context: str = "main_menu"

    def set_context(self, ctx: str):
        if ctx not in ("main_menu", "protocol_listing", "protocol_running", "protocol_completed", "protocol_discussion"):
            logger.warning(f"ContextManager: unknown context '{ctx}', ignoring")
            return
        logger.info(f"ContextManager: {self._context} -> {ctx}")
        self._context = ctx
        self._push_timeout(ctx)

    @staticmethod
    def _push_timeout(ctx: str):
        """Fire-and-forget: update the runtime's wake word timeout via WebSocket."""
        timeout = CONTEXT_TIMEOUTS.get(ctx, 20.0)

        async def _do_push():
            try:
                from config import _current_session_id
                from ws_handler import send_to_session
                session_id = _current_session_id.get("default-xr-session")
                await send_to_session(session_id, {
                    "type": "wake_timeout",
                    "seconds": timeout,
                })
                logger.info(f"Pushed timeout_seconds={timeout} for context '{ctx}'")
            except Exception as exc:
                logger.warning(f"Failed to push timeout for context '{ctx}': {exc}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_push())
        except RuntimeError:
            pass

    def get_context(self) -> str:
        return self._context

    def build_system_prompt(self, state=None) -> str:
        """Build the LLM system prompt for the current context.

        Args:
            state: Optional ProtocolState (needed for running/listing contexts).
        """
        if self._context == "protocol_listing":
            from tools.protocols.store import get_protocol_store, list_available_protocols
            from tools.protocols.state import get_protocol_state
            store = get_protocol_store()
            pstate = state or get_protocol_state()
            protocols = list_available_protocols(store, pstate)
            listing_text = "\n".join(
                f"  {i}. {p['pretty_name']} ({p.get('step_count', len(p.get('steps', [])))} steps)"
                for i, p in enumerate(protocols, 1)
            )
            return _build_listing_prompt(listing_text)

        if self._context == "protocol_completed" and state is not None:
            duration_s = int(time.time() - state.start_time) if state.start_time else 0
            m, s = divmod(duration_s, 60)
            duration = f"{m}m {s}s"

            obs_lines: list[str] = []
            if state.monitoring_high:
                obs_lines.append("Long-term observations:")
                for h in state.monitoring_high:
                    obs_lines.append(f"  {h}")
            if state.monitoring_medium:
                obs_lines.append("Medium-term observations:")
                for med in state.monitoring_medium:
                    obs_lines.append(f"  - {med}")
            obs_text = "\n".join(obs_lines) if obs_lines else "(none)"

            error_lines = [
                f"  Step {e.get('step', '?')}: {e.get('detail', 'unknown')}"
                for e in (state.error_history or [])
            ]
            error_text = "\n".join(error_lines) if error_lines else "(none)"

            exp_data = state.experiment_data_xml() if hasattr(state, "experiment_data_xml") else "(none)"

            return (
                f"You are STELLA, protocol coordinator on AR glasses.\n"
                f"The protocol '{state.protocol_name}' is COMPLETE.\n"
                f"Duration: {duration}. Steps: {len(state.completed_steps)}/{len(state.steps)}. "
                f"Errors: {len(state.error_history)}.\n\n"
                f"The user can ask about this completed run for the next minute.\n"
                f"Answer questions about observations, errors, logged data, or steps.\n"
                f"Keep answers concise (1-3 sentences). No flowery language.\n\n"
                f"<observations>\n{obs_text}\n</observations>\n\n"
                f"<errors>\n{error_text}\n</errors>\n\n"
                f"<experiment_data>\n{exp_data}\n</experiment_data>"
            )

        if self._context == "protocol_running" and state is not None:
            step_desc = ""
            step_errors = ""
            detail = state.current_step_detail()
            if detail:
                step_desc = detail.description
                if detail.common_errors:
                    step_errors = "\n".join(f"  - {e}" for e in detail.common_errors)

            obs_lines: list[str] = []
            if state.monitoring_high:
                obs_lines.append("Long-term (30min summaries):")
                for h in state.monitoring_high:
                    obs_lines.append(f"  {h}")
            if state.monitoring_medium:
                obs_lines.append("Medium-term (2min summaries):")
                for m in state.monitoring_medium:
                    obs_lines.append(f"  - {m}")
            if state.monitoring_granular:
                recent = state.monitoring_granular[-6:]
                obs_lines.append("Recent (~30s):")
                for g in recent:
                    obs_lines.append(f"  - {g}")
            stella_obs_history = "\n".join(obs_lines) if obs_lines else ""

            return _build_running_prompt(
                protocol_name=state.protocol_name,
                steps=state.step_texts(),
                current_step=state.current_step,
                completed_steps=state.completed_steps,
                errors=state.error_history,
                elapsed_time=state.elapsed_str(),
                current_step_description=step_desc,
                current_step_common_errors=step_errors,
                protocol_extra_context=state.extra_context,
                experiment_data_block=state.experiment_data_xml(),
                stella_observation_history=stella_obs_history,
            )

        if self._context == "protocol_discussion":
            template = _load_mode_template("protocol_discussion")
            draft = state.extra_context if state else ""
            return template.replace("{discussion_draft}", draft or "(no draft yet)")

        return _load_mode_template("main_menu")


# ---------------------------------------------------------------------------
# Per-session context managers
# ---------------------------------------------------------------------------

_context_managers: Dict[str, ContextManager] = {}


def get_context_manager() -> ContextManager:
    from config import _current_session_id
    sid = _current_session_id.get("default-xr-session")
    if sid not in _context_managers:
        _context_managers[sid] = ContextManager()
    return _context_managers[sid]
