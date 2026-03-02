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
        description="Question about the experiment, what the camera sees, "
        "technique guidance, reagent identification, or safety assessment"
    )]
) -> str:
    """Ask STELLA-VLM about what the user is looking at. STELLA is a specialized
    vision-language model that can see the user's live camera feed and has
    deep knowledge of laboratory protocols. Works at ALL times -- with or
    without a running protocol. Route visual questions, environment questions,
    technique questions, and domain-expert questions here."""
    from tools.vsop_providers import get_vsop_provider, init_vsop_provider
    from config import get_config

    provider = get_vsop_provider()
    if provider is None:
        provider = init_vsop_provider(get_config())

    if provider.is_active:
        return await provider.query(question)

    return await provider.query_standalone(question)
