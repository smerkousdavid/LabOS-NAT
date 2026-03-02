"""Agent factory -- creates the main LabOS Agent using the OpenAI Agents SDK.

The agent is configured with:
- OpenAIChatCompletionsModel pointed at the local vLLM server
- Dynamic instructions callable that reads the current ContextManager state
- All protocol, general, and communication tools
- RunHooks that push short TTS notifications for notable tool calls
"""

from typing import Any, Dict

from agents import Agent, ModelSettings, RunHooks, Tool, set_tracing_disabled
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from loguru import logger
from openai import AsyncOpenAI

from tools.protocols.tools import (
    list_protocols,
    start_protocol,
    stop_protocol,
    next_step,
    previous_step,
    go_to_step,
    restart_protocol,
    clear_error,
    get_protocol_status,
    query_completed_protocol_data,
    show_experiment_data,
    get_errors,
    detailed_step,
)
from tools.experts.stella_vlm import query_stella
from tools.display.ui import send_to_display, show_protocol_panel
from tools.display.tts import update_user
from tools.common.web import web_search, image_search
from tools.common.code import run_code
from tools.common.datetime import get_datetime
from tools.common.history_summary import summarize_history

from tools.protocols.state import get_protocol_state
from context.manager import get_context_manager
from config import get_all_tool_enabled

ALL_TOOLS = [
    list_protocols,
    start_protocol,
    stop_protocol,
    next_step,
    previous_step,
    go_to_step,
    restart_protocol,
    clear_error,
    query_stella,
    get_protocol_status,
    query_completed_protocol_data,
    show_experiment_data,
    get_errors,
    detailed_step,
    web_search,
    image_search,
    run_code,
    get_datetime,
    summarize_history,
    update_user,
    send_to_display,
    show_protocol_panel,
]

TOOL_NOTIFICATIONS: Dict[str, str] = {
    "query_stella": "Asking Stella for more info",
    "run_code": "Running some code",
    "start_protocol": "Starting the protocol",
    "web_search": "Searching the web",
    "image_search": "Searching for images",
    "detailed_step": "Getting step details",
    "get_datetime": "Checking the time",
}

TOOL_DESCRIPTIONS: Dict[str, str] = {
    "list_protocols": "List available laboratory protocols.",
    "start_protocol": "Start a selected protocol workflow.",
    "stop_protocol": "Stop the currently running protocol.",
    "next_step": "Advance protocol to the next step.",
    "previous_step": "Return to the previous protocol step.",
    "go_to_step": "Jump to a specific protocol step.",
    "restart_protocol": "Restart the active protocol from step one.",
    "clear_error": "Clear current protocol error state and continue.",
    "query_stella": "Ask STELLA vision model about current scene.",
    "get_protocol_status": "Get current protocol status summary.",
    "query_completed_protocol_data": "Query captured data from completed protocol runs in this session.",
    "show_experiment_data": "Show details from a captured experiment-data section.",
    "get_errors": "Report errors from the current or last protocol run.",
    "detailed_step": "Show expanded step details with image on AR display.",
    "web_search": "Search the web for current information.",
    "image_search": "Search for images and display on XR.",
    "run_code": "Execute sandboxed Python code.",
    "get_datetime": "Get current date and time information.",
    "summarize_history": "Summarize conversation history text.",
    "update_user": "Send spoken progress update via TTS.",
    "send_to_display": "Render custom content on XR display.",
    "show_protocol_panel": "Return XR display to protocol panel.",
}


def get_tool_catalog() -> list[dict]:
    state = get_all_tool_enabled()
    tools = []
    for tool in ALL_TOOLS:
        name = getattr(tool, "name", str(tool))
        tools.append(
            {
                "name": name,
                "enabled": bool(state.get(name, True)),
                "description": TOOL_DESCRIPTIONS.get(name, ""),
                "keywords": [],
            }
        )
    tools.sort(key=lambda t: t["name"])
    return tools


class LabOSRunHooks(RunHooks):
    """Push tool_call WS messages and optional TTS for notable tools."""

    async def _send_tool_call(self, tool_name: str, status: str) -> None:
        """Send a tool_call message over the session WebSocket."""
        summary = TOOL_DESCRIPTIONS.get(tool_name, tool_name)
        try:
            from ws_handler import send_to_session
            from config import _current_session_id
            sid = _current_session_id.get("default-xr-session")
            if sid:
                await send_to_session(sid, {
                    "type": "tool_call",
                    "tool_name": tool_name,
                    "summary": summary,
                    "status": status,
                })
        except Exception as exc:
            logger.debug(f"[Hooks] Failed to send tool_call WS message: {exc}")

    async def on_tool_start(self, context, agent, tool: Tool) -> None:
        await self._send_tool_call(tool.name, "started")

        phrase = TOOL_NOTIFICATIONS.get(tool.name)
        if phrase:
            try:
                from tools.display.tts import push_tts
                await push_tts(phrase)
            except Exception as exc:
                logger.debug(f"[Hooks] Failed to push tool TTS notification: {exc}")

    async def on_tool_end(self, context, agent, tool: Tool, result) -> None:
        await self._send_tool_call(tool.name, "completed")


def _dynamic_instructions(ctx, agent) -> str:
    """Build the system prompt dynamically based on current protocol state."""
    state = get_protocol_state()
    cm = get_context_manager()
    return cm.build_system_prompt(state)


def create_agent(config: Dict[str, Any]) -> Agent:
    """Create the main LabOS Agent from the NAT config."""
    llm_cfg = config.get("llms", {}).get("router", {})
    base_url = llm_cfg.get("base_url", "http://localhost:8001/v1")
    model_name = llm_cfg.get("model", "Qwen/Qwen3-32B-AWQ")
    api_key = llm_cfg.get("api_key", "not-needed")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    set_tracing_disabled(True)

    return Agent(
        name="LabOS Assistant",
        instructions=_dynamic_instructions,
        model=OpenAIChatCompletionsModel(
            model=model_name,
            openai_client=client,
        ),
        tools=ALL_TOOLS,
        model_settings=ModelSettings(
            temperature=0.7,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        ),
    )


def create_hooks(config: Dict[str, Any]) -> LabOSRunHooks:
    """Create the RunHooks instance for tool notifications."""
    return LabOSRunHooks()
