#!/usr/bin/env python3
"""Grok / ChatGPT bridge for the macOS Remote Work MCP server.

FastAPI service on 127.0.0.1:8010 that accepts Grok-style function calls and
routes them to the MCP server (127.0.0.1:8000) over MCP streamable HTTP.

Endpoints:
    GET  /health  - liveness + tool count
    GET  /tools   - available tool list (OpenAI/Grok function schemas)
    POST /tool    - {tool: str, arguments: dict} -> MCP tool result JSON
"""

import json
from pathlib import Path
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fastmcp import Client

try:  # fastmcp >= 2
    from fastmcp.exceptions import ToolError
except Exception:  # pragma: no cover - version fallback
    try:
        from fastmcp.client.client import ToolError  # type: ignore
    except Exception:
        ToolError = None  # type: ignore

BASE_DIR = Path(__file__).resolve().parent
TOOLS_FILE = BASE_DIR / "chatgpt_tools.json"
MCP_URL = "http://127.0.0.1:8000/mcp"
HOST = "127.0.0.1"
PORT = 8010

app = FastAPI(
    title="macOS Remote Work Bridge",
    description="Routes Grok/ChatGPT function calls to the local MCP server.",
    version="1.0.0",
)


def _load_tools() -> Dict[str, Dict[str, Any]]:
    """Load the OpenAI-compatible function schemas from chatgpt_tools.json."""
    try:
        raw = json.loads(TOOLS_FILE.read_text(encoding="utf-8"))
        functions = raw.get("tools", [])
        return {
            fn["function"]["name"]: fn["function"]
            for fn in functions
            if isinstance(fn, dict) and "function" in fn
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


TOOLS: Dict[str, Dict[str, Any]] = _load_tools()


class ToolRequest(BaseModel):
    tool: str = Field(..., description="Name of the tool to call")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Tool arguments")


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body = {"success": False, "error": code, "message": message}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Liveness probe used by setup.sh."""
    return {
        "status": "ok",
        "service": "macos-remote-work-bridge",
        "mcp_server": MCP_URL,
        "tools": len(TOOLS),
    }


@app.get("/tools")
async def list_tools() -> Dict[str, Any]:
    """Return the available tool list in a Grok/OpenAI-compatible format."""
    return {
        "tools": [
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            }
            for fn in TOOLS.values()
        ]
    }


@app.post("/tool")
async def call_tool(request: ToolRequest) -> Any:
    """Map a Grok function call {tool, arguments} onto the MCP server."""
    tool = request.tool.strip()
    if tool not in TOOLS:
        return _error(
            404,
            "unknown_tool",
            f"Unknown tool '{tool}'.",
            available_tools=sorted(TOOLS.keys()),
        )

    schema = TOOLS[tool]
    required = schema.get("parameters", {}).get("required", []) or []
    missing = [name for name in required if name not in request.arguments]
    if missing:
        return _error(
            400,
            "missing_required_arguments",
            f"Missing required argument(s) for '{tool}': {', '.join(missing)}.",
            missing=missing,
        )

    try:
        async with Client(MCP_URL) as client:
            result = await client.call_tool(tool, request.arguments)
    except Exception as exc:  # network / protocol / tool errors
        if ToolError is not None and isinstance(exc, ToolError):
            return JSONResponse(
                status_code=200,
                content={
                    "success": False,
                    "tool": tool,
                    "error": "tool_error",
                    "message": str(exc),
                },
            )
        return _error(
            503,
            "mcp_unreachable",
            f"MCP server at {MCP_URL} is unreachable. Start it with: python mcp_server.py",
            detail=str(exc)[:500],
        )

    data = getattr(result, "data", None)
    if data is None:
        data = {"success": not getattr(result, "is_error", False)}
    if not isinstance(data, dict):
        data = {"success": True, "result": data}
    if "tool" not in data:
        data["tool"] = tool
    if "success" not in data:
        data["success"] = not getattr(result, "is_error", False)
    return JSONResponse(status_code=200, content=data)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
