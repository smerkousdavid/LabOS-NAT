"""Rich-text panel builder and LLM formatting utility for XR display.

Provides two modes for constructing XR overlay panels:
  1. **RichPanelBuilder** -- fluent API for manual panel construction
  2. **llm_format_panel()** -- asks the LLM to generate TMP rich text

Both produce a ``List[Dict[str, str]]`` block array suitable for
``render_rich_panel()`` in ``tools.display.ui``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TMP_TAG_REFERENCE = """\
Supported Unity TextMeshPro rich-text tags for XR display (~480px wide, vertical layout):
  <size=N>...</size>      -- font size in points (default ~18, titles ~22-24)
  <color=#HEX>...</color> -- hex color (accent: #59D2FF, muted: #888888, warn: #FF4444)
  <b>...</b>              -- bold
  <i>...</i>              -- italic
  <u>...</u>              -- underline
  <s>...</s>              -- strikethrough
  <br>                    -- line break
  <sup>...</sup>          -- superscript
  <sub>...</sub>          -- subscript
  <mark=#HEX>...</mark>   -- highlight
  <align=left|center|right>...</align> -- alignment
  <link="URL">text</link>             -- clickable link (Unity TMP)
Keep content concise; the screen is narrow and mobile-sized.\
"""

# ---------------------------------------------------------------------------
# RichPanelBuilder -- fluent API
# ---------------------------------------------------------------------------

class RichPanelBuilder:
    """Build an XR panel block array with a chainable API."""

    def __init__(self):
        self._blocks: List[Dict[str, str]] = []

    def image(self, base64_data: str) -> "RichPanelBuilder":
        """Add a base64-encoded image block."""
        self._blocks.append({"type": "base64-image", "content": base64_data})
        return self

    def title(self, text: str, color: str = "#59D2FF") -> "RichPanelBuilder":
        self._blocks.append({
            "type": "rich-text",
            "content": f"<size=22><b><color={color}>{text}</color></b></size><br>",
        })
        return self

    def body(self, text: str, size: int = 18) -> "RichPanelBuilder":
        self._blocks.append({
            "type": "rich-text",
            "content": f"<size={size}>{text}</size><br>",
        })
        return self

    def caption(self, text: str) -> "RichPanelBuilder":
        self._blocks.append({
            "type": "rich-text",
            "content": f"<size=14><color=#888888>{text}</color></size>",
        })
        return self

    def divider(self) -> "RichPanelBuilder":
        self._blocks.append({
            "type": "rich-text",
            "content": "<br><color=#444444>────────────────────</color><br>",
        })
        return self

    def link(self, text: str, url: str) -> "RichPanelBuilder":
        """Add a clickable TMP link block."""
        self._blocks.append({
            "type": "rich-text",
            "content": f'<size=16><link="{url}"><u><color=#59D2FF>{text}</color></u></link></size><br>',
        })
        return self

    def raw(self, tmp_string: str) -> "RichPanelBuilder":
        """Add a raw TMP-formatted rich-text block."""
        self._blocks.append({"type": "rich-text", "content": tmp_string})
        return self

    def build(self) -> List[Dict[str, str]]:
        return list(self._blocks)


# ---------------------------------------------------------------------------
# push_to_display -- send blocks to XR
# ---------------------------------------------------------------------------

async def push_to_display(blocks: List[Dict[str, str]]) -> None:
    """Push a rich panel to the XR display via ``ui.render_rich_panel``."""
    from tools.display.ui import render_rich_panel
    await render_rich_panel(blocks)


# ---------------------------------------------------------------------------
# llm_format_panel -- ask the LLM to generate TMP rich text
# ---------------------------------------------------------------------------

async def llm_format_panel(
    data: str,
    instructions: Optional[str] = None,
) -> str:
    """Call the router LLM to format *data* as TMP rich text for the XR display.

    Returns a TMP-formatted string suitable for a ``rich-text`` block.
    """
    from config import get_llm_client

    prompt_parts = [
        "You are a text formatter for an AR heads-up display.",
        TMP_TAG_REFERENCE,
        "",
        "Format the following data as rich text for the display.",
        "Output ONLY the formatted TMP rich-text string, no explanation.",
    ]
    if instructions:
        prompt_parts.append(f"Additional instructions: {instructions}")
    prompt_parts.append(f"\nData:\n{data}")

    system_msg = "\n".join(prompt_parts)

    def _call():
        client, model = get_llm_client("router")
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": system_msg}],
            max_tokens=300,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()

    return await asyncio.get_event_loop().run_in_executor(None, _call)


# ---------------------------------------------------------------------------
# build_web_search_panel -- used by web_search tool
# ---------------------------------------------------------------------------

async def build_web_search_panel(
    query: str,
    text_results: List[dict],
    ai_overview: str = "",
    inline_images: Optional[List[dict]] = None,
) -> None:
    """Build and push a rich overlay panel for web search results.

    Uses inline_images from SerpAPI (if available) for the panel image,
    avoiding a separate image search call.  Shows ai_overview as the main
    body when present.
    """
    from tools.common.web import fetch_image_as_base64

    builder = RichPanelBuilder()

    image_b64: Optional[str] = None
    if inline_images:
        for img in inline_images[:3]:
            url = img.get("original", "") or img.get("thumbnail", "")
            if url:
                image_b64 = await fetch_image_as_base64(url)
                if image_b64:
                    break

    if image_b64:
        builder.image(image_b64)

    builder.title(query)

    if ai_overview:
        builder.body(ai_overview[:300], size=17)
        builder.divider()

    for i, r in enumerate(text_results[:3], 1):
        title = r.get("title", "No title")
        body = (r.get("body", "") or "")[:120]
        href = r.get("href", "")
        source = _short_domain(href)
        result_text = (
            f"<b>{i}. {title}</b><br>"
            f"<size=16>{body}</size><br>"
            f"<size=14><color=#888888>{source}</color></size>"
        )
        builder.body(result_text, size=18)
        # if href:
        #     builder.link(source, href)

    await push_to_display(builder.build())


def _short_domain(url: str) -> str:
    try:
        return urlparse(url).netloc or ""
    except Exception:
        return ""
