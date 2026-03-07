"""Protocol management tools.

Owns the ProtocolState lifecycle, STELLA monitor loop start/stop, and
exposes @function_tool functions for listing, starting, stopping,
navigating, and clearing errors.
"""

import hashlib
import re
import time
from datetime import datetime
from typing import Annotated, Optional

from agents import function_tool
from loguru import logger
from pydantic import Field

from tools.protocols.state import ProtocolState, StepDetail, get_protocol_state
from tools.protocols.store import (
    find_available_protocol,
    get_protocol_store,
    list_available_protocols,
)
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
- Do NOT invent or fabricate information not present in the step text. Only rephrase what is given.
- Do NOT add safety steps, cleanup steps, or extra actions not in the original step.
- Prefer concrete specifics (what to pick up, what to record, where to place).
- Return strict JSON only (ASCII), no markdown, no code fences.
- Escape any double quotes inside values.
- Keep each object shape EXACTLY: {{"description": "...", "common_errors": ["..."]}}.
- Do not include trailing commas, comments, or extra keys.
- Do not include URLs in descriptions.

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
- Each step has "text" (short action label, 10 words MAX) and "detail" (full specifics, 1-3 sentences).
- "text" is spoken aloud by TTS -- keep it brief, natural, and TTS-friendly.
- In "text", expand units/acronyms for speech: "5g" -> "5 grams", "10ml" -> "10 milliliters", "uL" -> "microliters". Spell out single letters: "F1" -> "F-1".
- "detail" MUST preserve ALL specific identifiers: tube labels, reagent names, volumes, temperatures, counts, times, concentrations.
- "detail" includes practical guidance, warnings, and the identifiers that were omitted from "text".
- Preserve order and critical constraints.
- Exclude decorative or repeated prose.
- Keep 3-20 steps total.
- Do NOT invent an "Introduction" or "Welcome" first step that does not exist in the source. Start with the first real action.
- The source may use XML/HTML tags -- extract only the actionable steps and context, never include markup tags in the output.
- CRITICAL: Do NOT invent, add, or fabricate steps that are not in the source protocol. Only rephrase what exists. If the source has 12 steps, output exactly 12 steps. Never add safety steps, cleanup steps, or any content not present in the original.
- If a step contains a URL in brackets like [https://...], strip it from both "text" and "detail". Do not include URLs in the output.

Example step:
{{
  "text": "Label five P-C-R tubes and mix tube",
  "detail": "Label tubes as F1/R1, F2/R2, F3/R3, F4/R4, F5/R5, and label the 0.5 mL tube for the mix. Do this before adding any liquid so you do not mix up reactions later."
}}

Another example:
{{
  "text": "Add reagents to the mix tube",
  "detail": "In the 0.5 mL tube, add 5 uL 5X QS buffer, 5 uL 5X G/C buffer, 0.5 uL each primer, 0.5 uL dNTPs, 0.25 uL Q5Pol*HS, and 13.25 uL pDNA. Multiply by number of reactions plus 10 percent if making a master mix."
}}

Return ONLY valid JSON, no markdown:
{{
  "steps": [
    {{"text": "short action label (10 words max)", "detail": "full specifics with all identifiers and guidance"}}
  ],
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

_ROBOT_TAG_RE = re.compile(r"\s*\[robot:([^\]]+)\]\s*", re.IGNORECASE)
_ROBOT_NL_RE = re.compile(
    r"\s*\(run\s+robot\s+protocol\s+[\"']([^\"']+)[\"']\)\s*",
    re.IGNORECASE,
)

_BRACKET_URL_RE = re.compile(r"\s*\[(https?://[^\]\s]+)\]\s*")
_PAREN_SOURCE_RE = re.compile(r"\s*\((?:Source:\s*)?(https?://\S+?)\)\s*")
_IMAGE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|bmp)", re.IGNORECASE)


def _extract_image_url(text: str) -> tuple[str, str]:
    """Extract first image URL from text, return (cleaned_text, image_url).

    Handles:
      - [https://example.com/image.jpg]  (bracketed URLs)
      - [https://th.bing.com/th/id/R.abc?rik=...&r=0]  (bracketed URLs without extension)
      - (Source: https://example.com/img.png)  (LLM-rewritten source links)
    """
    m = _BRACKET_URL_RE.search(text)
    if m:
        url = m.group(1)
        cleaned = text[:m.start()] + text[m.end():]
        return cleaned.strip(), url
    m = _PAREN_SOURCE_RE.search(text)
    if m:
        url = m.group(1)
        cleaned = text[:m.start()] + text[m.end():]
        return cleaned.strip(), url
    return text, ""


def _build_step_payload(raw) -> dict:
    """Normalize a step (str or dict) into {text, detail, image_url, image_query}."""
    if isinstance(raw, dict):
        text = str(raw.get("text", "")).strip()
        detail = str(raw.get("detail", "")).strip()
        image_url = str(raw.get("image_url", "")).strip()
        image_query = str(raw.get("image_query", "")).strip()
    else:
        text = str(raw).strip()
        detail = text
        image_url = ""
        image_query = ""

    if not image_url:
        text, image_url = _extract_image_url(text)
    if not image_url:
        detail, image_url = _extract_image_url(detail)

    words = text.split()
    if len(words) > 12:
        text = " ".join(words[:10]) + "..."
        if not detail or detail == text:
            detail = " ".join(words)

    return {"text": text, "detail": detail, "image_url": image_url, "image_query": image_query}


def _looks_like_intro_title(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith(("introduction", "welcome", "overview")) and len(t) < 80


def _strip_intro_prefix(text: str) -> str:
    for prefix in ("Introduction:", "Welcome:", "Overview:"):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _remove_synthetic_intro_step(steps: list) -> list:
    """If the first step looks like a synthetic intro, rewrite or remove it."""
    if not steps:
        return steps
    first = steps[0]
    if isinstance(first, dict):
        t = first.get("text", "")
    else:
        t = str(first)
    if _looks_like_intro_title(t):
        if isinstance(first, dict):
            first["text"] = _strip_intro_prefix(first["text"])
            if first.get("detail"):
                first["detail"] = _strip_intro_prefix(first["detail"])
        else:
            steps[0] = _strip_intro_prefix(t)
    return steps


def _extract_robot_annotation(step_text: str) -> tuple:
    """Extract ``[robot:name]`` or ``(run robot protocol "name")`` from step text.

    Returns ``(clean_text, robot_protocol_name | None)``.
    """
    for pattern in (_ROBOT_TAG_RE, _ROBOT_NL_RE):
        m = pattern.search(step_text)
        if m:
            clean = step_text[:m.start()] + step_text[m.end():]
            return clean.strip(), m.group(1).strip()
    return step_text, None


def _sync_step_statuses(state) -> None:
    """Synchronize each StepDetail.status with state.current_step and completed_steps."""
    for i, step in enumerate(state.steps):
        num = i + 1
        if num in state.completed_steps:
            step.status = "completed"
            step.error_detail = None
        elif num == state.current_step:
            step.status = "in_progress"
        else:
            step.status = "pending"
            step.error_detail = None


async def ensure_current_step_image_loaded(state) -> None:
    """Fetch and cache base64 for the current step's image_url if needed."""
    detail = state.current_step_detail()
    if detail is None:
        return
    if detail.image_base64:
        return
    url = (detail.image_url or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return
    try:
        import base64
        import httpx
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.content) > 100:
                b64 = base64.b64encode(resp.content).decode("ascii")
                if len(b64) > 200:
                    detail.image_base64 = b64
                    logger.debug(f"[Image] Loaded {len(b64)} chars for step {state.current_step}")
                else:
                    logger.debug(f"[Image] Fetched image too small for step {state.current_step}")
            else:
                logger.debug(f"[Image] Fetch failed ({resp.status_code}) for: {url[:80]}")
    except Exception as exc:
        logger.debug(f"[Image] Failed to load image for step {state.current_step}: {exc}")
        detail.image_base64 = ""


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
- Prefer section names: tube_weights, timings, temperatures, volumes, notes, observations.
- For free-form observations (e.g. "petri dish looks dead", "colonies look bad",
  "nothing grew in dish 3", "the phone is kind of heavy", "log, but X"),
  use section "observations" with row {{"note": "<observation>"}}.
- Text often comes from noisy STT. "log, but the phone is heavy" means the user
  said "log that the phone is heavy". Always extract the observation even if garbled.
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

PROTOCOL_SUMMARY_PROMPT = """\
Summarize this completed protocol run in 4-5 sentences for display on AR glasses.

Protocol: {protocol_name}
Duration: {duration}
Steps completed: {completed_count}/{total_steps}
Errors: {error_count}

Observation history:
{observation_history}

Experiment data:
{experiment_data}

Error log:
{error_log}

Rules:
- 4-5 sentences, no filler or flowery language.
- Mention the protocol name, how long it took, and whether all steps were completed.
- Highlight any errors or notable observations.
- If experiment data was logged, mention key findings.
- Be factual and concise.
"""


async def generate_protocol_summary(state) -> tuple[str, str]:
    """Generate a protocol summary using the reasoning LLM.

    Returns (plain_text, rich_text) where rich_text uses TMP tags for the AR panel.
    """
    import asyncio
    from config import get_reason_llm_client

    duration_s = int(time.time() - state.start_time) if state.start_time else 0
    minutes, seconds = divmod(duration_s, 60)
    duration = f"{minutes}m {seconds}s"

    obs_lines = []
    for h in (state.monitoring_high or []):
        obs_lines.append(f"[30min summary] {h}")
    for m in (state.monitoring_medium or []):
        obs_lines.append(f"[2min summary] {m}")
    recent = (state.monitoring_granular or [])[-6:]
    for g in recent:
        obs_lines.append(f"[recent] {g}")
    observation_history = "\n".join(obs_lines) if obs_lines else "(none)"

    experiment_data = state.experiment_data_xml() if hasattr(state, "experiment_data_xml") else "(none)"

    error_lines = []
    for err in (state.error_history or []):
        error_lines.append(f"Step {err.get('step', '?')}: {err.get('detail', 'unknown')}")
    error_log = "\n".join(error_lines) if error_lines else "(none)"

    prompt = PROTOCOL_SUMMARY_PROMPT.format(
        protocol_name=state.protocol_name or "Unknown",
        duration=duration,
        completed_count=len(state.completed_steps),
        total_steps=len(state.steps),
        error_count=len(state.error_history),
        observation_history=observation_history,
        experiment_data=experiment_data,
        error_log=error_log,
    )

    def _call_sync():
        client, model = get_reason_llm_client()
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=400,
        )

    plain_text = ""
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call_sync)
        plain_text = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning(f"Protocol summary LLM call failed: {exc}")

    if not plain_text:
        plain_text = (
            f"Completed {state.protocol_name} in {duration}. "
            f"{len(state.completed_steps)}/{len(state.steps)} steps done. "
            f"{len(state.error_history)} errors logged. "
            f"{len((state.experiment_data or {}).get('sections', {}))} data sections captured."
        )

    rich_text = (
        f'<size=22><color=#59D2FF><b>Protocol Summary</b></color></size><br>'
        f'<size=18><b>{state.protocol_name}</b></size><br>'
        f'<size=15><color=#AAAAAA>Duration: {duration} | '
        f'Steps: {len(state.completed_steps)}/{len(state.steps)} | '
        f'Errors: {len(state.error_history)}</color></size><br><br>'
        f'<size=16><color=#DDDDDD>{plain_text}</color></size><br><br>'
        f'<size=14><color=#999999>You can ask about observations, errors, or data. '
        f'Returning to main menu in 1 minute.</color></size>'
    )

    return plain_text, rich_text


async def _enrich_steps_via_llm(protocol_name: str, step_texts: list) -> list:
    """Call reason_llm (Gemini) to generate descriptions and common errors."""
    import asyncio, json, re
    from config import get_reason_llm_client

    steps_block = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(step_texts))
    prompt = STEP_ENRICHMENT_PROMPT.format(
        protocol_name=protocol_name,
        steps_block=steps_block,
    )

    def _defaults() -> list:
        return [{"description": "", "common_errors": []} for _ in step_texts]

    def _normalize_entries(data: list) -> list:
        normalized = []
        for item in data:
            if not isinstance(item, dict):
                normalized.append({"description": "", "common_errors": []})
                continue
            desc = str(item.get("description", "") or "").replace("\n", " ").strip()
            if len(desc) > 220:
                desc = desc[:220].rstrip() + "..."
            raw_errors = item.get("common_errors", [])
            if isinstance(raw_errors, str):
                raw_errors = [raw_errors]
            errors = []
            if isinstance(raw_errors, list):
                for e in raw_errors[:2]:
                    txt = str(e or "").replace("\n", " ").strip()
                    if txt:
                        errors.append(txt[:120])
            normalized.append({"description": desc, "common_errors": errors})

        if len(normalized) < len(step_texts):
            normalized.extend([{"description": "", "common_errors": []}] * (len(step_texts) - len(normalized)))
        return normalized[:len(step_texts)]

    def _extract_balanced_array(text: str) -> str:
        start = text.find("[")
        if start < 0:
            return ""
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return ""

    def _parse_from_candidates(raw: str) -> list | None:
        candidates = []
        direct = raw.strip()
        if direct:
            candidates.append(direct)

        for m in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE):
            block = (m.group(1) or "").strip()
            if block:
                candidates.append(block)

        bracket = _extract_balanced_array(raw)
        if bracket:
            candidates.append(bracket)

        for cand in candidates:
            try:
                parsed = json.loads(cand)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                continue
        return None

    def _repair_json_sync(bad_raw: str):
        client, model = get_reason_llm_client()
        repair_prompt = (
            "Fix this malformed JSON into a valid JSON array.\n"
            "Return ONLY a JSON array where each element is an object with EXACTLY:\n"
            "{\"description\": string, \"common_errors\": [string, ...]}\n"
            "No markdown, no extra keys, no commentary.\n\n"
            f"Malformed input:\n{bad_raw}"
        )
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": repair_prompt}],
            temperature=0.0,
            max_tokens=1200,
        )

    def _call_sync():
        client, model = get_reason_llm_client()
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2048,
        )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call_sync)
        raw = response.choices[0].message.content.strip()
        parsed = _parse_from_candidates(raw)
        if parsed is not None:
            return _normalize_entries(parsed)

        logger.debug("Step enrichment parse failed; attempting JSON repair pass.")
        repaired_resp = await loop.run_in_executor(None, lambda: _repair_json_sync(raw[:6000]))
        repaired_raw = (repaired_resp.choices[0].message.content or "").strip()
        repaired = _parse_from_candidates(repaired_raw)
        if repaired is not None:
            return _normalize_entries(repaired)
    except Exception as exc:
        logger.warning(f"Step enrichment LLM call failed: {exc}")

    logger.warning("Step enrichment failed after parse and repair; using empty descriptions.")
    return _defaults()


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


def _extract_balanced_object(text: str) -> str | None:
    """Extract the first balanced {...} from text using brace counting."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


async def _compact_protocol_via_llm(protocol_name: str, raw_protocol: str, fallback_steps: list) -> dict:
    """Compile source protocol text into compact steps + protocol-aware context."""
    import asyncio
    import json
    import re
    from config import get_reason_llm_client

    raw = (raw_protocol or "").strip()
    if not raw:
        return {"steps": fallback_steps, "context_text": ""}

    clipped_raw = raw[:14000]
    prompt = PROTOCOL_COMPACTION_PROMPT.format(
        protocol_name=protocol_name,
        raw_protocol=clipped_raw,
    )

    def _call_sync():
        client, model = get_reason_llm_client()
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2400,
        )

    compact_steps: list[dict | str] = list(fallback_steps or [])
    context_text = ""
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call_sync)
        raw_reply = (response.choices[0].message.content or "").strip()
        json_str = _extract_balanced_object(raw_reply)
        if json_str:
            data = json.loads(json_str)
            if isinstance(data, dict):
                steps = data.get("steps")
                if isinstance(steps, list):
                    cleaned = []
                    for s in steps:
                        if isinstance(s, dict) and s.get("text"):
                            cleaned.append({
                                "text": str(s["text"]).strip(),
                                "detail": str(s.get("detail", "")).strip(),
                            })
                        elif isinstance(s, str) and s.strip():
                            cleaned.append(s.strip())
                    if cleaned:
                        compact_steps = cleaned[:20]
                    compact_steps = _remove_synthetic_intro_step(compact_steps)
                context_text = _build_protocol_context_text(data.get("context") or {})
    except Exception as exc:
        logger.warning(f"Protocol compaction LLM call failed: {exc}")

    if not compact_steps:
        compact_steps = ["Prepare protocol according to source document."]

    return {"steps": compact_steps, "context_text": context_text}


def _quick_compact_steps(step_texts: list[str]) -> list[dict]:
    """Return list of step payload dicts with text, detail, image_url, image_query."""
    compact = []
    for step in step_texts:
        full = " ".join(str(step).split()).strip()
        if not full:
            continue
        payload = _build_step_payload(full)
        compact.append(payload)
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

    if isinstance(row, dict):
        row.setdefault("_step", str(state.current_step))
        row.setdefault("_timestamp", datetime.utcnow().strftime("%H:%M:%S"))

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

    try:
        import asyncio
        asyncio.ensure_future(_emit_labos_protocol_data(
            state.protocol_name, {section: row if isinstance(row, dict) else str(row)}
        ))
    except Exception:
        pass
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
    from config import get_reason_llm_client

    current_text = ""
    if 1 <= state.current_step <= len(state.steps):
        current_text = state.steps[state.current_step - 1].text
    def _call_sync():
        client, model = get_reason_llm_client()
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=240,
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
        raw_steps = compacted["steps"] or fallback_steps

        refined_texts: list[str] = []
        compacted_details: list[str] = []
        for s in raw_steps:
            if isinstance(s, dict):
                refined_texts.append(str(s.get("text", "")).strip())
                compacted_details.append(str(s.get("detail", "")).strip())
            else:
                refined_texts.append(str(s).strip())
                compacted_details.append("")

        if refined_texts:
            try:
                await _manager_double_check_steps(protocol_name, [s.text for s in state.steps], refined_texts)
            except Exception:
                pass

        if not state.is_active or state.protocol_name != protocol_name:
            return

        # Update step texts and pre-populate descriptions from compaction detail.
        # Re-extract image URLs from LLM output and preserve existing ones.
        cleaned_provider_texts = []
        for i, step in enumerate(state.steps):
            new_text = refined_texts[i] if i < len(refined_texts) else ""
            new_detail = compacted_details[i] if i < len(compacted_details) else ""

            if new_text:
                new_text, found_url = _extract_image_url(new_text)
                if found_url and not step.image_url:
                    step.image_url = found_url
                step.text = new_text
            if new_detail:
                new_detail, found_url = _extract_image_url(new_detail)
                if found_url and not step.image_url:
                    step.image_url = found_url
                step.description = new_detail

            cleaned_provider_texts.append(step.text)

        # Also update the provider's internal step list so TTS uses cleaned text
        if hasattr(provider, "_steps") and cleaned_provider_texts:
            provider._steps = cleaned_provider_texts[:len(provider._steps)]

        stable_step_texts = [s.text for s in state.steps] or list(fallback_steps or [])
        enrichments = await _enrich_steps_via_llm(protocol_name, stable_step_texts)

        if not state.is_active or state.protocol_name != protocol_name:
            return

        for i, step in enumerate(state.steps):
            enrich = enrichments[i] if i < len(enrichments) else {}
            if not step.description and enrich.get("description"):
                step.description = enrich["description"]
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
            await ensure_current_step_image_loaded(state)
            await viture_ui.render_step_panel(state)
    except Exception as exc:
        logger.warning(f"Background protocol refinement failed: {exc}")


async def _manager_double_check_steps(
    protocol_name: str,
    baseline_steps: list[str],
    candidate_steps: list[str],
) -> None:
    import asyncio
    from config import get_reason_llm_client

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
        client, model = get_reason_llm_client()
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=120,
        )

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, _call_sync)
    verdict = (response.choices[0].message.content or "").strip()
    if verdict:
        logger.info(f"Manager step-check ({protocol_name}): {verdict[:220]}")


async def format_experiment_data_rich_text(state: ProtocolState) -> str:
    """Format experiment_data for final panel with LLM, fallback deterministic."""
    import asyncio
    from config import get_reason_llm_client

    xml = state.experiment_data_xml()
    prompt = FINAL_DATA_RICHTEXT_PROMPT.format(
        protocol_name=state.protocol_name or "Protocol",
        completed_at=datetime.utcnow().isoformat(timespec="seconds"),
        experiment_data_xml=xml,
    )

    def _call_sync():
        client, model = get_reason_llm_client()
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=380,
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
    state = get_protocol_state()
    protocols = list_available_protocols(store, state)
    if not protocols:
        return "No protocols are currently available in the database."

    await viture_ui.render_protocol_list(store, state=state)

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

    proto = find_available_protocol(protocol_name, store, state)

    if not proto:
        try:
            idx = int(protocol_name)
            protocols = list_available_protocols(store, state)
            if 1 <= idx <= len(protocols):
                proto = protocols[idx - 1]
        except (ValueError, TypeError):
            pass

    if proto:
        fallback_steps = list(proto.get("steps", []))
        compacted_pairs = _quick_compact_steps(fallback_steps)
        if not compacted_pairs:
            compacted_pairs = [{"text": "Prepare protocol according to source document.", "detail": "Prepare protocol according to source document.", "image_url": "", "image_query": ""}]

        step_texts = [p["text"] for p in compacted_pairs]

        step_details = []
        for p in compacted_pairs:
            clean, robot_proto = _extract_robot_annotation(p["text"])
            step_details.append(StepDetail(
                text=clean,
                description=p["detail"],
                robot_protocol=robot_proto,
                image_url=p.get("image_url", ""),
                image_query=p.get("image_query", ""),
            ))

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

        await ensure_current_step_image_loaded(state)
        await viture_ui.render_step_panel(state)

        await _emit_labos_protocol_start(state)

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
        clean, robot_proto = _extract_robot_annotation(t)
        step_details.append(StepDetail(text=clean, robot_protocol=robot_proto))
    state.steps = step_details
    state.current_step = status.get("current_step", 1)

    if state.steps:
        await viture_ui.render_step_panel(state)
        await _emit_labos_protocol_start(state)
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
    state.is_active = False
    state.mode = "completed"
    from tools.protocols.events import complete_protocol_run
    await complete_protocol_run(state, completion_tts_prefix="Protocol finished.")
    return resp + " Showing protocol summary."


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
    _sync_step_statuses(state)
    if state.is_active:
        await ensure_current_step_image_loaded(state)
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
    _sync_step_statuses(state)
    await ensure_current_step_image_loaded(state)
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
    _sync_step_statuses(state)
    await ensure_current_step_image_loaded(state)
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
    _sync_step_statuses(state)
    await ensure_current_step_image_loaded(state)
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

    await ensure_current_step_image_loaded(state)
    await viture_ui.render_step_panel(state)

    step_text = ""
    if 1 <= state.current_step <= len(state.steps):
        step_text = state.steps[state.current_step - 1].text
    return f"Error cleared. Continuing with step {state.current_step}: {step_text}"


@function_tool
@toggle_dashboard("reset_session")
async def reset_session() -> str:
    """Reset the session to the main menu. Clears protocol state, session
    protocols, and context. Use when user says 'reset', 'go home',
    'main menu', or 'start over'."""
    provider = get_vsop_provider()
    state = get_protocol_state()

    if provider is not None and provider.is_active:
        try:
            await provider.stop()
        except Exception:
            pass

    state.reset(clear_session_protocols=True)
    get_context_manager().set_context("main_menu")

    try:
        await viture_ui.render_greeting()
    except Exception:
        pass

    return "Session reset. Back at main menu."


@function_tool
@toggle_dashboard("available_commands")
async def available_commands() -> str:
    """Show available commands on the AR display. Use when user asks
    'what can I do?', 'what can you do?', 'help', or 'commands'."""
    try:
        from tools.display.ui import render_available_commands
        await render_available_commands()
    except Exception:
        pass
    return "I've displayed the available commands."


@function_tool
@toggle_dashboard("practice_guidance")
async def practice_guidance(
    query: Annotated[str, Field(description="Lab equipment or technique name, e.g. 'pipette', 'centrifuge', 'vortexer'")]
) -> str:
    """Look up guidance for a lab tool or technique and display on AR.
    Adapts parameters to the current protocol context (e.g. RPM, volumes).
    Use when user asks 'how do I use a pipette?' or similar."""
    from tools.protocols.practices_store import get_practice_steps, list_practices

    result = get_practice_steps(query)
    if not result.get("found"):
        available = list_practices()
        if available:
            return f"No guidance found for '{query}'. Available: {', '.join(available[:8])}."
        return f"No guidance data available for '{query}'. Try asking me directly."

    state = get_protocol_state()
    context_hint = ""
    if state.is_active and state.current_step_detail():
        step_text = state.current_step_detail().description or state.current_step_detail().text
        context_hint = f"\nCurrent protocol step context: {step_text[:200]}"

    steps_text = ""
    for i, step in enumerate(result.get("steps", []), 1):
        if isinstance(step, dict):
            steps_text += f"\n{i}. {step.get('instruction', step.get('text', str(step)))}"
        else:
            steps_text += f"\n{i}. {step}"

    safety = result.get("safety_notes", "")
    ppe = ", ".join(result.get("ppe", []))

    lines = [
        f'<size=22><b>{result["name"]}</b></size><br>',
        f'<size=16><color=#D9D8FF>{result.get("goal", "")}</color></size><br><br>',
    ]
    if steps_text:
        lines.append(f'<size=16><color=#DDDDDD>{steps_text.strip()}</color></size><br><br>')
    if safety:
        lines.append(f'<size=14><color=#FFB347>Safety: {safety}</color></size><br>')
    if ppe:
        lines.append(f'<size=14><color=#FFB347>PPE: {ppe}</color></size><br>')
    if context_hint:
        lines.append(f'<br><size=14><color=#59D2FF>Adapted to current step.{context_hint[:100]}</color></size>')

    try:
        await viture_ui.render_rich_panel([{"type": "rich-text", "content": "".join(lines)}])
    except Exception:
        pass

    spoken = f"Here's guidance on {result['name']}."
    if result.get("goal"):
        spoken += f" {result['goal'][:80]}"
    return spoken


@function_tool
@toggle_dashboard("start_protocol_discussion")
async def start_protocol_discussion() -> str:
    """Begin a protocol discussion session where the user can describe and
    refine a temporary protocol before running it. Use when user says
    'let's create a protocol' or 'discuss a protocol'."""
    state = get_protocol_state()
    if state.is_active:
        return "A protocol is already running. Stop it first."

    state.mode = "discussion"
    get_context_manager().set_context("protocol_discussion")
    return "Protocol discussion started. Describe the steps you'd like to include."


@function_tool
@toggle_dashboard("update_protocol_discussion")
async def update_protocol_discussion(
    text: Annotated[str, Field(description="Updated protocol text or step list")]
) -> str:
    """Update the draft protocol being discussed. Call when user provides
    or modifies protocol steps during discussion mode."""
    state = get_protocol_state()
    state.extra_context = text
    return f"Draft updated with {len(text)} characters. Say 'run this' when ready."


@function_tool
@toggle_dashboard("run_discussed_protocol")
async def run_discussed_protocol(
    name: Annotated[str, Field(description="Name for the protocol")] = "Custom Protocol"
) -> str:
    """Compile and start the discussed protocol from the draft text.
    Use when user says 'run this', 'start it', 'let's go'."""
    state = get_protocol_state()
    draft = state.extra_context.strip()
    if not draft:
        return "No draft protocol text. Describe the steps first."

    from tools.protocols.store import build_protocol_entry, _parse_steps
    steps = _parse_steps(draft)
    if not steps:
        return "Could not parse steps from the draft. Try listing them as numbered items."

    safe_key = name.lower().replace(" ", "_")
    state.session_protocols[safe_key] = build_protocol_entry(name, steps, draft)

    result = await start_protocol(protocol_name=name)
    return result


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
@toggle_dashboard("log_observation")
async def log_observation(
    observation: Annotated[str, Field(description="The observation or data point to record, e.g. 'phone is heavy', 'colonies look dead in dish 3'")],
    section: Annotated[str, Field(description="Category: observations, tube_weights, timings, temperatures, volumes, notes")] = "observations",
) -> str:
    """Log an observation or data point for the current protocol run.
    Use when user says log, note, record, or describes something noteworthy.
    The data is saved with the current step number and timestamp."""
    state = get_protocol_state()
    if not state.is_active or state.mode != "running":
        return "No protocol is currently running."

    row = {"note": observation.strip()}
    if _record_capture(state, section, row):
        state.extra_context = (state.extra_context or "").strip()
        if state.extra_context:
            state.extra_context += "\n\n"
        state.extra_context += state.experiment_data_xml()
        step_info = f"step {state.current_step}" if state.current_step else "current step"
        return f"Logged at {step_info}: {observation.strip()}"
    return "Already recorded that observation."


@function_tool
@toggle_dashboard("query_completed_protocol_data")
async def query_completed_protocol_data(
    protocol_name: Annotated[str, Field(description="Optional protocol name filter, or leave blank for latest")] = "",
) -> str:
    """Query experiment data captured during this session (current or completed runs)."""
    state = get_protocol_state()

    if state.is_active and state.experiment_data.get("sections"):
        sections = state.experiment_data["sections"]
        section_names = ", ".join(sections.keys())
        rows_total = sum(len(s.get("rows", [])) for s in sections.values())
        return (
            f"Current run ({state.protocol_name}): {rows_total} entries across sections: {section_names}. "
            "Say 'show experiment data' with a section name for details."
        )

    runs = list(state.completed_runs)
    if protocol_name:
        q = protocol_name.lower().strip()
        runs = [r for r in runs if q in str(r.get("protocol_name", "")).lower()]

    if not runs:
        return "No experiment data found for this session."

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
    section: Annotated[str, Field(description="Section name like tube_weights, timings, temperatures, volumes, notes, observations")] = "",
) -> str:
    """Show captured experiment data for the current or latest completed run."""
    state = get_protocol_state()

    sections: dict = {}
    source_label = ""
    if state.is_active and state.experiment_data.get("sections"):
        sections = state.experiment_data["sections"]
        source_label = f"current run ({state.protocol_name})"
    elif state.completed_runs:
        latest = state.completed_runs[-1]
        sections = latest.get("experiment_data", {}).get("sections", {})
        source_label = f"completed run ({latest.get('protocol_name', 'Unknown')})"

    if not isinstance(sections, dict) or not sections:
        return "No experiment data found for this session."

    if section:
        key = section.strip().lower().replace(" ", "_")
        payload = sections.get(key)
        if not payload:
            return f"Section '{section}' was not found in {source_label}."
        headers = payload.get("headers", [])
        rows = payload.get("rows", [])
        lines = [f"{key} ({source_label}):"]
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
    return f"Available data sections in {source_label}: {names}."


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


# ---------------------------------------------------------------------------
# LabOS Live event helpers (no-op when not connected)
# ---------------------------------------------------------------------------

async def _emit_labos_protocol_start(state: ProtocolState):
    try:
        from labos_live_client import get_labos_client
        from config import _current_session_id
        client = get_labos_client(_current_session_id.get("default-xr-session"))
        if client and client.connected:
            steps = []
            for i, s in enumerate(state.steps):
                steps.append({
                    "step": i + 1,
                    "short": s.text,
                    "long": s.description or s.text,
                })
            await client.send_protocol_start(state.protocol_name, steps)
    except Exception:
        pass


async def _emit_labos_protocol_stop_event():
    try:
        from labos_live_client import get_labos_client
        from config import _current_session_id
        client = get_labos_client(_current_session_id.get("default-xr-session"))
        if client and client.connected:
            await client.send_protocol_stop()
    except Exception:
        pass


async def _emit_labos_protocol_data(protocol_name: str, data: dict):
    try:
        from labos_live_client import get_labos_client
        from config import _current_session_id
        client = get_labos_client(_current_session_id.get("default-xr-session"))
        if client and client.connected:
            await client.send_protocol_data(protocol_name, data)
    except Exception:
        pass
