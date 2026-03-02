"""Step event handler -- bridge between VSOP providers and the UI/state.

Moved from server_xr.py to keep the HTTP server thin.  This module owns
the TTS deduplication state for error messages.
"""

import time
import uuid

from loguru import logger

from tools.protocols.state import get_protocol_state
from tools.protocols.tools import format_experiment_data_rich_text
from tools.display import ui as viture_ui
from tools.display import tts as viture_tts
from context.manager import get_context_manager

# TTS deduplication for errors
_last_error_tts_step: int = 0
_last_error_tts_time: float = 0.0
_ERROR_TTS_COOLDOWN = 20.0  # seconds between error TTS for same step


async def on_step_event(event):
    """Callback wired to the VSOP provider to relay step events.

    Guards on ``state.mode`` so stale events after stop are silently dropped.
    Delegates all rendering to the Viture UI/TTS modules.
    Deduplicates error TTS to prevent audio queue flood.
    """
    global _last_error_tts_step, _last_error_tts_time

    state = get_protocol_state()
    state_val = event.state.value if hasattr(event.state, "value") else str(event.state)

    if state.mode != "running":
        return

    # Update protocol state from event
    step_idx = event.step_num - 1
    if state_val == "COMPLETED":
        if event.step_num not in state.completed_steps:
            state.completed_steps.append(event.step_num)
        if 0 <= step_idx < len(state.steps):
            state.steps[step_idx].status = "completed"
            state.steps[step_idx].error_detail = None
        if event.step_num >= len(state.steps):
            state.mode = "completed"
    elif state_val == "STARTED":
        state.current_step = event.step_num
        if 0 <= step_idx < len(state.steps):
            state.steps[step_idx].status = "in_progress"
        _last_error_tts_step = 0
    elif state_val == "ERROR":
        err_msg = event.error_detail or event.message
        state.error_history.append({
            "step": event.step_num,
            "detail": err_msg,
        })
        if 0 <= step_idx < len(state.steps):
            state.steps[step_idx].status = "error"
            state.steps[step_idx].error_detail = err_msg

    # Render
    if state_val == "ERROR":
        if viture_ui.get_display_mode() == "overlay":
            logger.debug("[Events] Error suppressed -- overlay mode active")
        else:
            await viture_ui.render_error(state, event.error_detail or event.message)
            now = time.time()
            if event.step_num != _last_error_tts_step or (now - _last_error_tts_time) > _ERROR_TTS_COOLDOWN:
                _last_error_tts_step = event.step_num
                _last_error_tts_time = now
                tts_message = f"Error on step {event.step_num}. {event.error_detail or event.message}"
                await viture_tts.push_tts(tts_message)
            else:
                logger.debug(f"[Events] Suppressed duplicate error TTS for step {event.step_num}")

    elif state_val == "COMPLETED" and state.mode == "completed":
        completed_at = time.time()
        run_snapshot = {
            "run_id": uuid.uuid4().hex[:8],
            "protocol_name": state.protocol_name,
            "completed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(completed_at)),
            "duration_s": max(0, int(completed_at - state.start_time)) if state.start_time else 0,
            "completed_steps": list(state.completed_steps),
            "error_history": list(state.error_history),
            "experiment_data": dict(state.experiment_data),
        }
        state.completed_runs.append(run_snapshot)
        rich_summary = await format_experiment_data_rich_text(state)
        await viture_ui.render_completion(state.protocol_name, rich_summary=rich_summary)
        tts = f"Protocol complete! You have finished the {state.protocol_name} protocol. Data saved."
        await viture_tts.push_tts(tts)
        get_context_manager().set_context("main_menu")
        state.reset()

    elif state_val == "STARTED":
        await viture_ui.render_step_panel(state)
        await viture_tts.push_tts(event.message)

    elif state_val == "COMPLETED":
        await viture_ui.render_step_panel(state)
        await viture_tts.push_tts(event.message)
