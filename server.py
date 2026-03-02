"""LabOS NAT Server -- WebSocket-based agent protocol assistant.

Uses the OpenAI Agents SDK to run a multi-turn tool-calling loop.
Agent interaction happens exclusively over WebSocket at /ws.
HTTP endpoints are kept for health checks and tool catalog management.
"""

import asyncio
import json
import os
import re
import sys
import uuid
import time
from typing import Any, Dict, List, Optional
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from loguru import logger

_log_dir = os.environ.get("LOG_DIR", "/app/logs")
if os.path.isdir(_log_dir):
    logger.add(os.path.join(_log_dir, "nat_server.log"), rotation="20 MB", retention="3 days", level="DEBUG")

from agents import Runner, set_tracing_disabled

from config import (
    set_config as _set_config,
    _current_session_id,
    set_tool_enabled_many,
    get_all_tool_enabled,
)
from context.session import (
    get_session_items,
    save_session_items,
    clear_session,
    strip_reasoning,
    prepare_input,
    configure_budget,
)
from tools.protocols.state import get_protocol_state
from tools.protocols.store import init_protocol_store
from tools.protocols.events import on_step_event
from tools.protocols.tools import auto_capture_experiment_data_from_utterance
from tools.vsop_providers import get_vsop_provider_for_session, init_vsop_provider_for_session
from ws_handler import websocket_endpoint


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_file = os.environ.get("NAT_CONFIG_FILE")
    if config_file:
        config_path = Path(config_file)
    else:
        config_path = Path("./configs/config.yml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


CONFIG = load_config()
_set_config(CONFIG)
set_tracing_disabled(True)

_session_cfg = CONFIG.get("session", {})
_llm_max_len = int(CONFIG.get("llms", {}).get("router", {}).get("max_model_len", 16384))
_target_context_len = int(_session_cfg.get("context_window_tokens", 6000))
_effective_context_len = max(2048, min(_llm_max_len, _target_context_len))
configure_budget(
    _effective_context_len,
    summarize_trigger_tokens=_session_cfg.get("summarize_trigger_tokens", 7000),
    summary_target_tokens=_session_cfg.get("summary_target_tokens", 600),
)

init_protocol_store(CONFIG)


# ---------------------------------------------------------------------------
# Agent / hooks
# ---------------------------------------------------------------------------

_running_tasks: Dict[str, asyncio.Task] = {}
_agent = None
_hooks = None


def _get_agent():
    global _agent
    if _agent is None:
        from agent import create_agent
        _agent = create_agent(CONFIG)
    return _agent


def _get_hooks():
    global _hooks
    if _hooks is None:
        from agent import create_hooks
        _hooks = create_hooks(CONFIG)
    return _hooks


def _ensure_vsop(session_id: str):
    provider = get_vsop_provider_for_session(session_id)
    if provider is not None:
        return
    provider = init_vsop_provider_for_session(session_id, CONFIG)
    if provider._on_step_event is None:
        provider.set_on_step_event(on_step_event)


def _parse_context_overflow(error: Exception) -> Optional[tuple]:
    text = str(error or "")
    match = re.search(
        r"maximum context length is\s+(\d+)\s+tokens.*request has\s+(\d+)\s+input tokens",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    try:
        return int(match.group(1)), int(match.group(2))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core chat handler (called from ws_handler)
# ---------------------------------------------------------------------------

async def handle_chat_for_ws(session_id: str, user_input: str) -> str:
    """Run the agent for a single user message. Returns the response text."""
    _current_session_id.set(session_id)

    # Experiment data auto-capture
    try:
        captured, capture_message = await auto_capture_experiment_data_from_utterance(user_input)
    except Exception as exc:
        logger.warning(f"[NAT] Experiment data pre-pass failed: {exc}")
        captured, capture_message = False, ""
    if captured:
        state = get_protocol_state()
        if state.is_active:
            try:
                from tools.display import ui as display_ui
                await display_ui.render_step_panel(state)
            except Exception:
                pass
        return capture_message

    logger.info(f"[NAT] Session={session_id[:8]}... User: '{user_input}'")
    _ensure_vsop(session_id)

    if session_id in _running_tasks and not _running_tasks[session_id].done():
        _running_tasks[session_id].cancel()
        logger.info(f"[NAT] Cancelled in-flight task for session {session_id[:8]}...")

    agent = _get_agent()
    prev_items = get_session_items(session_id)
    run_input = prepare_input(prev_items, user_input)

    max_turns = _session_cfg.get("max_turns", 10)
    history_limit = _session_cfg.get("history_limit", 40)
    hooks = _get_hooks()

    t0 = time.time()
    try:
        task = asyncio.create_task(Runner.run(agent, run_input, max_turns=max_turns, hooks=hooks))
        _running_tasks[session_id] = task
        result = await task
    except asyncio.CancelledError:
        return "I was interrupted. How can I help you?"
    except Exception as e:
        overflow = _parse_context_overflow(e)
        if overflow:
            max_ctx, input_ctx = overflow
            logger.warning(f"[NAT] Context overflow (input={input_ctx}, max={max_ctx}). Retrying.")
            configure_budget(max_ctx)
            try:
                retry_input = prepare_input(prev_items, user_input)
                retry_task = asyncio.create_task(
                    Runner.run(agent, retry_input, max_turns=min(max_turns, 8), hooks=hooks)
                )
                _running_tasks[session_id] = retry_task
                result = await retry_task
            except Exception as e2:
                overflow2 = _parse_context_overflow(e2)
                if overflow2:
                    try:
                        fresh_input = [{"role": "user", "content": user_input}]
                        fresh_task = asyncio.create_task(
                            Runner.run(agent, fresh_input, max_turns=min(max_turns, 4), hooks=hooks)
                        )
                        _running_tasks[session_id] = fresh_task
                        result = await fresh_task
                    except Exception as e3:
                        logger.error(f"[NAT] Agent failed after overflow retries: {e3}")
                        raise
                else:
                    raise
        else:
            raise

    elapsed_ms = (time.time() - t0) * 1000
    final_output = strip_reasoning(result.final_output or "")
    logger.info(f"[NAT] Response ({elapsed_ms:.0f}ms): {final_output[:120]}")

    save_session_items(session_id, result.to_input_list(), history_limit)
    return final_output


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="LabOS NAT Server", version="3.0.0")

server_config = CONFIG.get("server", {})
if server_config.get("cors_enabled", True):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# WebSocket endpoint
@app.websocket("/ws")
async def ws_route(websocket: WebSocket):
    await websocket_endpoint(websocket)


# HTTP endpoints (admin / dashboard)
@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/tools/catalog")
async def tools_catalog():
    from agent import get_tool_catalog
    return {"tools": get_tool_catalog(), "state": get_all_tool_enabled()}


@app.put("/tools/catalog")
async def update_tools_catalog(payload: Dict[str, bool]):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be {tool_name: bool}")
    set_tool_enabled_many(payload)
    from agent import get_tool_catalog
    return {"status": "ok", "tools": get_tool_catalog(), "state": get_all_tool_enabled()}


@app.post("/clear_memory")
async def clear_memory_endpoint(payload: Dict[str, str] = {}):
    sid = payload.get("session_id", "default-xr-session")
    if clear_session(sid):
        return {"status": "success", "message": f"Memory cleared for session {sid[:16]}..."}
    return {"status": "not_found", "message": "Session not found"}


@app.get("/v1/models")
async def list_models():
    models = []
    router_config = CONFIG.get("llms", {}).get("router", {})
    if router_config:
        models.append({"id": router_config.get("model", "router"), "object": "model", "owned_by": "labos"})
    return {"object": "list", "data": models}


@app.on_event("startup")
async def startup_event():
    logger.info("[NAT] LabOS NAT Server starting (WebSocket mode)")
    llm_model = CONFIG.get("llms", {}).get("router", {}).get("model", "unknown")
    logger.info(f"[NAT] LLM model: {llm_model}")
    video_mode = CONFIG.get("video", {}).get("mode", "websocket")
    logger.info(f"[NAT] Video mode: {video_mode}")


if __name__ == "__main__":
    import uvicorn
    host = server_config.get("host", "0.0.0.0")
    port = server_config.get("port", 8002)
    logger.info(f"Starting LabOS NAT server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
