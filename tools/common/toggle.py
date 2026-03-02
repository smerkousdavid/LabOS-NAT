"""Dashboard-controlled tool toggle decorator."""

from __future__ import annotations

from functools import wraps

from config import get_tool_enabled


def toggle_dashboard(tool_name: str):
    """Block tool execution when disabled from dashboard/NAT toggle state."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if not get_tool_enabled(tool_name, default=True):
                return (
                    f"Tool '{tool_name}' is disabled in dashboard settings. "
                    "Please enable it before using this operation."
                )
            return await func(*args, **kwargs)

        return wrapper

    return decorator
