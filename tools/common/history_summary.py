"""Conversation history summarization helpers and tool.

The helpers here are used by session compaction to reduce old history into
short memory blocks when context usage grows too large.
"""

from __future__ import annotations

from typing import Iterable, List

from agents import function_tool
from pydantic import Field
from typing import Annotated
from tools.common.toggle import toggle_dashboard


def _normalize_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: List[str] = []
        for block in value:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return " ".join(parts).strip()
    return ""


def summarize_items_for_memory(items: Iterable[dict], target_tokens: int = 600) -> str:
    """Create a compact summary string from history items.

    This is intentionally deterministic and lightweight so it can run inline
    during request handling without extra model latency.
    """
    if target_tokens < 120:
        target_tokens = 120
    char_budget = target_tokens * 4

    lines: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        item_type = item.get("type")

        if role in ("user", "assistant"):
            text = _normalize_text(item.get("content"))
            if text:
                speaker = "User" if role == "user" else "Assistant"
                lines.append(f"- {speaker}: {text}")
            continue

        if item_type == "function_call":
            name = item.get("name", "tool")
            args = _normalize_text(item.get("arguments", ""))
            if args:
                lines.append(f"- ToolCall `{name}` args: {args[:220]}")
            else:
                lines.append(f"- ToolCall `{name}`")
            continue

        if item_type == "function_call_output":
            out = _normalize_text(item.get("output", ""))
            if out:
                lines.append(f"- ToolResult: {out[:240]}")

    if not lines:
        return "No prior conversation details were available."

    # Bias toward recency by keeping the last lines that fit.
    selected: List[str] = []
    used = 0
    for line in reversed(lines):
        add = len(line) + 1
        if used + add > char_budget:
            break
        selected.append(line)
        used += add
    selected.reverse()

    summary = "Key conversation memory:\n" + "\n".join(selected)
    if len(summary) > char_budget:
        summary = summary[: char_budget - 3] + "..."
    return summary


@function_tool
@toggle_dashboard("summarize_history")
async def summarize_history(
    history: Annotated[str, Field(description="Conversation history text to summarize.")],
    target_tokens: Annotated[int, Field(description="Approximate summary token target.", ge=120, le=1200)] = 600,
) -> str:
    """Summarize conversation history into a compact memory block."""
    fake_item = {"role": "user", "content": history}
    return summarize_items_for_memory([fake_item], target_tokens=target_tokens)
