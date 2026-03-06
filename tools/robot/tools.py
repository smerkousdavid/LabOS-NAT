"""LLM-facing robot tools.

Thin @function_tool wrappers that proxy calls to the connected robot
via RobotConnectionManager.  All tools are gated by toggle_dashboard
and disabled by default until a robot connects.
"""

from typing import Annotated

from agents import function_tool
from pydantic import Field

from tools.common.toggle import toggle_dashboard
from tools.robot import get_robot_manager


def _format_result(result: dict) -> str:
    if result.get("success"):
        return str(result.get("result", "OK"))
    return f"Robot error: {result.get('result', 'unknown error')}"


@function_tool
@toggle_dashboard("robot_get_status")
async def robot_get_status() -> str:
    """Get the current robot status: idle, running a protocol step, etc.
    Use when the user asks about the robot's state or progress."""
    mgr = get_robot_manager()
    if not mgr.is_connected():
        return "No robot is currently connected."
    result = await mgr.call_tool("get_status")
    return _format_result(result)


@function_tool
@toggle_dashboard("robot_list_objects")
async def robot_list_objects() -> str:
    """List objects visible in the robot's camera with locations, colors,
    and depth. Use when the user asks what the robot can see."""
    mgr = get_robot_manager()
    if not mgr.is_connected():
        return "No robot is currently connected."
    result = await mgr.call_tool("list_objects")
    return _format_result(result)


@function_tool
@toggle_dashboard("robot_start_protocol")
async def robot_start_protocol(
    protocol_name: Annotated[str, Field(
        description="Name of the robot protocol to run, e.g. 'vortexing'."
    )]
) -> str:
    """Start a protocol on the connected robot arm. Use when the user
    explicitly asks the robot to run a specific protocol."""
    mgr = get_robot_manager()
    if not mgr.is_connected():
        return "No robot is currently connected."
    result = await mgr.call_tool("start_protocol", {"protocol_name": protocol_name})
    return _format_result(result)


@function_tool
@toggle_dashboard("robot_stop")
async def robot_stop() -> str:
    """Emergency-stop the robot: cancels the current protocol and returns
    to position control mode. Use when the user says stop robot, halt,
    or emergency stop."""
    mgr = get_robot_manager()
    if not mgr.is_connected():
        return "No robot is currently connected."
    result = await mgr.call_tool("stop_robot")
    return _format_result(result)


@function_tool
@toggle_dashboard("robot_gripper")
async def robot_gripper(
    position: Annotated[str, Field(
        description="Gripper position: 'close', 'midway', 'open', or a number 0-800."
    )]
) -> str:
    """Control the robot gripper. Use when the user asks to open, close,
    or adjust the gripper."""
    mgr = get_robot_manager()
    if not mgr.is_connected():
        return "No robot is currently connected."
    result = await mgr.call_tool("gripper", {"position": position})
    return _format_result(result)


@function_tool
@toggle_dashboard("robot_go_home")
async def robot_go_home() -> str:
    """Send the robot arm to its home position. Use when the user says
    home, park, or reset the robot."""
    mgr = get_robot_manager()
    if not mgr.is_connected():
        return "No robot is currently connected."
    result = await mgr.call_tool("go_home")
    return _format_result(result)
