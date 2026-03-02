"""Conversation session storage.

Manages per-session conversation item lists for the OpenAI Agents SDK
Runner, and provides the ``strip_reasoning`` helper to clean up LLM
output before it reaches TTS.

Includes context-window protection: tool outputs are truncated, base64
data is replaced with placeholders, and total token budget is enforced
both before items are sent to the agent *and* when saving history.
"""

import json
import re
from typing import Any, Dict

from tools.common.history_summary import summarize_items_for_memory

_sessions: Dict[str, list] = {}

_BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")

# Overhead estimate for system prompt (~1000 tokens) + 17 tool definitions
# (~1500 tokens).  The history budget is max_model_len minus this overhead.
_DEFAULT_OVERHEAD_TOKENS = 3000

# Fallback budget when max_model_len is not configured.
TOKEN_BUDGET = 12000

TOOL_OUTPUT_MAX_CHARS = 160
TOOL_ARGS_MAX_CHARS = 320
USER_MESSAGE_MAX_CHARS = 1200
ASSISTANT_MESSAGE_MAX_CHARS = 900
SYSTEM_MESSAGE_MAX_CHARS = 1200
RESERVED_REPLY_TOKENS = 700
SUMMARIZE_TRIGGER_TOKENS = 7000
SUMMARY_TARGET_TOKENS = 600
SUMMARY_PREFIX = "Conversation memory summary:"
_CHARS_PER_TOKEN_ESTIMATE = 3


def configure_budget(
    max_model_len: int = 16384,
    summarize_trigger_tokens: int = 7000,
    summary_target_tokens: int = 600,
):
    """Set history and summarization budgets from model context settings."""
    global TOKEN_BUDGET, SUMMARIZE_TRIGGER_TOKENS, SUMMARY_TARGET_TOKENS
    # Be stricter for 4k-context models; tool schemas/system prompt consume a lot.
    overhead_tokens = 3400 if max_model_len <= 4096 else _DEFAULT_OVERHEAD_TOKENS
    TOKEN_BUDGET = max(600, max_model_len - overhead_tokens)
    SUMMARIZE_TRIGGER_TOKENS = max(1000, min(summarize_trigger_tokens, TOKEN_BUDGET))
    SUMMARY_TARGET_TOKENS = max(120, summary_target_tokens)


def _estimate_tokens(items: list) -> int:
    # Conservative estimate to avoid near-limit requests.
    return len(json.dumps(items, default=str)) // _CHARS_PER_TOKEN_ESTIMATE


def _sanitize_text(text: str) -> str:
    """Replace long base64 strings with a placeholder."""
    if not text or len(text) < 150:
        return text
    return _BASE64_PATTERN.sub("<image: displayed on XR panel>", text)


def _clip_text(text: str, max_chars: int) -> str:
    """Clip text fields to keep prompt payload compact."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "... [truncated]"


def _content_to_text(content: Any) -> str:
    """Convert structured content blocks into compact plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return "\n".join(p for p in parts if p).strip()
    return ""


def _normalize_item(item: dict) -> dict:
    """Keep only compact fields required for useful memory."""
    if not isinstance(item, dict):
        return {}

    item_type = item.get("type")
    if item_type == "function_call":
        return {
            "type": "function_call",
            "name": item.get("name", ""),
            "call_id": item.get("call_id", ""),
            "arguments": item.get("arguments", ""),
        }
    if item_type == "function_call_output":
        return {
            "type": "function_call_output",
            "call_id": item.get("call_id", ""),
            "output": item.get("output", ""),
        }

    role = item.get("role")
    if role in ("system", "assistant", "user"):
        return {"role": role, "content": item.get("content", "")}

    return item


def _sanitize_items(items: list) -> list:
    """Walk items and compact tool outputs + strip base64 data."""
    compacted: list = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item = _normalize_item(item)
        if not item:
            continue

        if item.get("type") == "function_call_output":
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, default=str)
            output = _sanitize_text(output)
            item["output"] = _clip_text(output, TOOL_OUTPUT_MAX_CHARS)

        if item.get("type") == "function_call":
            args = item.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False, default=str)
            item["arguments"] = _clip_text(_sanitize_text(args), TOOL_ARGS_MAX_CHARS)

        if item.get("role") in ("assistant", "user"):
            content = _content_to_text(item.get("content"))
            content = _sanitize_text(content)
            max_chars = ASSISTANT_MESSAGE_MAX_CHARS if item.get("role") == "assistant" else USER_MESSAGE_MAX_CHARS
            item["content"] = _clip_text(content, max_chars)

        if item.get("role") == "system":
            content = _clip_text(_sanitize_text(_content_to_text(item.get("content"))), SYSTEM_MESSAGE_MAX_CHARS)
            item["content"] = content

        compacted.append(item)

    return compacted


def _trim_to_budget(items: list, budget: int) -> list:
    """Drop oldest turns until estimated tokens are within budget.

    Drops in groups that keep function_call / function_call_output pairs
    together.  Falls back to dropping 2 items at a time when no
    structured boundaries are found.
    """
    while _estimate_tokens(items) > budget and len(items) > 2:
        drop = _find_turn_boundary(items)
        items = items[drop:]
    return items


def _is_summary_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("role") != "system":
        return False
    content = item.get("content")
    return isinstance(content, str) and content.startswith(SUMMARY_PREFIX)


def _summarize_if_needed(items: list, budget: int) -> list:
    """Compact old history into a summary block before hard trimming.

    This keeps recent turns verbatim while turning older turns into a compact
    memory note once history grows beyond the summarize trigger.
    """
    if len(items) < 6:
        return items
    if _estimate_tokens(items) <= SUMMARIZE_TRIGGER_TOKENS:
        return items

    # Keep roughly the newest 35% of turns untouched.
    keep_from = max(2, int(len(items) * 0.65))
    if keep_from >= len(items) - 1:
        return items
    keep_from = min(keep_from + _find_turn_boundary(items[keep_from:]), len(items) - 1)

    older = [it for it in items[:keep_from] if isinstance(it, dict) and not _is_summary_item(it)]
    recent = items[keep_from:]
    if not older or not recent:
        return items

    summary = summarize_items_for_memory(older, target_tokens=SUMMARY_TARGET_TOKENS)
    if not summary:
        return items

    summary_item = {"role": "system", "content": f"{SUMMARY_PREFIX}\n{summary}"}
    compacted = [summary_item] + recent

    # If still over trigger, aggressively keep a shorter recent tail.
    while _estimate_tokens(compacted) > SUMMARIZE_TRIGGER_TOKENS and len(compacted) > 5:
        compacted = [compacted[0]] + compacted[2:]

    if _estimate_tokens(compacted) > budget:
        compacted = _trim_to_budget(compacted, budget)
    return compacted


def _find_turn_boundary(items: list) -> int:
    """Return the number of leading items that form one complete turn.

    A "turn" is either:
      - a user message (1 item)
      - an assistant message possibly followed by tool call/output pairs
      - a function_call + function_call_output pair
    Falls back to 2 items.
    """
    if not items:
        return 1
    first = items[0]
    if not isinstance(first, dict):
        return 1

    if first.get("role") == "user":
        return 1

    # Assistant message + any following tool-call/output pairs
    i = 1
    while i < len(items):
        item = items[i]
        if not isinstance(item, dict):
            break
        t = item.get("type", "")
        if t in ("function_call", "function_call_output"):
            i += 1
            continue
        break
    return max(i, 2)


def prepare_input(prev_items: list, user_message: str) -> list:
    """Build and trim the input list before sending to the agent.

    This is the critical trimming point -- ensures the history + new user
    message fits within the model's context window *before* the agent run.
    """
    items = list(prev_items)
    items = _sanitize_items(items)
    sanitized_user = _clip_text(_sanitize_text(user_message or ""), USER_MESSAGE_MAX_CHARS)

    # Reserve room for this user turn + the model's reply/tool calls.
    user_tokens = _estimate_tokens([{"role": "user", "content": sanitized_user}])
    history_budget = max(1000, TOKEN_BUDGET - user_tokens - RESERVED_REPLY_TOKENS)
    items = _summarize_if_needed(items, history_budget)
    items = _trim_to_budget(items, history_budget)

    combined = items + [{"role": "user", "content": sanitized_user}]
    if _estimate_tokens(combined) > TOKEN_BUDGET:
        combined = _trim_to_budget(combined, TOKEN_BUDGET)
    return combined



def get_session_items(session_id: str) -> list:
    return _sessions.get(session_id, [])


def save_session_items(session_id: str, items: list, history_limit: int = 40):
    if len(items) > history_limit:
        items = items[-history_limit:]
    items = _sanitize_items(items)
    items = _summarize_if_needed(items, TOKEN_BUDGET)
    items = _trim_to_budget(items, TOKEN_BUDGET)
    _sessions[session_id] = items


def clear_session(session_id: str) -> bool:
    """Remove a session.  Returns True if it existed."""
    if session_id in _sessions:
        del _sessions[session_id]
        return True
    return False


def strip_reasoning(text: str) -> str:
    """Strip ``<think>``/``<reasoning>`` blocks that shouldn't reach TTS."""
    if not text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\n\s*\n", "\n", text)
    return text.strip()
