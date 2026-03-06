"""STELLA-VLM expert tool.

Exposes ``query_stella`` as an @function_tool that the agent can call
for ad-hoc visual questions.  Works both during protocol execution
(uses protocol context) and standalone (general visual Q&A).
"""

from typing import Annotated

from agents import function_tool
from pydantic import Field
from tools.common.toggle import toggle_dashboard


@function_tool
@toggle_dashboard("query_stella")
async def query_stella(
    question: Annotated[str, Field(
        description="Visual question requiring the live camera feed -- "
        "e.g. identifying objects, checking setup, assessing cell cultures, reading labels"
    )]
) -> str:
    """Ask STELLA-VLM about what the camera sees RIGHT NOW. Only use for
    visual questions requiring live camera feed analysis: identifying objects,
    checking equipment setup, assessing cell cultures, reading labels, etc.
    Do NOT use for general protocol questions -- answer those directly."""
    from tools.vsop_providers import get_vsop_provider, init_vsop_provider
    from config import get_config

    provider = get_vsop_provider()
    if provider is None:
        provider = init_vsop_provider(get_config())

    if provider.is_active:
        return await provider.query(question)

    return await provider.query_standalone(question)
