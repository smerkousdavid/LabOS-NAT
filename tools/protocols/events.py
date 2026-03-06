"""Step event handler -- bridge between VSOP providers and the UI/state.

Moved from server_xr.py to keep the HTTP server thin.  This module owns
the TTS deduplication state for error messages and the robot auto-trigger
for steps annotated with ``robot_protocol``.
"""

import asyncio
import time
import uuid

from loguru import logger

from tools.protocols.state import get_protocol_state
from tools.protocols.tools import format_experiment_data_rich_text, generate_protocol_summary
from tools.display import ui as viture_ui
from tools.display import tts as viture_tts
from context.manager import get_context_manager

# TTS deduplication for errors
_last_error_tts_step: int = 0
_last_error_tts_time: float = 0.0
_ERROR_TTS_COOLDOWN = 20.0  # seconds between error TTS for same step
_COMPLETION_LINGER_SECONDS = 60.0
_completion_reset_task: asyncio.Task | None = None

_ROBOT_STATUS_POLL_INTERVAL = 3.0


async def _run_robot_protocol(state, step_idx: int):
    """Background task: execute a robot protocol and poll until complete."""
    from tools.robot import get_robot_manager

    step = state.steps[step_idx]
    step_num = step_idx + 1
    robot_proto = step.robot_protocol
    mgr = get_robot_manager()

    await viture_tts.push_tts(f"Starting robot protocol {robot_proto} for step {step_num}.")
    logger.info(f"[Events] Triggering robot protocol '{robot_proto}' for step {step_num}")

    result = await mgr.call_tool("start_protocol", {"protocol_name": robot_proto}, timeout=30.0)
    if not result.get("success"):
        err = f"Robot failed to start protocol '{robot_proto}': {result.get('result', 'unknown')}"
        logger.error(f"[Events] {err}")
        await viture_tts.push_tts(err)
        return

    await viture_tts.push_tts(f"Robot is running {robot_proto}.")

    while state.mode == "running" and state.current_step == step_num:
        await asyncio.sleep(_ROBOT_STATUS_POLL_INTERVAL)
        if not mgr.is_connected():
            await viture_tts.push_tts("Robot disconnected while running protocol.")
            break
        status = await mgr.call_tool("get_status", timeout=10.0)
        status_text = str(status.get("result", ""))
        logger.debug(f"[Events] Robot status poll: {status_text[:120]}")
        if "waiting" in status_text.lower() or "no protocol" in status_text.lower():
            await viture_tts.push_tts(f"Robot has finished the {robot_proto} protocol.")
            break


async def on_step_event(event):
    """Callback wired to the VSOP provider to relay step events.

    Guards on ``state.mode`` so stale events after stop are silently dropped.
    Delegates all rendering to the Viture UI/TTS modules.
    Deduplicates error TTS to prevent audio queue flood.
    When a STARTED step has a ``robot_protocol`` annotation, auto-triggers
    the robot protocol (or reports an error if the robot is disconnected).
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
        await _emit_labos_protocol_error(state.protocol_name, event.error_detail or event.message)

    elif state_val == "COMPLETED" and state.mode == "completed":
        await complete_protocol_run(state)

    elif state_val == "STARTED":
        await viture_ui.render_step_panel(state)
        tts_msg = f"Step {event.step_num}: {event.step_text}"
        await viture_tts.push_tts(tts_msg)
        prev_step = event.step_num - 1 if event.step_num > 1 else 0
        await _emit_labos_step_change(state.protocol_name, prev_step, event.step_num)

        # Auto-trigger robot protocol if annotated
        if 0 <= step_idx < len(state.steps) and state.steps[step_idx].robot_protocol:
            from tools.robot import get_robot_manager
            mgr = get_robot_manager()
            if mgr.is_connected():
                asyncio.create_task(_run_robot_protocol(state, step_idx))
            else:
                err = f"Failed to run protocol step {event.step_num}, robot is not connected!"
                logger.error(f"[Events] {err}")
                state.steps[step_idx].status = "error"
                state.steps[step_idx].error_detail = err
                state.error_history.append({"step": event.step_num, "detail": err})
                await viture_ui.render_error(state, err)
                await viture_tts.push_tts(err)

    elif state_val == "COMPLETED":
        await viture_ui.render_step_panel(state)


# ---------------------------------------------------------------------------
# LabOS Live event helpers (no-op when not connected)
# ---------------------------------------------------------------------------

async def _emit_labos_step_change(protocol_name: str, prev: int, step: int):
    try:
        from labos_live_client import get_labos_client
        from config import _current_session_id
        client = get_labos_client(_current_session_id.get("default-xr-session"))
        if client and client.connected:
            await client.send_protocol_change_step(protocol_name, prev, step)
    except Exception:
        pass


async def _emit_labos_protocol_error(protocol_name: str, error: str):
    try:
        from labos_live_client import get_labos_client
        from config import _current_session_id
        client = get_labos_client(_current_session_id.get("default-xr-session"))
        if client and client.connected:
            await client.send_protocol_error(protocol_name, error)
    except Exception:
        pass


async def _emit_labos_protocol_stop():
    try:
        from labos_live_client import get_labos_client
        from config import _current_session_id
        client = get_labos_client(_current_session_id.get("default-xr-session"))
        if client and client.connected:
            await client.send_protocol_stop()
    except Exception:
        pass


async def complete_protocol_run(state, completion_tts_prefix: str = "Protocol complete.") -> None:
    """Finalize protocol completion UI/state and keep context for post-run Q&A."""
    global _completion_reset_task

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

    state.is_active = False
    state.mode = "completed"

    plain_summary, rich_summary = await generate_protocol_summary(state)
    await viture_ui.render_completion(state.protocol_name, rich_summary=rich_summary)
    tts = f"{completion_tts_prefix} {plain_summary[:200]}".strip() if plain_summary else completion_tts_prefix
    await viture_tts.push_tts(tts)

    get_context_manager().set_context("protocol_completed")

    async def _delayed_reset():
        try:
            await asyncio.sleep(_COMPLETION_LINGER_SECONDS)
            logger.info("[Events] Completion linger expired -- returning to main menu")
            state.reset()
            get_context_manager().set_context("main_menu")
            await viture_ui.render_greeting()
        except asyncio.CancelledError:
            pass

    if _completion_reset_task and not _completion_reset_task.done():
        _completion_reset_task.cancel()
    _completion_reset_task = asyncio.create_task(_delayed_reset())
    await _emit_labos_protocol_stop()
