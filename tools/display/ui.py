"""Viture XR panel rendering and display tool.

All panel updates flow through this module so that display logic is
consistent and testable.  The error display auto-reverts after 10 seconds.
Also exposes the ``send_to_display`` and ``show_protocol_panel``
@function_tools.

NOTE: The XR display is a mobile-like screen (~480px wide). Content should
be laid out vertically with short text blocks and small images. Avoid wide
layouts or long horizontal text. Images should be resized to fit the narrow
viewport. When the LLM generates rich-text for this display, keep it
concise and vertically stacked.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from agents import function_tool
from loguru import logger
from pydantic import Field
from typing import Annotated

from config import _current_session_id
from tools.common.toggle import toggle_dashboard

ERROR_DISPLAY_SECONDS = 10.0

# ---------------------------------------------------------------------------
# Display mode tracking (per-session)
# ---------------------------------------------------------------------------

_display_modes: Dict[str, str] = {}


def get_display_mode() -> str:
    from config import _current_session_id
    sid = _current_session_id.get("default-xr-session")
    return _display_modes.get(sid, "protocol")


def set_display_mode(mode: str) -> None:
    from config import _current_session_id
    sid = _current_session_id.get("default-xr-session")
    _display_modes[sid] = mode


# ---------------------------------------------------------------------------
# Low-level panel push
# ---------------------------------------------------------------------------

async def _push_panel(messages: List[Dict[str, str]]):
    """Push a panel update to the XR runtime via WebSocket."""
    from ws_handler import send_to_session
    sid = _current_session_id.get("default-xr-session")
    await send_to_session(sid, {
        "type": "display_update",
        "message_type": "SINGLE_STEP_PANEL_CONTENT",
        "payload": json.dumps({"messages": messages}),
    })


# ---------------------------------------------------------------------------
# Rich mixed-content panel
# ---------------------------------------------------------------------------

async def render_rich_panel(blocks: List[Dict[str, str]]) -> None:
    """Push a mixed-content panel to the XR display.

    Each block is a dict with:
      - {"type": "rich-text",     "content": "<TMP rich-text string>"}
      - {"type": "base64-image",  "content": "<base64-encoded image data>"}
    """
    set_display_mode("overlay")
    await _push_panel(blocks)


# ---------------------------------------------------------------------------
# Panel renderers
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "completed": "\u2713",  # checkmark
    "error":     "\u2717",  # x-mark
    "in_progress": "\u2014",  # em-dash
    "pending":   " ",
}
_STATUS_COLORS = {
    "completed": "#88CC88",
    "error":     "#FF4444",
    "in_progress": "#FFB347",
    "pending":   "#888888",
}


def _build_step_panel_content(state) -> str:
    parts: List[str] = []
    steps = state.steps
    total = len(steps)

    parts.append(
        f'<size=22><color=#59D2FF><b>Step {state.current_step}/{total}: '
        f'{state.protocol_name}</b></color></size><br><br>'
    )

    for i, step in enumerate(steps):
        num = i + 1
        icon = _STATUS_ICONS.get(step.status, " ")
        color = _STATUS_COLORS.get(step.status, "#888888")
        label = f"{icon} Step {num}: {step.text}"
        if step.status == "error" and step.error_detail:
            label += f" ({step.error_detail[:60]})"
        size = 18 if step.status == "in_progress" else 16
        parts.append(f'<size={size}><color={color}>{label}</color></size><br>')

    current = state.current_step_detail()
    if current and current.description:
        desc = current.description[:300]
        parts.append(
            f'<br><size=16><color=#DDDDDD>{desc}</color></size>'
        )

    if state.stella_vision_text:
        parts.append(
            f'<br><size=14><color=#59D2FF>STELLA: '
            f'{state.stella_vision_text}</color></size>'
        )

    parts.append(
        '<br><br><size=13><color=#999999>'
        '"more details" \u2022 "log data" \u2022 "next step" \u2022 "what errors?"'
        '</color></size>'
    )

    return "".join(parts)


async def render_step_panel(state) -> None:
    set_display_mode("protocol")
    content = _build_step_panel_content(state)
    await _push_panel([{"type": "rich-text", "content": content}])


async def render_error(state, error_msg: str) -> None:
    idx = state.current_step - 1
    step_text = state.steps[idx].text if 0 <= idx < len(state.steps) else ""

    state.error_display_until = time.time() + ERROR_DISPLAY_SECONDS

    parts: List[str] = [
        f'<size=22><color=#FF4444>Error Detected!</color></size><br><br>',
        f'<size=20><color=#FF4444><s>{step_text}</s></color></size>',
    ]
    if error_msg:
        parts.append(f'<br><size=18><color=#FF4444>{error_msg}</color></size>')
    parts.append(
        '<br><br><size=16><color=#D9D8FF>'
        "Say 'clear' or 'continue' to move on.</color></size>"
    )

    await _push_panel([{"type": "rich-text", "content": "".join(parts)}])

    async def _auto_revert():
        await asyncio.sleep(ERROR_DISPLAY_SECONDS)
        if state.is_active and time.time() >= state.error_display_until:
            await render_step_panel(state)
    try:
        asyncio.get_event_loop().create_task(_auto_revert())
    except RuntimeError:
        pass


async def render_greeting() -> None:
    content = (
        '<size=22><b>LabOS Protocol Assistant</b></size><br><br>'
        "<size=20>Say <color=#D9D8FF>'List Protocols'</color> "
        "to see available experiments. Or ask me any question you like!</size>"
    )
    set_display_mode("protocol")
    await _push_panel([{"type": "rich-text", "content": content}])


async def render_protocol_list(store) -> None:
    set_display_mode("protocol")
    await _push_panel(store.format_protocol_list_for_display())


async def render_completion(protocol_name: str, rich_summary: str = "") -> None:
    content = (
        f'<size=25><color=#59D2FF>Protocol Completed!</color></size><br><br>'
        f'<size=20>You have completed the {protocol_name} protocol!</size><br><br>'
    )
    if rich_summary:
        content += f"{rich_summary}<br><br>"
    content += (
        "<size=18>Data saved. You can ask about this completed protocol data. "
        "Say <color=#D9D8FF>'List Protocols'</color> to run another.</size>"
    )
    set_display_mode("protocol")
    await _push_panel([{"type": "rich-text", "content": content}])


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

@function_tool
@toggle_dashboard("send_to_display")
async def send_to_display(
    content: Annotated[str, Field(
        description="Rich-text content using Unity TextMeshPro tags. "
        "Supported tags: <size=N>, <color=#HEX>, <b>, <i>, <u>, <s>, "
        "<br>, <sup>, <sub>, <mark=#HEX>, <align=left|center|right>. "
        "Keep it concise and vertical -- the display is ~480px wide."
    )],
    title: Annotated[str, Field(description="Optional title for the panel")] = "",
    image_base64: Annotated[Optional[str], Field(
        description="Optional base64-encoded image to display above the text. "
        "Pass the raw base64 string (no data-URI prefix). "
        "Do NOT use HTML <img> tags -- they are not supported."
    )] = None,
) -> str:
    """Update the main AR display panel with rich text and optionally an image.
    This sets the display to 'overlay' mode (non-protocol content). Use
    show_protocol_panel to return to the protocol step view when done.

    The display is a narrow mobile-like screen (~480px). Use TMP rich-text
    tags for formatting: <size>, <color>, <b>, <i>, <u>, <br>, <sup>, <sub>,
    <mark>, <align>. Do NOT use HTML tags like <img> or <a>.

    For images, pass raw base64 via the image_base64 parameter -- the image
    is shown as a separate block above the text content."""
    blocks: List[Dict[str, str]] = []

    if image_base64:
        blocks.append({"type": "base64-image", "content": image_base64})

    panel_content = content
    if title:
        panel_content = f"<size=22><b>{title}</b></size><br><br>{content}"
    blocks.append({"type": "rich-text", "content": panel_content})

    try:
        await render_rich_panel(blocks)
    except Exception:
        return "Failed to update display."
    return "Content displayed on XR panel. Call show_protocol_panel to return to protocol view."


@function_tool
@toggle_dashboard("show_protocol_panel")
async def show_protocol_panel() -> str:
    """Restore the protocol step display on the AR panel. Use when the user
    says 'show steps', 'back to protocol', or is done viewing other content
    like web search results or images."""
    from tools.protocols.state import get_protocol_state

    state = get_protocol_state()
    if state.mode == "running" and state.steps:
        await render_step_panel(state)
        return "Protocol step panel restored."
    else:
        await render_greeting()
        return "No active protocol. Showing greeting panel."
