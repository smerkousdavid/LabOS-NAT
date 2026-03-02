"""Global configuration and shared service accessors.

Holds the application config dict and provides factory functions for
commonly needed services (LLM clients, WebSocket session registry).
Each domain module owns its own singletons; this file only holds
cross-cutting config.

Multi-client support: ``_current_session_id`` is a ContextVar set at
the beginning of each request in ``ws_handler.py``.  Tools call
``get_ws_connection()`` to send messages back to the XR runtime
for the current session.
"""

import contextvars
from typing import Any, Dict, Optional, Tuple

_config: Dict[str, Any] = {}

_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_id", default="default-xr-session"
)

_ws_connections: Dict[str, Any] = {}  # session_id -> WebSocket
_tool_enabled_overrides: Dict[str, bool] = {}


def set_config(config: Dict[str, Any]):
    global _config
    _config = config
    _initialize_tool_overrides(config)


def get_config() -> Dict[str, Any]:
    return _config


# ---------------------------------------------------------------------------
# WebSocket session registry
# ---------------------------------------------------------------------------

def register_ws_connection(session_id: str, ws):
    _ws_connections[session_id] = ws


def unregister_ws_connection(session_id: str):
    _ws_connections.pop(session_id, None)


def get_ws_connection(session_id: Optional[str] = None):
    """Return the WebSocket for a session, or the current session if not specified."""
    sid = session_id or _current_session_id.get("default-xr-session")
    return _ws_connections.get(sid)


def get_active_sessions() -> list[str]:
    return list(_ws_connections.keys())


# ---------------------------------------------------------------------------
# LLM client factory
# ---------------------------------------------------------------------------

def get_llm_client(llm_name: str = "router") -> Tuple:
    """Return ``(OpenAI_client, model_name)`` for the requested LLM config."""
    from openai import OpenAI
    llm_cfg = _config.get("llms", {}).get(llm_name, {})
    base_url = llm_cfg.get("base_url", "http://localhost:8001/v1")
    api_key = llm_cfg.get("api_key", "not-needed")
    model = llm_cfg.get("model", "default-model")
    return OpenAI(base_url=base_url, api_key=api_key), model


# ---------------------------------------------------------------------------
# Tool toggles
# ---------------------------------------------------------------------------

def _initialize_tool_overrides(config: Dict[str, Any]):
    """Seed in-memory tool enabled state from NAT config."""
    tools_cfg = config.get("tools", {})

    def seed(name: str, enabled: bool):
        _tool_enabled_overrides[name] = bool(enabled)

    for key, value in tools_cfg.items():
        if isinstance(value, dict) and "enabled" in value:
            seed(key, bool(value.get("enabled", True)))

    web_enabled = bool(tools_cfg.get("web", {}).get("enabled", True))
    seed("web_search", web_enabled)
    seed("image_search", web_enabled)

    code_enabled = bool(tools_cfg.get("code", {}).get("enabled", True))
    seed("run_code", code_enabled)

    datetime_enabled = bool(tools_cfg.get("datetime", {}).get("enabled", True))
    seed("get_datetime", datetime_enabled)

    vlm_enabled = bool(tools_cfg.get("vlm", {}).get("enabled", True))
    seed("query_stella", vlm_enabled)

    vsop_enabled = bool(tools_cfg.get("vsop", {}).get("enabled", True))
    for name in (
        "list_protocols",
        "start_protocol",
        "stop_protocol",
        "next_step",
        "previous_step",
        "go_to_step",
        "restart_protocol",
        "clear_error",
        "get_protocol_status",
    ):
        seed(name, vsop_enabled)


def get_tool_enabled(tool_name: str, default: bool = True) -> bool:
    return bool(_tool_enabled_overrides.get(tool_name, default))


def set_tool_enabled(tool_name: str, enabled: bool):
    _tool_enabled_overrides[tool_name] = bool(enabled)


def set_tool_enabled_many(state: Dict[str, bool]):
    for name, enabled in state.items():
        set_tool_enabled(name, bool(enabled))


def get_all_tool_enabled() -> Dict[str, bool]:
    return dict(_tool_enabled_overrides)
