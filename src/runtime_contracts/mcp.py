"""MCP transport probing — stdio, SSE, and streamable HTTP."""

from __future__ import annotations

import sys
import time
import json
from datetime import timedelta
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

SAFE_TOOL_PREFIXES = ("list", "get", "read", "search", "check")


def dump_model(value: Any) -> Any:
    """Serialize an MCP model object to a plain dict."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def mcp_content_preview(result_dump: dict[str, Any]) -> str:
    """Extract a short text preview from an MCP tool result."""
    previews: list[str] = []
    for item in result_dump.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            previews.append(str(item.get("text", "")))
    if previews:
        return "\n".join(previews)[:400]
    structured = result_dump.get("structuredContent")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)[:400]
    return ""


def mcp_case(name: str, ok: bool, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a standardized MCP test-case dict."""
    return {
        "case": name,
        "ok": ok,
        "tool": payload.get("tool"),
        "is_error": payload.get("is_error"),
        "error": payload.get("error"),
        "body_preview": payload.get("body_preview", ""),
        "result": payload.get("result"),
    }


def choose_safe_mcp_tool(spec: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick a safe (read-only, no required args) MCP tool from the tools list."""
    explicit = spec.get("safe_tool") or {}
    if explicit.get("name"):
        return {
            "name": explicit["name"],
            "arguments": explicit.get("arguments") or {},
            "contains": explicit.get("contains", []),
            "expect_error": bool(explicit.get("expect_error")),
        }
    for tool in tools:
        name = str(tool.get("name", ""))
        schema = tool.get("inputSchema") or {}
        required = schema.get("required") or []
        if not required and name.startswith(SAFE_TOOL_PREFIXES):
            return {
                "name": name,
                "arguments": {},
                "contains": [],
                "expect_error": False,
            }
    return None


async def invoke_mcp_tool(
    session: ClientSession, tool_spec: dict[str, Any], timeout_seconds: int
) -> dict[str, Any]:
    """Call an MCP tool and evaluate its result."""
    name = tool_spec["name"]
    arguments = tool_spec.get("arguments") or {}
    contains = tool_spec.get("contains") or []
    expect_error = bool(tool_spec.get("expect_error"))
    try:
        result = await session.call_tool(
            name, arguments, read_timeout_seconds=timedelta(seconds=timeout_seconds)
        )
        result_dump = dump_model(result)
        body_preview = mcp_content_preview(result_dump)
        ok = bool(result_dump.get("isError")) if expect_error else not bool(result_dump.get("isError"))
        for needle in contains:
            if needle not in body_preview:
                ok = False
        return {
            "ok": ok,
            "tool": name,
            "arguments": arguments,
            "contains": contains,
            "is_error": bool(result_dump.get("isError")),
            "body_preview": body_preview,
            "result": result_dump,
        }
    except Exception as e:
        return {
            "ok": False,
            "tool": name,
            "arguments": arguments,
            "contains": contains,
            "is_error": True,
            "error": str(e),
            "body_preview": "",
        }


async def run_mcp_stdio_probe_async(spec: dict[str, Any]) -> dict[str, Any]:
    """Probe an MCP server over stdio transport."""
    timeout_seconds = int(spec.get("timeout_seconds", 10))
    log_path = spec.get("stderr_log")
    errlog = open(log_path, "a", encoding="utf-8") if log_path else sys.stderr
    try:
        server = StdioServerParameters(
            command=spec["command"],
            args=spec.get("args", []),
            env=spec.get("env"),
            cwd=spec.get("cwd"),
        )
        async with stdio_client(server, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            ) as session:
                init = dump_model(await session.initialize())
                init_ok = (
                    bool(init.get("serverInfo", {}).get("name"))
                    and init.get("protocolVersion") is not None
                )

                tools_result = dump_model(await session.list_tools())
                tools = tools_result.get("tools", [])
                tool_names = [t.get("name") for t in tools if isinstance(t, dict)]
                tools_ok = isinstance(tools, list) and len(tools) > 0 and all(tool_names)

                safe_tool_spec = choose_safe_mcp_tool(spec, tools)
                safe_result = await invoke_mcp_tool(session, safe_tool_spec, timeout_seconds) if safe_tool_spec else None

                integration_result = await invoke_mcp_tool(session, spec["integration_tool"], timeout_seconds) if spec.get("integration_tool") else None
                failure_result = await invoke_mcp_tool(session, spec["failure_tool"], timeout_seconds) if spec.get("failure_tool") else None

                return _build_mcp_result(
                    "stdio", init, init_ok, tools, tool_names, tools_ok,
                    safe_result, integration_result, failure_result,
                )
    finally:
        if log_path:
            errlog.close()


async def run_mcp_http_probe_async(spec: dict[str, Any]) -> dict[str, Any]:
    """Probe an MCP server over HTTP/SSE transport."""
    transport = spec.get("transport", "sse")
    url = spec.get("url")
    headers = spec.get("headers", {})
    timeout_seconds = int(spec.get("timeout_seconds", 10))
    read_timeout = int(spec.get("read_timeout_seconds", 300))

    if not url:
        return _empty_mcp_result(transport, "Missing 'url' for HTTP/SSE transport")

    client_fn = sse_client if transport == "sse" else streamablehttp_client
    try:
        async with client_fn(
            url=url, headers=headers, timeout=timeout_seconds,
            sse_read_timeout=read_timeout,
        ) as streams:
            if transport == "streamable_http":
                read_stream, write_stream, _ = streams
            else:
                read_stream, write_stream = streams

            async with ClientSession(
                read_stream, write_stream,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            ) as session:
                init = dump_model(await session.initialize())
                init_ok = (
                    bool(init.get("serverInfo", {}).get("name"))
                    and init.get("protocolVersion") is not None
                )
                tools_result = dump_model(await session.list_tools())
                tools = tools_result.get("tools", [])
                tool_names = [t.get("name") for t in tools if isinstance(t, dict)]
                tools_ok = isinstance(tools, list) and len(tools) > 0 and all(tool_names)

                safe_tool_spec = choose_safe_mcp_tool(spec, tools)
                safe_result = await invoke_mcp_tool(session, safe_tool_spec, timeout_seconds) if safe_tool_spec else None
                integration_result = await invoke_mcp_tool(session, spec["integration_tool"], timeout_seconds) if spec.get("integration_tool") else None
                failure_result = await invoke_mcp_tool(session, spec["failure_tool"], timeout_seconds) if spec.get("failure_tool") else None

                result = _build_mcp_result(
                    transport, init, init_ok, tools, tool_names, tools_ok,
                    safe_result, integration_result, failure_result,
                )
                result["url"] = url
                return result
    except Exception as e:
        error = str(e)
        if hasattr(e, "exceptions"):
            sub = ", ".join(str(ex) for ex in e.exceptions[:3])
            error = f"{error} — caused by: {sub}"
        elif hasattr(e, "__context__") and e.__context__:
            error = f"{error} — caused by: {e.__context__}"
        return _empty_mcp_result(transport, error, url=url)


def _build_mcp_result(
    transport: str, init: dict, init_ok: bool,
    tools: list, tool_names: list[str], tools_ok: bool,
    safe_result: dict | None, integration_result: dict | None,
    failure_result: dict | None,
) -> dict[str, Any]:
    """Assemble a normalized MCP probe result dict."""
    health_preview = json.dumps(
        {
            "server": init.get("serverInfo", {}).get("name"),
            "protocolVersion": init.get("protocolVersion"),
            "tools": tool_names,
        },
        ensure_ascii=False,
    )[:400]
    return {
        "ok": init_ok and tools_ok,
        "transport": transport,
        "initialize": init,
        "tools_list": {"tools": tools},
        "health": {
            "ok": init_ok and tools_ok,
            "server_name": init.get("serverInfo", {}).get("name"),
            "protocol_version": init.get("protocolVersion"),
            "tool_count": len(tool_names),
            "tool_names": tool_names,
            "body_preview": health_preview,
        },
        "smoke": {
            "ok": bool(safe_result and safe_result["ok"]),
            "cases": [mcp_case("safe_tool_call", bool(safe_result and safe_result["ok"]), safe_result or {"error": "No safe MCP tool available"})],
        },
        "integration": {
            "ok": bool(integration_result and integration_result["ok"]),
            "cases": [mcp_case("integration_tool_call", bool(integration_result and integration_result["ok"]), integration_result or {"error": "No integration MCP tool configured"})],
        },
        "failure_handling": {
            "ok": bool(failure_result and failure_result["ok"]),
            "cases": [mcp_case("failure_tool_call", bool(failure_result and failure_result["ok"]), failure_result or {"error": "No failure MCP tool configured"})],
        },
    }


def _empty_mcp_result(transport: str, error: str, url: str | None = None) -> dict[str, Any]:
    """Return a failure MCP result with all sub-checks failed."""
    result: dict[str, Any] = {
        "ok": False,
        "transport": transport,
        "error": error,
        "health": {"ok": False, "error": error, "body_preview": ""},
        "smoke": {"ok": False, "cases": [mcp_case("safe_tool_call", False, {"error": error})]},
        "integration": {"ok": False, "cases": [mcp_case("integration_tool_call", False, {"error": error})]},
        "failure_handling": {"ok": False, "cases": [mcp_case("failure_tool_call", False, {"error": error})]},
    }
    if url:
        result["url"] = url
    return result


def run_mcp_probe(spec: dict[str, Any]) -> dict[str, Any]:
    """Synchronous entry point — dispatch to stdio or HTTP MCP probe."""
    transport = spec.get("transport", "stdio")
    if transport == "stdio":
        try:
            return anyio.run(run_mcp_stdio_probe_async, spec)
        except Exception as e:
            return _empty_mcp_result("stdio", str(e))
    elif transport in ("sse", "streamable_http"):
        try:
            return anyio.run(run_mcp_http_probe_async, spec)
        except Exception as e:
            return _empty_mcp_result(transport, str(e))
    return _empty_mcp_result(transport, f"Unsupported MCP transport: {transport}")
