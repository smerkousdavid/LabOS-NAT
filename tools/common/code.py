"""Safe code execution tool.

Executes Python code in a restricted sandbox.
The agent LLM should generate the Python code itself -- this tool only
runs code, it does not generate it.
"""

import ast
import builtins
import contextlib
import io
import traceback
from typing import Annotated, Any, Dict

from agents import function_tool
from pydantic import Field
from tools.common.toggle import toggle_dashboard


class CodeExecutor:
    """Safe Python code executor."""

    SAFE_BUILTINS = {
        "abs", "all", "any", "ascii", "bin", "bool", "bytes",
        "chr", "dict", "divmod", "enumerate", "filter", "float",
        "format", "hex", "int", "isinstance", "len", "list", "map",
        "max", "min", "oct", "ord", "pow", "print", "range",
        "repr", "reversed", "round", "set", "sorted", "str",
        "sum", "tuple", "type", "zip",
    }

    SAFE_MODULES = {
        "math", "statistics", "datetime", "json", "random",
        "collections", "itertools", "functools", "decimal",
    }

    def __init__(self, timeout: int = 5):
        self.timeout = timeout

    def _is_safe(self, code: str) -> tuple[bool, str]:
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in self.SAFE_MODULES:
                        return False, f"Import not allowed: {alias.name}"
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] not in self.SAFE_MODULES:
                    return False, f"Import not allowed: {node.module}"
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("eval", "exec", "compile", "__import__"):
                        return False, f"Function not allowed: {node.func.id}"
            if isinstance(node, ast.Name):
                if node.id in ("open", "file"):
                    return False, "File operations not allowed"

        return True, ""

    def execute(self, code: str, variables: Dict[str, Any] = None) -> Dict[str, Any]:
        is_safe, error_msg = self._is_safe(code)
        if not is_safe:
            return {"success": False, "error": error_msg, "output": "", "result": None}

        safe_builtins = {
            name: getattr(builtins, name)
            for name in self.SAFE_BUILTINS
            if hasattr(builtins, name)
        }

        def safe_import(name, *args, **kwargs):
            if name.split('.')[0] in self.SAFE_MODULES:
                return __import__(name, *args, **kwargs)
            raise ImportError(f"Import not allowed: {name}")

        safe_builtins['__import__'] = safe_import
        safe_globals = {"__builtins__": safe_builtins}

        import math, statistics, datetime, json, random
        import collections, itertools, functools, decimal
        safe_globals.update({
            'math': math, 'statistics': statistics, 'datetime': datetime,
            'json': json, 'random': random, 'collections': collections,
            'itertools': itertools, 'functools': functools, 'decimal': decimal,
        })

        if variables:
            safe_globals.update(variables)

        output_buffer = io.StringIO()
        error_buffer = io.StringIO()
        result = None

        try:
            with contextlib.redirect_stdout(output_buffer), \
                 contextlib.redirect_stderr(error_buffer):
                exec(code, safe_globals)
            try:
                tree = ast.parse(code)
                if tree.body and isinstance(tree.body[-1], ast.Expr):
                    result = eval(
                        compile(ast.Expression(tree.body[-1].value), "<string>", "eval"),
                        safe_globals,
                    )
            except Exception:
                pass
            return {
                "success": True,
                "output": output_buffer.getvalue(),
                "error": error_buffer.getvalue(),
                "result": result,
            }
        except Exception:
            return {
                "success": False,
                "output": output_buffer.getvalue(),
                "error": traceback.format_exc(),
                "result": None,
            }


_executor = None


def _get_executor() -> CodeExecutor:
    global _executor
    if _executor is None:
        _executor = CodeExecutor()
    return _executor


def execute_code(code: str) -> str:
    """Execute Python code safely and return results."""
    result = _get_executor().execute(code)
    response = []
    if result["success"]:
        if result["result"] is not None:
            response.append(str(result["result"]))
        elif result["output"]:
            response.append(result["output"].strip())
        if result["error"]:
            response.append(f"Warning: {result['error']}")
    else:
        response.append(f"Error: {result['error']}")
        if result["output"]:
            response.append(result["output"].strip())
    return "\n".join(response) if response else "No output"


# ---------------------------------------------------------------------------
# Agent tool
# ---------------------------------------------------------------------------

@function_tool
@toggle_dashboard("run_code")
async def run_code(
    code: Annotated[str, Field(
        description="Python code to execute. Must use print() to produce output. "
        "Allowed modules: math, statistics, datetime, json, random, "
        "collections, itertools, functools, decimal."
    )]
) -> str:
    """Execute Python code in a sandboxed environment and return the output.
    Use when the user asks to calculate, compute, or run code.
    You must generate the Python code yourself and pass it here."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, execute_code, code.strip())
