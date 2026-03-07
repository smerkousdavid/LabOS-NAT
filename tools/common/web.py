"""Web and image search tools.

When SERPAPI_KEY is set, uses the SerpAPI ``google-search-results`` library
for Google Search (text) and Google Images Light (images).
Falls back to DuckDuckGo via ``ddgs`` otherwise.

Returns compact results to stay within the LLM context window.
Also provides a helper to download an image URL as base64 for the XR display.
"""

import asyncio
import base64
import logging
import os
import time
from typing import Annotated, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from agents import function_tool
from pydantic import Field
from tools.common.toggle import toggle_dashboard

logger = logging.getLogger(__name__)

_SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()
_USE_SERP = bool(_SERPAPI_KEY)

if _USE_SERP:
    logger.info("Web search: using SerpAPI (SERPAPI_KEY set)")
else:
    logger.info("Web search: using DuckDuckGo (SERPAPI_KEY not set)")


class SearchServiceError(Exception):
    """Raised when the search backend is unreachable or fails."""


# ---------------------------------------------------------------------------
# SerpAPI helpers
# ---------------------------------------------------------------------------

def _serp_text_search(query: str, max_results: int = 3) -> Dict:
    """Google Search via SerpAPI (engine=google).

    Returns a dict with:
      - ai_overview:     str (joined text_blocks from ai_overview, or "")
      - inline_images:   list of {thumbnail, original, title, source_name}
      - organic_results: list of {title, body, href}
    """
    from serpapi import GoogleSearch

    params = {
        "engine": "google",
        "q": query,
        "api_key": _SERPAPI_KEY,
        "num": min(max_results, 10),
    }
    data = GoogleSearch(params).get_dict()

    ai_overview = ""
    ai_block = data.get("ai_overview", {})
    if isinstance(ai_block, dict):
        text_blocks = ai_block.get("text_blocks", [])
        paragraphs = []
        for block in text_blocks:
            if isinstance(block, dict):
                snippet = block.get("snippet") or block.get("text", "")
                if snippet:
                    paragraphs.append(snippet)
            elif isinstance(block, str):
                paragraphs.append(block)
        ai_overview = "\n".join(paragraphs)

    inline_images: List[dict] = []
    for img in data.get("inline_images", []):
        inline_images.append({
            "thumbnail": img.get("thumbnail", ""),
            "original": img.get("original", ""),
            "title": img.get("title", ""),
            "source_name": img.get("source_name", ""),
        })

    organic: List[dict] = []
    for item in data.get("organic_results", [])[:max_results]:
        organic.append({
            "title": item.get("title", ""),
            "body": item.get("snippet", ""),
            "href": item.get("link", ""),
        })

    return {
        "ai_overview": ai_overview,
        "inline_images": inline_images,
        "organic_results": organic,
    }


def _serp_image_search(query: str, max_results: int = 10) -> List[dict]:
    """Google Images Light via SerpAPI (engine=google_images_light).

    Returns list of {title, image, thumbnail, source, link}.
    """
    from serpapi import GoogleSearch

    params = {
        "engine": "google_images_light",
        "q": query,
        "api_key": _SERPAPI_KEY,
        "num": min(max_results, 20),
    }
    data = GoogleSearch(params).get_dict()
    results: List[dict] = []
    for item in data.get("images_results", [])[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "image": item.get("original", item.get("thumbnail", "")),
            "thumbnail": item.get("thumbnail", ""),
            "source": item.get("source", ""),
            "link": item.get("link", ""),
        })
    return results


# ---------------------------------------------------------------------------
# DuckDuckGo fallback helpers
# ---------------------------------------------------------------------------

_DDG_MAX_RETRIES = 3
_DDG_BASE_DELAY = 2.0


def _ddg_text_search(query: str, max_results: int = 3) -> List[dict]:
    """DuckDuckGo text search with exponential backoff retry."""
    from ddgs import DDGS
    last_exc = None
    for attempt in range(_DDG_MAX_RETRIES):
        try:
            return list(DDGS().text(query, max_results=max_results))
        except Exception as exc:
            last_exc = exc
            if attempt < _DDG_MAX_RETRIES - 1:
                delay = _DDG_BASE_DELAY * (2 ** attempt)
                logger.debug(f"DDG text search attempt {attempt + 1} failed, retry in {delay}s: {exc}")
                time.sleep(delay)
    raise SearchServiceError(str(last_exc)) from last_exc


def _ddg_image_search(query: str, max_results: int = 3) -> List[dict]:
    """DuckDuckGo image search with exponential backoff retry."""
    from ddgs import DDGS
    last_exc = None
    for attempt in range(_DDG_MAX_RETRIES):
        try:
            return list(DDGS().images(query, max_results=max_results))
        except Exception as exc:
            last_exc = exc
            if attempt < _DDG_MAX_RETRIES - 1:
                delay = _DDG_BASE_DELAY * (2 ** attempt)
                logger.debug(f"DDG image search attempt {attempt + 1} failed, retry in {delay}s: {exc}")
                time.sleep(delay)
    raise SearchServiceError(str(last_exc)) from last_exc


# ---------------------------------------------------------------------------
# Unified search interface — picks backend automatically
# ---------------------------------------------------------------------------

def _text_search(query: str, max_results: int = 3) -> Dict:
    """Unified text search dispatcher.

    Returns a dict with:
      - ai_overview:     str (SerpAPI only, empty for DDG)
      - inline_images:   list (SerpAPI only, empty for DDG)
      - organic_results: list of {title, body, href}
    """
    if _USE_SERP:
        try:
            return _serp_text_search(query, max_results)
        except Exception as exc:
            logger.warning(f"SerpAPI text search failed, falling back to DDG: {exc}")

    ddg_results = _ddg_text_search(query, max_results)
    organic = []
    for r in ddg_results:
        organic.append({
            "title": r.get("title", ""),
            "body": r.get("body", r.get("snippet", "")),
            "href": r.get("href", r.get("link", "")),
        })
    return {
        "ai_overview": "",
        "inline_images": [],
        "organic_results": organic,
    }


def _image_search(query: str, max_results: int = 10) -> List[dict]:
    """Try image search backends in order, requesting up to *max_results*
    candidates so the caller has many URLs to try before giving up."""
    collected: List[dict] = []

    if _USE_SERP:
        try:
            collected.extend(_serp_image_search(query, max_results))
        except Exception as exc:
            logger.warning(f"SerpAPI image search failed: {exc}")

    if len(collected) < max_results:
        try:
            collected.extend(_ddg_image_search(query, max_results))
        except Exception as exc:
            logger.warning(f"DDG image search failed: {exc}")

    if len(collected) < max_results:
        try:
            for r in _ddg_text_search(query, max_results):
                href = r.get("href", r.get("link", ""))
                if href and _looks_like_image_url(href):
                    collected.append({
                        "title": r.get("title", ""),
                        "image": href,
                        "source": _short_domain(href),
                    })
        except Exception as exc:
            logger.warning(f"DDG text-to-image fallback failed: {exc}")

    if collected:
        return collected[:max_results]
    raise SearchServiceError("All image search methods exhausted")


def _looks_like_image_url(url: str) -> bool:
    """Heuristic: does the URL path end in a common image extension?"""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _short_domain(url: str) -> str:
    """Extract a short domain from a URL (e.g. 'en.wikipedia.org')."""
    try:
        return urlparse(url).netloc or url[:40]
    except Exception:
        return url[:40]


async def fetch_image_as_base64(url: str, max_size_bytes: int = 500_000) -> Optional[str]:
    """Download an image URL and return its base64 encoding.

    Returns None on failure. Images larger than *max_size_bytes* are skipped
    to avoid blowing up the XR panel payload.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; LabOS/1.0)"
            })
            resp.raise_for_status()
            if len(resp.content) > max_size_bytes:
                logger.debug(f"Image too large ({len(resp.content)} bytes), skipping: {url}")
                return None
            return base64.b64encode(resp.content).decode("ascii")
    except Exception as exc:
        logger.warning(f"Failed to fetch image as base64: {exc}")
        return None


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

@function_tool
@toggle_dashboard("web_search")
async def web_search(
    query: Annotated[str, Field(
        description="The search query, e.g. 'how does a micropipette work'"
    )],
    show_on_display: Annotated[bool, Field(
        description="If True (default), results are automatically shown as a "
        "rich overlay on the AR display with an image when relevant."
    )] = True,
) -> str:
    """Search the web for current information. Results are automatically
    displayed on the AR glasses as a rich panel with images when available.
    You should give a brief spoken summary of the top result -- do NOT
    repeat all results verbatim."""
    try:
        loop = asyncio.get_running_loop()
        search_result = await loop.run_in_executor(None, _text_search, query, 3)
    except SearchServiceError:
        return (
            f"Web search service is currently unavailable. "
            f"Unable to search for '{query}'. Try again in a moment."
        )

    organic = search_result.get("organic_results", [])
    if not organic:
        return f"No results found for '{query}'."

    if show_on_display:
        try:
            from tools.common.rich_panel import build_web_search_panel
            await build_web_search_panel(
                query,
                organic,
                ai_overview=search_result.get("ai_overview", ""),
                inline_images=search_result.get("inline_images", []),
            )
        except Exception as exc:
            logger.warning(f"Failed to build display panel: {exc}")

    ai_overview = search_result.get("ai_overview", "")
    if ai_overview:
        summary = ai_overview[:400]
        return f"AI Overview for '{query}':\n{summary}"

    lines = [f"Web results for '{query}':"]
    for i, r in enumerate(organic[:3], 1):
        title = r.get("title", "No title")
        body = (r.get("body", "") or "")[:100]
        lines.append(f"{i}. {title} -- {body}")

    return "\n".join(lines)


def _get_current_step_explicit_image():
    """Return the current step's embedded image base64 if present."""
    try:
        from tools.protocols.state import get_protocol_state
        state = get_protocol_state()
        detail = state.current_step_detail()
        if detail and detail.image_base64:
            return detail.image_base64
    except Exception:
        pass
    return None


async def _validate_candidate_image(image_url: str, description: str) -> str | None:
    """Fetch an image URL, validate via VLM, return base64 if valid."""
    import base64
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(image_url)
            if resp.status_code != 200 or len(resp.content) < 500:
                return None
            b64 = base64.b64encode(resp.content).decode("ascii")
            if len(b64) < 200:
                return None
        try:
            from tools.vsop_providers import get_vsop_provider
            provider = get_vsop_provider()
            if provider:
                valid = await provider.validate_external_image(b64, description)
                if not valid:
                    return None
        except Exception:
            pass
        return b64
    except Exception:
        return None


@function_tool
@toggle_dashboard("image_search")
async def image_search(
    query: Annotated[str, Field(
        description="Image search query, e.g. 'picture of a cat'"
    )]
) -> str:
    """Search the web for images and display the best one on the AR glasses.
    The image is automatically shown on the XR panel -- just give a brief
    spoken description of what was found."""
    explicit = _get_current_step_explicit_image()
    if explicit:
        from tools.display.ui import render_rich_panel
        await render_rich_panel([
            {"type": "base64-image", "content": explicit},
            {"type": "rich-text", "content": f"<size=16><color=#D9D8FF>Step image for: {query}</color></size>"},
        ])
        return f"Displaying step image for '{query}'."

    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, _image_search, query, 10)
    except SearchServiceError:
        return (
            f"The image search service is currently unavailable. "
            f"Unable to search for '{query}'. Try again in a moment."
        )

    if not results:
        return f"No images found for '{query}'."

    displayed = False
    display_title = query
    display_source = ""
    for r in results:
        for url_key in ("image", "thumbnail", "url"):
            image_url = r.get(url_key, "")
            if not image_url:
                continue
            validated_b64 = await _validate_candidate_image(image_url, query)
            image_b64 = validated_b64 if validated_b64 else await fetch_image_as_base64(image_url)
            if image_b64:
                try:
                    from tools.common.rich_panel import RichPanelBuilder, push_to_display
                    display_title = r.get("title", query)
                    display_source = _short_domain(r.get("source", image_url))
                    panel = (
                        RichPanelBuilder()
                        .image(image_b64)
                        .title(query)
                        .caption(f"{display_title} — {display_source}")
                        .build()
                    )
                    await push_to_display(panel)
                    displayed = True
                except Exception as exc:
                    logger.warning(f"Failed to push image panel: {exc}")
                break
        if displayed:
            break

    top = results[0]
    title = top.get("title", "No title")
    source = _short_domain(top.get("source", top.get("url", top.get("image", ""))))
    if displayed:
        return f"Image displayed on AR panel. Showing: {display_title} (from {display_source})"
    return f"Found {len(results)} image results but could not display any. Top result: {title} (from {source})"
