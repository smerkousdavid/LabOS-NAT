"""Protocol management tools.

Owns the ProtocolState lifecycle, STELLA monitor loop start/stop, and
exposes @function_tool functions for listing, starting, stopping,
navigating, and clearing errors.
"""

import hashlib
import time
from datetime import datetime
from typing import Annotated, Optional

from agents import function_tool
from loguru import logger
from pydantic import Field

from tools.protocols.state import ProtocolState, StepDetail, get_protocol_state
from tools.protocols.store import get_protocol_store
from tools.vsop_providers import get_vsop_provider, init_vsop_provider
from tools.display import ui as viture_ui
from context.manager import get_context_manager
from tools.common.toggle import toggle_dashboard

STEP_ENRICHMENT_PROMPT = """\
You are a lab protocol expert writing concise AR guidance.
For each step below, return:
1. A concise instruction (1-2 short sentences, practical only).
2. A short list of common mistakes (1-2 items max).

Protocol: {protocol_name}

Steps:
{steps_block}

Rules:
- Keep each description under 180 characters when possible.
- Use direct action language.
- Do NOT add generic filler like "make sure your workspace is clean" unless required by the specific step.
- Prefer concrete specifics (what to pick up, what to record, where to place).

Examples:
- Step text: "Weight 5 PCR tubes"
  description: "Take 5 empty PCR tubes and record each tube's weight."
  common_errors: ["Skipping one tube", "Recording weights without tube labels"]
- Step text: "Place tubes in thermocycler"
  description: "Place all tubes in the thermocycler block and close the lid fully."
  common_errors: ["Leaving lid partially open"]

Reply in EXACTLY this JSON format (no markdown, no extra text):
[
  {{
    "description": "concise step guidance",
    "common_errors": ["mistake 1"]
  }},
  ...
]
One entry per step, in order.\
"""

PROTOCOL_COMPACTION_PROMPT = """\
You are a laboratory protocol compiler. Convert the source protocol file into:
1) A compact executable step list for real-time AR guidance.
2) Concise protocol context that helps safety, setup, and troubleshooting.

Protocol name: {protocol_name}

Source protocol file:
{raw_protocol}

Rules:
- Keep step text short and action-oriented (5-18 words each).
- Merge verbose subtext into a single compact step when possible.
- Preserve order and critical constraints (volumes, times, temperatures, counts).
- Exclude decorative or repeated prose.
- Keep 3-20 steps total.
- The source may use XML/HTML tags -- extract only the actionable steps and context, never include markup tags in the output.

Return ONLY valid JSON, no markdown:
{{
  "steps": ["..."],
  "context": {{
    "goal": "one sentence",
    "materials": ["..."],
    "critical_parameters": ["..."],
    "reaction_mix": ["..."],
    "safety_notes": ["..."],
    "quality_checks": ["..."],
    "notes": ["..."]
  }}
}}\
"""

ERROR_COOLDOWN_SECONDS = 5.0
_ORDINAL_TO_NUM = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}

EXPERIMENT_DATA_PARSE_PROMPT = """\
Extract structured experiment data from this protocol utterance.

Current protocol: {protocol_name}
Current step: {current_step}
Step text: {step_text}
User utterance: {utterance}

Return ONLY JSON with this schema:
{{
  "captures": [
    {{
      "section": "tube_weights",
      "row": {{"tube": "3", "weight": "5.6g"}}
    }}
  ]
}}

Rules:
- If no data to capture, return {{"captures":[]}}.
- Keep values short and normalized.
- Prefer section names: tube_weights, timings, temperatures, volumes, notes.
"""

FINAL_DATA_RICHTEXT_PROMPT = """\
Format this completed protocol data as concise TextMeshPro rich text for a narrow AR panel.

Protocol: {protocol_name}
Completed at: {completed_at}
Data XML:
{experiment_data_xml}

Requirements:
- Output plain text with TMP tags only (<size>, <color>, <b>, <br>).
- Keep concise: <= 14 lines.
- Include a short title and key captured values.
- If tabular data exists, show compact CSV-like rows.
- No markdown.
"""


async def _enrich_steps_via_llm(protocol_name: str, step_texts: list) -> list:
    """Call the router LLM once to generate descriptions and common errors."""
    import asyncio, json, re
    from config import get_llm_client

    steps_block = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(step_texts))
    prompt = STEP_ENRICHMENT_PROMPT.format(
        protocol_name=protocol_name,
        steps_block=steps_block,
    )

    def _call_sync():
        client, model = get_llm_client("router")
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2048,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call_sync)
        raw = response.choices[0].message.content.strip()
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            if isinstance(data, list) and len(data) >= len(step_texts):
                return data[:len(step_texts)]
    except Exception as exc:
        logger.warning(f"Step enrichment LLM call failed: {exc}")

    return [{"description": "", "common_errors": []} for _ in step_texts]


def _build_protocol_context_text(context: dict) -> str:
    if not isinstance(context, dict):
        return ""

    lines = []
    goal = (context.get("goal") or "").strip()
    if goal:
        lines.append(f"Goal: {goal}")

    sections = [
        ("materials", "Materials"),
        ("critical_parameters", "Critical Parameters"),
        ("reaction_mix", "Reaction Mix"),
        ("safety_notes", "Safety Notes"),
        ("quality_checks", "Quality Checks"),
        ("notes", "Notes"),
    ]
    for key, title in sections:
        values = context.get(key) or []
        if isinstance(values, str):
            values = [values]
        values = [str(v).strip() for v in values if str(v).strip()]
        if not values:
            continue
        lines.append(f"{title}:")
        for value in values[:12]:
            lines.append(f"- {value}")
    text = "\n".join(lines).strip()
    if len(text) > 1400:
        text = text[:1400] + "... [truncated]"
    return text


async def _compact_protocol_via_llm(protocol_name: str, raw_protocol: str, fallback_steps: list) -> dict:
    """Compile source protocol text into compact steps + protocol-aware context."""
    import asyncio
    import json
    import re
    from config import get_llm_client

    raw = (raw_protocol or "").strip()
    if not raw:
        return {"steps": fallback_steps, "context_text": ""}

    clipped_raw = raw[:14000]
    prompt = PROTOCOL_COMPACTION_PROMPT.format(
        protocol_name=protocol_name,
        raw_protocol=clipped_raw,
    )

    def _call_sync():
        client, model = get_llm_client("router")
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1400,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    compact_steps = list(fallback_steps or [])
    context_text = ""
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call_sync)
        raw_reply = (response.choices[0].message.content or "").strip()
        match = re.search(r"\{.*\}", raw_reply, re.DOTALL)
        if match:
            data = json.loads(match.group())
            if isinstance(data, dict):
                steps = data.get("steps")
                if isinstance(steps, list):
                    cleaned = [str(s).strip() for s in steps if str(s).strip()]
                    if cleaned:
                        compact_steps = cleaned[:20]
                context_text = _build_protocol_context_text(data.get("context") or {})
    except Exception as exc:
        logger.warning(f"Protocol compaction LLM call failed: {exc}")

    if not compact_steps:
        compact_steps = ["Prepare protocol according to source document."]

    return {"steps": compact_steps, "context_text": context_text}


def _quick_compact_steps(step_texts: list[str]) -> list[str]:
    compact = []
    for step in step_texts:
        text = " ".join(str(step).split()).strip()
        if not text:
            continue
        if len(text) > 90:
            text = text[:90].rstrip() + "..."
        compact.append(text)
    return compact[:20]


def _init_experiment_data(state: ProtocolState) -> None:
    state.experiment_data = {
        "protocol_name": state.protocol_name,
        "started_at": datetime.utcnow().isoformat(timespec="seconds"),
        "sections": {},
    }
    state.data_capture_hashes = []


def _record_capture(state: ProtocolState, section: str, row: dict) -> bool:
    if not section:
        return False
    payload = f"{section}|{row}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    if digest in state.data_capture_hashes:
        return False

    state.data_capture_hashes.append(digest)
    sections = state.experiment_data.setdefault("sections", {})
    bucket = sections.setdefault(section, {"headers": [], "rows": []})

    if isinstance(row, dict):
        ordered_headers = list(row.keys())
        if not bucket["headers"]:
            bucket["headers"] = ordered_headers
        for h in ordered_headers:
            if h not in bucket["headers"]:
                bucket["headers"].append(h)
        bucket["rows"].append({k: str(v) for k, v in row.items()})
    else:
        bucket["rows"].append(str(row))
    return True


def _extract_tube_weight_regex(utterance: str) -> dict | None:
    import re

    text = utterance.lower()
    idx = None
    for word, num in _ORDINAL_TO_NUM.items():
        if word in text:
            idx = num
            break
    if idx is None:
        m = re.search(r"\btube\s*(\d+)\b", text)
        if m:
            idx = int(m.group(1))
    if idx is None:
        return None

    m_weight = re.search(r"(-?\d+(?:\.\d+)?)\s*(g|gram|grams|mg|kg)\b", text)
    if not m_weight:
        return None
    value = m_weight.group(1)
    unit = m_weight.group(2)
    unit = "g" if unit.startswith("gram") else unit
    return {
        "section": "tube_weights",
        "row": {"tube": str(idx), "weight": f"{value}{unit}"},
    }


async def _extract_with_llm(state: ProtocolState, utterance: str) -> list[dict]:
    import asyncio
    import json
    import re
    from config import get_llm_client

    current_text = ""
    if 1 <= state.current_step <= len(state.steps):
        current_text = state.steps[state.current_step - 1].text
    def _call_sync():
        client, model = get_llm_client("router")
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=240,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    try:
        prompt = EXPERIMENT_DATA_PARSE_PROMPT.format(
            protocol_name=state.protocol_name or "Unknown",
            current_step=state.current_step,
            step_text=current_text,
            utterance=utterance,
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call_sync)
        raw = (response.choices[0].message.content or "").strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return []
        parsed = json.loads(match.group())
        captures = parsed.get("captures", []) if isinstance(parsed, dict) else []
        return [c for c in captures if isinstance(c, dict)]
    except Exception as exc:
        logger.debug(f"Experiment data LLM parse skipped: {exc}")
        return []


async def auto_capture_experiment_data_from_utterance(utterance: str) -> tuple[bool, str]:
    """Try to capture protocol data from a user utterance during active protocol mode."""
    try:
        state = get_protocol_state()
        if state.mode != "running" or not state.is_active:
            return False, ""
        if not utterance or len(utterance.strip()) < 4:
            return False, ""

        captures: list[dict] = []
        regex_hit = _extract_tube_weight_regex(utterance)
        if regex_hit:
            captures.append(regex_hit)
        else:
            captures.extend(await _extract_with_llm(state, utterance))

        if not captures:
            return False, ""

        saved = 0
        for cap in captures:
            section = str(cap.get("section", "")).strip().lower().replace(" ", "_")
            row = cap.get("row", {})
            if _record_capture(state, section, row):
                saved += 1

        if saved <= 0:
            return False, ""

        state.extra_context = (state.extra_context or "").strip()
        if state.extra_context:
            state.extra_context += "\n\n"
        state.extra_context += state.experiment_data_xml()
        return True, "Data saved. I recorded that in the protocol notes."
    except Exception as exc:
        logger.warning(f"Experiment data capture failed open: {exc}")
        return False, ""


async def _refine_steps_background(
    protocol_name: str,
    raw_protocol: str,
    fallback_steps: list[str],
    state: ProtocolState,
    provider,
):
    """Refine step details asynchronously after protocol starts."""
    try:
        compacted = await _compact_protocol_via_llm(protocol_name, raw_protocol, fallback_steps)
        refined_steps = compacted["steps"] or fallback_steps
        if refined_steps:
            # Ask the manager LLM to sanity-check major drift in parsed step sets.
            try:
                await _manager_double_check_steps(protocol_name, [s.text for s in state.steps], refined_steps)
            except Exception:
                pass
        stable_step_texts = [s.text for s in state.steps] or list(fallback_steps or [])
        enrichments = await _enrich_steps_via_llm(protocol_name, stable_step_texts)

        if not state.is_active or state.protocol_name != protocol_name:
            return

        # Keep startup step text stable; only enrich metadata/context in background.
        for i, step in enumerate(state.steps):
            enrich = enrichments[i] if i < len(enrichments) else {}
            step.description = enrich.get("description", step.description)
            step.common_errors = enrich.get("common_errors", step.common_errors)

        if compacted.get("context_text"):
            old_preview = (state.extra_context or "")[:120]
            state.extra_context = compacted["context_text"]
            if state.experiment_data.get("sections"):
                state.extra_context += "\n\n" + state.experiment_data_xml()
            logger.info(
                "Protocol context updated in background refinement "
                f"(old_preview={old_preview!r}, new_preview={state.extra_context[:120]!r})"
            )

        if state.is_active:
            await viture_ui.render_step_panel(state)
    except Exception as exc:
        logger.warning(f"Background protocol refinement failed: {exc}")


async def _manager_double_check_steps(
    protocol_name: str,
    baseline_steps: list[str],
    candidate_steps: list[str],
) -> None:
    import asyncio
    from config import get_llm_client

    if not baseline_steps or not candidate_steps:
        return
    if len(candidate_steps) < max(1, len(baseline_steps) // 2):
        logger.warning(
            f"Manager check: candidate steps shrank sharply "
            f"({len(baseline_steps)} -> {len(candidate_steps)}) for '{protocol_name}'."
        )

    prompt = (
        "You are a protocol QA manager. Compare baseline steps and candidate parsed steps.\n"
        "Flag only severe problems: dropped major procedures, order corruption, or dangerous omissions.\n"
        "Return one line only: OK or ISSUE: <reason>.\n\n"
        f"Protocol: {protocol_name}\n"
        f"Baseline ({len(baseline_steps)}):\n" + "\n".join(f"- {s}" for s in baseline_steps[:20]) + "\n\n"
        f"Candidate ({len(candidate_steps)}):\n" + "\n".join(f"- {s}" for s in candidate_steps[:20])
    )

    def _call_sync():
        client, model = get_llm_client("router")
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=120,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, _call_sync)
    verdict = (response.choices[0].message.content or "").strip()
    if verdict:
        logger.info(f"Manager step-check ({protocol_name}): {verdict[:220]}")


async def format_experiment_data_rich_text(state: ProtocolState) -> str:
    """Format experiment_data for final panel with LLM, fallback deterministic."""
    import asyncio
    from config import get_llm_client

    xml = state.experiment_data_xml()
    prompt = FINAL_DATA_RICHTEXT_PROMPT.format(
        protocol_name=state.protocol_name or "Protocol",
        completed_at=datetime.utcnow().isoformat(timespec="seconds"),
        experiment_data_xml=xml,
    )

    def _call_sync():
        client, model = get_llm_client("router")
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=380,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call_sync)
        text = (response.choices[0].message.content or "").strip()
        if text:
            return text[:1600]
    except Exception as exc:
        logger.warning(f"Final experiment-data formatter failed: {exc}")

    # Deterministic fallback
    lines = [
        f"<size=22><b>{state.protocol_name} Data</b></size><br>",
        "<size=16><color=#AAAAAA>Captured experiment data</color></size><br>",
    ]
    sections = state.experiment_data.get("sections", {})
    if isinstance(sections, dict):
        for section, payload in sections.items():
            lines.append(f"<br><size=18><b>{section.replace('_', ' ').title()}</b></size><br>")
            headers = payload.get("headers", []) if isinstance(payload, dict) else []
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            if headers:
                lines.append(", ".join(headers) + "<br>")
            for row in rows[:8]:
                if isinstance(row, dict):
                    vals = [str(row.get(h, "")) for h in headers] if headers else [str(v) for v in row.values()]
                    lines.append(", ".join(vals) + "<br>")
                else:
                    lines.append(str(row) + "<br>")
    return "".join(lines)[:1600]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
@toggle_dashboard("list_protocols")
async def list_protocols() -> str:
    """List all available laboratory protocols. Shows them on the AR display
    and returns the list for voice output. Use when user says 'list protocols',
    'show protocols', 'what can I run', or 'what experiments are available'."""
    store = get_protocol_store()
    protocols = store.list_protocols()
    if not protocols:
        return "No protocols are currently available in the database."

    await viture_ui.render_protocol_list(store)

    state = get_protocol_state()
    state.mode = "listing"
    get_context_manager().set_context("protocol_listing")

    names = ", ".join(p["pretty_name"] for p in protocols)
    return (
        f"Here are the available protocols: {names}. "
        "Say the number or name of the protocol you'd like to run, "
        "describe your own, or say 'quit' to go back."
    )


@function_tool
@toggle_dashboard("start_protocol")
async def start_protocol(
    protocol_name: Annotated[str, Field(
        description="Name of the protocol to start, e.g. 'phone placement', "
        "'PCR amplification'. Can be a number from the listed protocols, "
        "a protocol name, or a custom experiment description."
    )]
) -> str:
    """Start running a laboratory protocol. Use when the user wants to run,
    start, execute, begin, or do a protocol or experiment."""
    from config import get_config

    provider = get_vsop_provider()
    if provider is None:
        provider = init_vsop_provider(get_config())
    store = get_protocol_store()
    state = get_protocol_state()

    if provider.is_active:
        await provider.stop()
        state.reset()

    proto = store.find_protocol(protocol_name)

    if not proto:
        try:
            idx = int(protocol_name)
            protocols = store.list_protocols()
            if 1 <= idx <= len(protocols):
                proto = protocols[idx - 1]
        except (ValueError, TypeError):
            pass

    if proto:
        fallback_steps = list(proto.get("steps", []))
        step_texts = _quick_compact_steps(fallback_steps)
        if not step_texts:
            step_texts = ["Prepare protocol according to source document."]

        # Fast baseline: STELLA starts with concise steps immediately.
        step_details = [StepDetail(text=t, description=t) for t in step_texts]

        state.is_active = True
        state.mode = "running"
        state.protocol_name = proto["pretty_name"]
        state.steps = step_details
        state.current_step = 1
        state.completed_steps = []
        state.error_history = []
        state.start_time = time.time()
        state.stella_vision_text = ""
        state.extra_context = ""
        _init_experiment_data(state)
        state.error_display_until = 0.0
        state.error_cooldown_until = 0.0

        get_context_manager().set_context("protocol_running")

        resp = await provider.start(
            protocol_name=proto["pretty_name"],
            protocol_steps=step_texts,
            protocol_context=state.extra_context,
        )

        await viture_ui.render_step_panel(state)

        # Refine details/context in background without delaying startup.
        try:
            import asyncio
            asyncio.create_task(
                _refine_steps_background(
                    protocol_name=proto["pretty_name"],
                    raw_protocol=proto.get("raw", ""),
                    fallback_steps=fallback_steps,
                    state=state,
                    provider=provider,
                )
            )
        except Exception as exc:
            logger.warning(f"Failed to start background refinement task: {exc}")

        return resp

    # Generate mode -- STELLA extracts protocol from camera
    state.is_active = True
    state.mode = "running"
    state.protocol_name = protocol_name
    state.start_time = time.time()
    _init_experiment_data(state)
    get_context_manager().set_context("protocol_running")

    resp = await provider.start(protocol_name=protocol_name)
    status = await provider.get_status()
    raw_steps = list(getattr(provider, "_steps", []))

    step_details = []
    for t in raw_steps:
        step_details.append(StepDetail(text=t))
    state.steps = step_details
    state.current_step = status.get("current_step", 1)

    if state.steps:
        await viture_ui.render_step_panel(state)
    return resp


@function_tool
@toggle_dashboard("stop_protocol")
async def stop_protocol() -> str:
    """Stop the currently running protocol and return to the main menu.
    Also exits the protocol selection screen. Use when user says stop,
    cancel, end, quit, back, never mind, or exit."""
    provider = get_vsop_provider()
    state = get_protocol_state()

    if state.mode == "listing":
        state.reset()
        get_context_manager().set_context("main_menu")
        await viture_ui.render_greeting()
        return "No problem. Say 'list protocols' whenever you're ready."

    if (provider is None or not provider.is_active) and not state.is_active:
        return "No protocol is currently being monitored."

    resp = await provider.stop()
    state.reset()
    get_context_manager().set_context("main_menu")

    store = get_protocol_store()
    await viture_ui.render_protocol_list(store)
    return resp + " Say 'list protocols' to start another."


@function_tool
@toggle_dashboard("next_step")
async def next_step() -> str:
    """Advance to the next step in the current protocol. Use when the user
    says next, skip, move on, advance, or continue to next step."""
    provider = get_vsop_provider()
    if provider is None or not provider.is_active:
        return "No protocol is currently running."
    result = await provider.manual_advance()

    state = get_protocol_state()
    state.current_step = provider._current_step
    state.completed_steps = list(provider._completed_steps)
    if state.is_active:
        await viture_ui.render_step_panel(state)
    return result


@function_tool
@toggle_dashboard("previous_step")
async def previous_step() -> str:
    """Go back to the previous step in the current protocol. Use when user
    says previous, go back, back, or undo."""
    provider = get_vsop_provider()
    if provider is None or not provider.is_active:
        return "No protocol is currently running."
    result = await provider.manual_retreat()

    state = get_protocol_state()
    state.current_step = provider._current_step
    state.completed_steps = list(provider._completed_steps)
    await viture_ui.render_step_panel(state)
    return result


@function_tool
@toggle_dashboard("go_to_step")
async def go_to_step(
    step_number: Annotated[int, Field(description="Step number to jump to (1-based)")]
) -> str:
    """Jump to a specific step number in the current protocol."""
    provider = get_vsop_provider()
    if provider is None or not provider.is_active:
        return "No protocol is currently running."
    result = await provider.manual_goto(step_number)

    state = get_protocol_state()
    state.current_step = provider._current_step
    state.completed_steps = list(provider._completed_steps)
    await viture_ui.render_step_panel(state)
    return result


@function_tool
@toggle_dashboard("restart_protocol")
async def restart_protocol() -> str:
    """Restart the current protocol from step 1."""
    provider = get_vsop_provider()
    if provider is None or not provider.is_active:
        return "No protocol is currently running."
    result = await provider.manual_restart()

    state = get_protocol_state()
    state.current_step = 1
    state.completed_steps = []
    state.error_history = []
    await viture_ui.render_step_panel(state)
    return result


@function_tool
@toggle_dashboard("clear_error")
async def clear_error() -> str:
    """Dismiss the current error and continue with the protocol. Use when
    user says clear, continue, move on, or dismiss after an error."""
    provider = get_vsop_provider()
    state = get_protocol_state()

    if provider is None or not provider.is_active:
        return "No protocol is currently running."

    if hasattr(provider, "_in_error_state"):
        provider._in_error_state = False

    state.error_display_until = 0.0
    state.error_cooldown_until = time.time() + ERROR_COOLDOWN_SECONDS

    await viture_ui.render_step_panel(state)

    step_text = ""
    if 1 <= state.current_step <= len(state.steps):
        step_text = state.steps[state.current_step - 1].text
    return f"Error cleared. Continuing with step {state.current_step}: {step_text}"


@function_tool
@toggle_dashboard("get_protocol_status")
async def get_protocol_status() -> str:
    """Get the current protocol status: which step, progress, time elapsed.
    Use when user asks 'what step am I on', 'progress', or 'status'."""
    provider = get_vsop_provider()
    state = get_protocol_state()

    if provider is None or not provider.is_active:
        return "No protocol is currently being monitored."

    status = await provider.get_status()
    current = status.get("current_step", 0)
    total = status.get("total_steps", 0)
    completed = len(status.get("completed_steps", []))
    remaining = total - completed
    elapsed = state.elapsed_str()

    return (
        f"Protocol: {status.get('protocol_name', 'Unknown')}. "
        f"On step {current} of {total}. "
        f"{completed} completed, {remaining} remaining. "
        f"Time elapsed: {elapsed}."
    )


@function_tool
@toggle_dashboard("query_completed_protocol_data")
async def query_completed_protocol_data(
    protocol_name: Annotated[str, Field(description="Optional protocol name filter, or leave blank for latest")] = "",
) -> str:
    """Query experiment data captured from completed protocols in this session."""
    state = get_protocol_state()
    runs = list(state.completed_runs)
    if protocol_name:
        q = protocol_name.lower().strip()
        runs = [r for r in runs if q in str(r.get("protocol_name", "")).lower()]

    if not runs:
        return "No completed protocol data found in this session."

    latest = runs[-1]
    pname = latest.get("protocol_name", "Unknown")
    completed_at = latest.get("completed_at", "unknown time")
    sections = latest.get("experiment_data", {}).get("sections", {})
    section_names = ", ".join(sections.keys()) if isinstance(sections, dict) and sections else "none"
    return (
        f"Latest completed run: {pname} at {completed_at}. "
        f"Captured sections: {section_names}. "
        "Say show experiment data with a section name for details."
    )


@function_tool
@toggle_dashboard("show_experiment_data")
async def show_experiment_data(
    section: Annotated[str, Field(description="Section name like tube_weights, timings, temperatures, volumes, notes")] = "",
) -> str:
    """Show captured experiment data details for the latest completed run in this session."""
    state = get_protocol_state()
    if not state.completed_runs:
        return "No completed protocol data found in this session."

    latest = state.completed_runs[-1]
    sections = latest.get("experiment_data", {}).get("sections", {})
    if not isinstance(sections, dict) or not sections:
        return "No experiment data was captured in the latest completed run."

    if section:
        key = section.strip().lower().replace(" ", "_")
        payload = sections.get(key)
        if not payload:
            return f"Section '{section}' was not found in the latest completed run."
        headers = payload.get("headers", [])
        rows = payload.get("rows", [])
        lines = [f"{key}:"]
        if headers:
            lines.append(", ".join(headers))
        for row in rows[:12]:
            if isinstance(row, dict):
                vals = [str(row.get(h, "")) for h in headers] if headers else [str(v) for v in row.values()]
                lines.append(", ".join(vals))
            else:
                lines.append(str(row))
        return "\n".join(lines)

    names = ", ".join(sections.keys())
    return f"Available experiment data sections: {names}."


@function_tool
@toggle_dashboard("get_errors")
async def get_errors() -> str:
    """Report errors from the current protocol run or the most recently
    completed run. Returns each error with its step number and detail."""
    state = get_protocol_state()

    errors = []
    source = "current run"
    if state.mode == "running" and state.error_history:
        errors = state.error_history
    elif state.completed_runs:
        latest = state.completed_runs[-1]
        errors = latest.get("error_history", [])
        source = f"completed run ({latest.get('protocol_name', 'unknown')})"
    elif state.error_history:
        errors = state.error_history

    if not errors:
        return "No errors recorded."

    lines = [f"Errors from {source}:"]
    for e in errors:
        lines.append(f"  Step {e.get('step', '?')}: {e.get('detail', 'unknown error')}")
    return "\n".join(lines)


@function_tool
@toggle_dashboard("detailed_step")
async def detailed_step(
    step_number: Annotated[Optional[int], Field(
        description="Step number to show details for. Defaults to current step."
    )] = None,
) -> str:
    """Show an expanded detailed view of a protocol step on the AR display.

    Includes a longer description, common errors, and optionally searches
    for a relevant image. Use when the user says 'more details' or
    'explain this step'."""
    state = get_protocol_state()
    if state.mode != "running" or not state.steps:
        return "No active protocol. Start a protocol first."

    num = step_number or state.current_step
    if num < 1 or num > len(state.steps):
        return f"Invalid step number. Protocol has steps 1-{len(state.steps)}."

    step = state.steps[num - 1]
    total = len(state.steps)

    from tools.common.rich_panel import RichPanelBuilder
    builder = RichPanelBuilder()
    builder.title(f"Step {num}/{total}: {step.text}")

    desc = step.description or step.text
    builder.body(desc, size=17)

    if step.common_errors:
        errs = "; ".join(step.common_errors[:3])
        builder.caption(f"Watch out for: {errs}")

    if step.status == "error" and step.error_detail:
        builder.raw(
            f'<br><size=16><color=#FF4444>Error: {step.error_detail}</color></size>'
        )

    builder.divider()

    image_b64 = None
    try:
        import asyncio as _aio
        from tools.common.web import _image_search, fetch_image_as_base64
        loop = _aio.get_running_loop()
        query = f"{state.protocol_name} {step.text} lab"
        results = await loop.run_in_executor(None, _image_search, query, 5)
        for r in results:
            for url_key in ("image", "thumbnail", "url"):
                url = r.get(url_key, "")
                if url:
                    image_b64 = await fetch_image_as_base64(url)
                    if image_b64:
                        break
            if image_b64:
                break
    except Exception:
        pass

    blocks = builder.build()
    if image_b64:
        blocks.insert(0, {"type": "base64-image", "content": image_b64})

    viture_ui.set_display_mode("overlay")
    await viture_ui._push_panel(blocks)
    return f"Detailed view for step {num} displayed. Say 'show steps' to return."
