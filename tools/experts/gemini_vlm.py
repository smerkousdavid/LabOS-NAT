"""Gemini Live VLM expert tool.

Exposes ``query_gemini`` as an @function_tool for vision_only mode.
The Gemini Live session has continuous video context, so no frame
capture or special VLM call is needed -- questions go directly to
the persistent session.
"""

from typing import Annotated

from agents import function_tool
from pydantic import Field
from tools.common.toggle import toggle_dashboard


@function_tool
@toggle_dashboard("query_gemini")
async def query_gemini(
    question: Annotated[str, Field(
        description="Visual question about the live camera feed -- "
        "e.g. identifying objects, checking setup, assessing cell cultures, reading labels. "
        "The Gemini Live session has continuous video context."
    )]
) -> str:
    """Ask Gemini about what the camera sees. The Gemini Live session has
    continuous video context from the AR glasses, so it can answer visual
    questions directly without needing to capture frames."""
    from tools.vsop_providers import get_vsop_provider
    from config import get_config

    provider = get_vsop_provider()
    if provider is None:
        from tools.vsop_providers import init_vsop_provider
        provider = init_vsop_provider(get_config())

    if provider.is_active:
        return await provider.query(question)

    return await provider.query_standalone(question)
