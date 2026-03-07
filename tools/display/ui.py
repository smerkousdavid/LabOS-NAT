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

async def _push_panel(messages: List[Dict[str, str]], session_id: Optional[str] = None):
    """Push a panel update to the XR runtime via WebSocket."""
    from ws_handler import send_to_session
    sid = session_id or _current_session_id.get("default-xr-session")
    await send_to_session(sid, {
        "type": "display_update",
        "message_type": "SINGLE_STEP_PANEL_CONTENT",
        "payload": json.dumps({"messages": messages}),
    })


# ---------------------------------------------------------------------------
# Rich mixed-content panel
# ---------------------------------------------------------------------------

async def render_rich_panel(blocks: List[Dict[str, str]], session_id: Optional[str] = None) -> None:
    """Push a mixed-content panel to the XR display.

    Each block is a dict with:
      - {"type": "rich-text",     "content": "<TMP rich-text string>"}
      - {"type": "base64-image",  "content": "<base64-encoded image data>"}
    """
    set_display_mode("overlay")
    await _push_panel(blocks, session_id=session_id)


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


_HIDE_NEXT_STEP_AT_CHARS = 72
_HIDE_PREV_STEP_AT_CHARS = 108


def _clip_to_sentences(text: str, max_sentences: int = 3) -> str:
    """Return first N sentences of text."""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return " ".join(sentences[:max_sentences])


def _is_valid_base64_payload(data: str) -> bool:
    """Quick check that a string looks like valid base64 image data."""
    if not data or len(data) < 200:
        return False
    import re
    return bool(re.match(r'^[A-Za-z0-9+/=\s]+$', data[:200]))


def _strip_urls_for_display(text: str) -> str:
    """Remove any leftover URLs from display text as a safety net."""
    import re
    text = re.sub(r'\s*\[https?://[^\]]*\]\s*', ' ', text)
    text = re.sub(r'\s*\((?:Source:\s*)?https?://\S+?\)\s*', ' ', text)
    text = re.sub(r'https?://\S+', '', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def _get_current_step_display_text(step) -> str:
    """Return source-faithful text for the current step (up to ~3 sentences)."""
    if step.description and len(step.description) > len(step.text):
        return _strip_urls_for_display(_clip_to_sentences(step.description, 3))
    return _strip_urls_for_display(step.text)


def _get_step_window(state, has_image: bool) -> tuple:
    """Return (win_start, win_end) indices for visible steps."""
    steps = state.steps
    total = len(steps)
    cur = state.current_step  # 1-based

    if has_image:
        current = state.current_step_detail()
        current_text_len = len(_get_current_step_display_text(current)) if current else 0
        before_count = 1
        after_count = 1
        if current_text_len > _HIDE_NEXT_STEP_AT_CHARS:
            after_count = 0
        if current_text_len > _HIDE_PREV_STEP_AT_CHARS:
            before_count = 0
        win_start = max(0, cur - 1 - before_count)
        win_end = min(total, cur + after_count)
        return win_start, win_end

    radius = _WINDOW_RADIUS
    win_start = max(0, cur - 1 - radius)
    win_end = min(total, cur + radius)
    return win_start, win_end


_WINDOW_RADIUS = 2  # show 2 before + current + 2 after = 5 visible


def _build_step_panel_content(state) -> list:
    """Build panel blocks: list of dicts with type rich-text or base64-image."""
    parts: List[str] = []
    steps = state.steps
    total = len(steps)
    cur = state.current_step  # 1-based

    current = state.current_step_detail()
    has_image = bool(current and current.image_base64 and _is_valid_base64_payload(current.image_base64))

    parts.append(
        f'<size=22><color=#59D2FF><b>Step {cur}/{total}: '
        f'{state.protocol_name}</b></color></size><br><br>'
    )

    win_start, win_end = _get_step_window(state, has_image)

    if win_start > 0:
        parts.append(
            f'<size=14><color=#CC7722>... {win_start} more above</color></size><br>'
        )

    for i in range(win_start, win_end):
        step = steps[i]
        num = i + 1
        icon = _STATUS_ICONS.get(step.status, " ")
        color = _STATUS_COLORS.get(step.status, "#888888")

        if num == cur:
            label = f"{icon} Step {num}: {_get_current_step_display_text(step)}"
        else:
            label = f"{icon} Step {num}: {_strip_urls_for_display(step.text)}"

        if step.status == "error" and step.error_detail:
            label += f" ({step.error_detail[:60]})"
        size = 18 if step.status == "in_progress" else 16
        parts.append(f'<size={size}><color={color}>{label}</color></size><br>')

    remaining_below = total - win_end
    if remaining_below > 0:
        parts.append(
            f'<size=14><color=#CC7722>... {remaining_below} more below</color></size><br>'
        )

    # Build blocks list
    blocks: list = [{"type": "rich-text", "content": "".join(parts)}]

    # Image block (between steps and details)
    if has_image:
        blocks.append({"type": "base64-image", "content": current.image_base64})

    # Details + STELLA block
    detail_parts: List[str] = []
    if current and current.description:
        clean_desc = _strip_urls_for_display(current.description)
        if clean_desc:
            detail_parts.append(
                f'<br><size=16><color=#DDDDDD>{clean_desc}</color></size>'
            )

    if state.stella_vision_text:
        detail_parts.append(
            f'<br><size=14><color=#59D2FF>STELLA: '
            f'{state.stella_vision_text}</color></size>'
        )

    detail_parts.append(
        '<br><br><size=13><color=#999999>'
        '"next step" \u2022 "more details" \u2022 "log data" \u2022 "how to use [tool]?"'
        '</color></size>'
    )

    if detail_parts:
        blocks.append({"type": "rich-text", "content": "".join(detail_parts)})

    return blocks


async def render_step_panel(state, session_id: Optional[str] = None) -> None:
    set_display_mode("protocol")
    blocks = _build_step_panel_content(state)
    await _push_panel(blocks, session_id=session_id)


async def render_error(state, error_msg: str, session_id: Optional[str] = None) -> None:
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
        "Say 'next step', 'clear', or 'continue' to move on.</color></size>"
    )

    await _push_panel([{"type": "rich-text", "content": "".join(parts)}], session_id=session_id)

    async def _auto_revert():
        await asyncio.sleep(ERROR_DISPLAY_SECONDS)
        if state.is_active and time.time() >= state.error_display_until:
            await render_step_panel(state, session_id=session_id)
    try:
        asyncio.get_event_loop().create_task(_auto_revert())
    except RuntimeError:
        pass


async def render_greeting(session_id: Optional[str] = None) -> None:
    content = (
        '<size=22><b>LabOS Protocol Assistant</b></size><br><br>'
        '<size=17>Say <color=#D9D8FF>"Hey Stella"</color> then try:</size><br><br>'

        '<size=15><color=#FFB347>NAVIGATE</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"list protocols" \u2022 "start protocol" \u2022 "next step"<br>'
        '"previous step" \u2022 "stop protocol"'
        '</color></size><br><br>'

        '<size=15><color=#FFB347>ASK ABOUT STEPS</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"give me more details" \u2022 "explain this step"'
        '</color></size><br><br>'

        '<size=15><color=#FFB347>EQUIPMENT HELP</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"how do I use a pipette?" \u2022 "how do I use you?"'
        '</color></size><br><br>'

        '<size=15><color=#FFB347>LOG DATA / OBSERVATIONS</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"log that tube 1 weighs 5 grams"<br>'
        '"note that colonies look dead"<br>'
        '"my cell culture in dish 3 looks like nothing grew"'
        '</color></size><br><br>'

        '<size=13><color=#999999>'
        'To learn how to use Stella, say "Stella, how do I use you?"'
        '</color></size>'
    )
    set_display_mode("protocol")
    await _push_panel([{"type": "rich-text", "content": content}], session_id=session_id)


async def render_protocol_list(store, state=None, session_id: Optional[str] = None) -> None:
    set_display_mode("protocol")
    if state is None:
        await _push_panel(store.format_protocol_list_for_display(), session_id=session_id)
        return
    from tools.protocols.store import format_protocols_for_display

    await _push_panel(format_protocols_for_display(store, state), session_id=session_id)


async def render_completion(protocol_name: str, rich_summary: str = "", session_id: Optional[str] = None) -> None:
    if rich_summary:
        content = rich_summary
    else:
        content = (
            f'<size=22><color=#59D2FF><b>Protocol Summary</b></color></size><br>'
            f'<size=18><b>{protocol_name}</b></size><br><br>'
            f'<size=16><color=#DDDDDD>Protocol completed.</color></size><br><br>'
            f'<size=14><color=#999999>You can ask about observations, errors, or data. '
            f'Returning to main menu in 1 minute.</color></size>'
        )
    set_display_mode("protocol")
    await _push_panel([{"type": "rich-text", "content": content}], session_id=session_id)


async def render_qr_scanning(session_id: Optional[str] = None) -> None:
    """Show QR code scanning prompt with camera preview placeholder."""
    content = (
        '<size=22><b>LabOS Protocol Assistant</b></size><br><br>'
        '<size=18><color=#59D2FF>Scan QR Code</color></size><br><br>'
        '<size=16><color=#DDDDDD>'
        'To get started, please point your XR glasses at '
        'the QR code on the screen.'
        '</color></size><br><br>'
        '<size=14><color=#999999>'
        'The QR code is displayed on the LabOS web dashboard.'
        '</color></size>'
    )
    set_display_mode("protocol")
    await _push_panel([{"type": "rich-text", "content": content}], session_id=session_id)


async def render_qr_preview(image_b64: str, session_id: Optional[str] = None) -> None:
    """Show QR scanning prompt with a live camera preview image."""
    text = (
        '<size=16><color=#59D2FF>Point at the QR code on screen</color></size>'
    )
    set_display_mode("protocol")
    await _push_panel([
        {"type": "base64-image", "content": image_b64},
        {"type": "rich-text", "content": text},
    ], session_id=session_id)


async def render_connecting(session_id: str = "", target_session_id: Optional[str] = None) -> None:
    """Show connecting state after QR code is scanned."""
    content = (
        '<size=22><b>LabOS Protocol Assistant</b></size><br><br>'
        '<size=18><color=#FFB347>Connecting to session...</color></size><br><br>'
        '<size=14><color=#999999>'
        f'{session_id[:16] + "..." if len(session_id) > 16 else session_id}'
        '</color></size>'
    )
    set_display_mode("protocol")
    await _push_panel([{"type": "rich-text", "content": content}], session_id=target_session_id)


async def render_available_commands(session_id: Optional[str] = None) -> None:
    """Show available voice commands on the XR display."""
    from tools.protocols.state import get_protocol_state
    state = get_protocol_state()

    content = (
        '<size=22><b>Available Commands</b></size><br><br>'

        '<size=15><color=#FFB347>NAVIGATE</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"next step" \u2022 "previous step" \u2022 "go to step 3"<br>'
        '"stop protocol" \u2022 "restart protocol" \u2022 "list protocols"'
        '</color></size><br><br>'

        '<size=15><color=#FFB347>ASK ABOUT STEPS</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"give me more details" \u2022 "explain this step"'
        '</color></size><br><br>'

        '<size=15><color=#FFB347>EQUIPMENT HELP</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"how do I use a pipette?" \u2022 "how do I use a centrifuge?"'
        '</color></size><br><br>'

        '<size=15><color=#FFB347>LOG DATA</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"log that tube 1 weighs 5 grams" \u2022 "note colonies look dead"'
        '</color></size><br><br>'

        '<size=15><color=#FFB347>OTHER</color></size><br>'
        '<size=14><color=#D9D8FF>'
        '"show me an image of..." \u2022 "reset session" \u2022 "create a protocol"'
        '</color></size>'
    )

    if state.mode == "running" and state.is_active:
        content += (
            '<br><br><size=16><color=#59D2FF>'
            'Say "Stella, back to protocol" to return to your running protocol.'
            '</color></size>'
        )

    set_display_mode("overlay")
    await _push_panel([{"type": "rich-text", "content": content}], session_id=session_id)


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
