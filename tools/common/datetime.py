"""Date/time tool."""

from typing import Annotated

from agents import function_tool
from pydantic import Field
from tools.common.toggle import toggle_dashboard


@function_tool
@toggle_dashboard("get_datetime")
async def get_datetime(
    question: Annotated[str, Field(
        description="What the user wants to know, e.g. 'what time is it', "
        "'what day is today', 'what's the date'"
    )]
) -> str:
    """Get the current date and/or time. Use when the user asks about the
    current time, date, day of the week, month, or year."""
    from datetime import datetime as dt

    now = dt.now()
    q = question.lower()

    if "time" in q:
        time_str = now.strftime("%I:%M %p").lstrip("0")
        if "date" in q or "day" in q:
            return f"It's {time_str} on {now.strftime('%A, %B %d, %Y')}."
        return f"It's {time_str}."
    elif "date" in q or "day" in q:
        if "what day" in q:
            return f"Today is {now.strftime('%A')}."
        return f"Today is {now.strftime('%A, %B %d, %Y')}."
    elif "year" in q:
        return f"It's {now.year}."
    elif "month" in q:
        return f"It's {now.strftime('%B')}."
    else:
        time_str = now.strftime("%I:%M %p").lstrip("0")
        return f"It's {time_str} on {now.strftime('%A, %B %d, %Y')}."
