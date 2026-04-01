#!/usr/bin/env python3
"""
runtime-contract-check.py — Declarative runtime validator + Tachikoma PROMOTE gate scorer

Checks a small JSON contract against a live service and emits a machine-readable
report. Designed to make hidden runtime dependencies visible before `[PROMOTE]`.

Usage:
  python3 tools/runtime-contract-check.py contract.json
  python3 tools/runtime-contract-check.py contract.json --strict

Notes:
- `--strict` fails when runtime checks fail. A HOLD on the PROMOTE gate does not
  automatically fail the process; it is advisory unless the underlying checks fail.
- Contract profiles are lightweight templates, not a full schema engine.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


PROFILE_RULES: dict[str, dict[str, Any]] = {
    "http_api": {
        "description": "Loopback/local HTTP API with health, smoke, failure and guard checks.",
        "required": ["commands", "artifacts", "deps", "packaging", "health", "smoke", "failures", "logs", "guards"],
        "recommended": ["integration", "verdict"],
    },
    "worker": {
        "description": "Background worker or consumer with logs, failure paths and reproducible commands.",
        "required": ["commands", "artifacts", "deps", "packaging", "logs"],
        "recommended": ["failures", "guards", "integration", "verdict"],
    },
    "mcp_server": {
        "description": "MCP server with reproducible launch, native stdio handshake proof and observable logs.",
        "required": ["commands", "artifacts", "deps", "packaging", "logs", "mcp"],
        "recommended": ["integration", "guards", "verdict", "failures"],
    },
}

PROMOTE_CATEGORIES = [
    ("artifacts", "Artifacts exacts"),
    ("execution", "Exécution reproductible"),
    ("outputs", "Sorties réelles"),
    ("health", "Health + smoke test"),
    ("packaging", "Packaging explicite"),
    ("failure_handling", "Failure handling"),
    ("logs", "Logs exploitables"),
    ("guards", "Gardes-fous"),
    ("integration", "Intégration cible prouvée"),
    ("verdict", "Verdict motivé"),
]

CRITICAL_GATE_CATEGORIES = {"artifacts", "execution", "outputs", "health", "packaging", "failure_handling", "logs", "guards"}
SAFE_TOOL_PREFIXES = ("list", "get", "read", "search", "check")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def mcp_content_preview(result_dump: dict[str, Any]) -> str:
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
    body_preview = payload.get("body_preview", "")
    return {
        "case": name,
        "ok": ok,
        "tool": payload.get("tool"),
        "is_error": payload.get("is_error"),
        "error": payload.get("error"),
        "body_preview": body_preview,
        "result": payload.get("result"),
    }


class Report:
    def __init__(self, name: str, contract_path: Path):
        self.name = name
        self.contract_path = str(contract_path)
        self.generated_at = now_iso()
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, details: Any) -> None:
        self.checks.append({"name": name, "ok": ok, "details": details})

    def payload(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        passed = sum(1 for c in self.checks if c["ok"])
        payload = {
            "name": self.name,
            "contract_path": self.contract_path,
            "generated_at": self.generated_at,
            "summary": {
                "passed": passed,
                "failed": len(self.checks) - passed,
                "total": len(self.checks),
                "ok": passed == len(self.checks),
            },
            "checks": self.checks,
        }
        if extra:
            payload.update(extra)
        return payload


def run_http(spec: dict[str, Any]) -> dict[str, Any]:
    method = spec.get("method", "GET").upper()
    url = spec["url"]
    headers = spec.get("headers", {})
    timeout = int(spec.get("timeout", 10))

    data = None
    if "json" in spec:
        data = json.dumps(spec["json"]).encode()
        headers = {"Content-Type": "application/json", **headers}
    elif "body" in spec:
        data = str(spec["body"]).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.time()
    status = None
    body_text = ""
    error = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body_text = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        body_text = e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        error = str(e.reason)
    except Exception as e:
        error = str(e)
    latency_ms = round((time.time() - started) * 1000, 2)
    body_json = safe_json_loads(body_text)
    return {
        "status": status,
        "latency_ms": latency_ms,
        "body_text": body_text,
        "body_json": body_json,
        "error": error,
    }


def json_subset(expected: dict[str, Any], actual: Any) -> bool:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        if key not in actual or actual[key] != value:
            return False
    return True


def eval_http_case(case: dict[str, Any]) -> dict[str, Any]:
    result = run_http(case)
    ok = not bool(result.get("error"))

    expected_status = case.get("expect_status")
    if expected_status is not None and result["status"] != expected_status:
        ok = False

    contains = case.get("contains", [])
    for needle in contains:
        if needle not in result["body_text"]:
            ok = False

    expect_json = case.get("expect_json")
    if expect_json is not None:
        if not json_subset(expect_json, result["body_json"]):
            ok = False

    result["ok"] = ok
    result["case"] = case.get("name", case.get("url", "http-case"))
    result["body_preview"] = result["body_text"][:400]
    result.pop("body_text", None)
    return result


def check_paths(paths: list[str]) -> dict[str, Any]:
    items = []
    ok = True
    for path in paths:
        exists = Path(path).exists()
        items.append({"path": path, "exists": exists})
        ok = ok and exists
    return {"ok": ok, "items": items}


def check_deps(deps: list[str]) -> dict[str, Any]:
    items = []
    ok = True
    for dep in deps:
        resolved = shutil.which(dep)
        items.append({"dep": dep, "resolved": resolved})
        ok = ok and bool(resolved)
    return {"ok": ok, "items": items}


def check_logs(spec: dict[str, Any]) -> dict[str, Any]:
    path = Path(spec["path"])
    if not path.exists():
        return {"ok": False, "error": f"log file missing: {path}"}
    text = path.read_text(errors="replace")
    contains = spec.get("contains", [])
    missing = [needle for needle in contains if needle not in text]
    return {
        "ok": not missing,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "missing": missing,
        "tail_preview": "\n".join(text.splitlines()[-20:])[:1200],
    }


def check_guard(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("transport") in ("stdio", "sse", "streamable_http"):
        return {
            "ok": True,
            "transport": spec.get("transport", "stdio"),
            "note": spec.get("note", f"{spec.get('transport', 'stdio')} transport — no local port binding required"),
        }
    host = spec.get("host", "127.0.0.1")
    port = int(spec.get("port", 0))
    if not port:
        return {
            "ok": False,
            "error": "Missing 'port' for guard check on network transport",
        }
    cmd = ["ss", "-ltn", f"( sport = :{port} )"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    listen_ok = proc.returncode == 0 and f"{host}:{port}" in stdout
    details = {
        "command": " ".join(cmd),
        "exit_code": proc.returncode,
        "stdout": stdout[:800],
        "stderr": stderr[:400],
        "host": host,
        "port": port,
    }
    if spec.get("health_bind"):
        health = run_http({"url": spec["health_bind"]["url"], "method": "GET", "timeout": 5})
        body = health.get("body_json") or {}
        details["health_bind_seen"] = body.get(spec["health_bind"].get("field", "bind"))
        if details["health_bind_seen"] != spec["health_bind"].get("expected"):
            listen_ok = False
    details["ok"] = listen_ok
    return details


def normalize_case_group(spec: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(spec, list):
        return spec, {}
    if isinstance(spec, dict):
        cases = spec.get("cases", [])
        meta = {k: v for k, v in spec.items() if k != "cases"}
        return cases, meta
    return [], {"invalid": True}


def eval_http_group(spec: Any) -> dict[str, Any]:
    cases, meta = normalize_case_group(spec)
    results = [eval_http_case(case) for case in cases]
    ok = bool(results) and all(case["ok"] for case in results)
    return {"ok": ok, "meta": meta, "cases": results}


def choose_safe_mcp_tool(spec: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any] | None:
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


async def invoke_mcp_tool(session: ClientSession, tool_spec: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    name = tool_spec["name"]
    arguments = tool_spec.get("arguments") or {}
    contains = tool_spec.get("contains") or []
    expect_error = bool(tool_spec.get("expect_error"))
    try:
        result = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=timeout_seconds))
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
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=timeout_seconds)) as session:
                init = dump_model(await session.initialize())
                init_ok = bool(init.get("serverInfo", {}).get("name")) and init.get("protocolVersion") is not None

                tools_result = dump_model(await session.list_tools())
                tools = tools_result.get("tools", [])
                tool_names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
                tools_ok = isinstance(tools, list) and len(tools) > 0 and all(name for name in tool_names)

                safe_tool_spec = choose_safe_mcp_tool(spec, tools)
                safe_result = None
                if safe_tool_spec:
                    safe_result = await invoke_mcp_tool(session, safe_tool_spec, timeout_seconds)

                integration_result = None
                if spec.get("integration_tool"):
                    integration_result = await invoke_mcp_tool(session, spec["integration_tool"], timeout_seconds)

                failure_result = None
                if spec.get("failure_tool"):
                    failure_result = await invoke_mcp_tool(session, spec["failure_tool"], timeout_seconds)

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
                    "transport": "stdio",
                    "initialize": init,
                    "tools_list": tools_result,
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
    finally:
        if log_path:
            errlog.close()


async def run_mcp_http_probe_async(spec: dict[str, Any]) -> dict[str, Any]:
    """MCP probe for HTTP/SSE transport — connects to a remote MCP server."""
    transport = spec.get("transport", "sse")
    url = spec.get("url")
    headers = spec.get("headers", {})
    timeout_seconds = int(spec.get("timeout_seconds", 10))
    read_timeout = int(spec.get("read_timeout_seconds", 300))

    if not url:
        return {
            "ok": False,
            "error": "Missing 'url' for HTTP/SSE transport",
            "health": {"ok": False, "error": "Missing 'url'", "body_preview": ""},
            "smoke": {"ok": False, "cases": []},
            "integration": {"ok": False, "cases": []},
            "failure_handling": {"ok": False, "cases": []},
        }

    client_fn = sse_client if transport == "sse" else streamablehttp_client

    try:
        async with client_fn(
            url=url,
            headers=headers,
            timeout=timeout_seconds,
            sse_read_timeout=read_timeout,
        ) as streams:
            # streamablehttp returns 3-tuple (read, write, session_id_cb)
            if transport == "streamable_http":
                read_stream, write_stream, _ = streams
            else:
                read_stream, write_stream = streams

            async with ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=timeout_seconds)) as session:
                init = dump_model(await session.initialize())
                init_ok = bool(init.get("serverInfo", {}).get("name")) and init.get("protocolVersion") is not None

                tools_result = dump_model(await session.list_tools())
                tools = tools_result.get("tools", [])
                tool_names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
                tools_ok = isinstance(tools, list) and len(tools) > 0 and all(name for name in tool_names)

                safe_tool_spec = choose_safe_mcp_tool(spec, tools)
                safe_result = None
                if safe_tool_spec:
                    safe_result = await invoke_mcp_tool(session, safe_tool_spec, timeout_seconds)

                integration_result = None
                if spec.get("integration_tool"):
                    integration_result = await invoke_mcp_tool(session, spec["integration_tool"], timeout_seconds)

                failure_result = None
                if spec.get("failure_tool"):
                    failure_result = await invoke_mcp_tool(session, spec["failure_tool"], timeout_seconds)

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
                    "url": url,
                    "initialize": init,
                    "tools_list": tools_result,
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
    except Exception as e:
        # Unwrap anyio TaskGroup / ExceptionGroup for clearer diagnostics
        error = str(e)
        if hasattr(e, "exceptions") and hasattr(e, "exceptions"):  # ExceptionGroup
            sub = ", ".join(str(ex) for ex in e.exceptions[:3])
            error = f"{error} — caused by: {sub}"
        elif hasattr(e, "__context__") and e.__context__:
            error = f"{error} — caused by: {e.__context__}"
        return {
            "ok": False,
            "transport": transport,
            "url": url,
            "error": error,
            "health": {"ok": False, "error": error, "body_preview": ""},
            "smoke": {"ok": False, "cases": [{"case": "safe_tool_call", "ok": False, "error": error, "body_preview": ""}]},
            "integration": {"ok": False, "cases": [{"case": "integration_tool_call", "ok": False, "error": error, "body_preview": ""}]},
            "failure_handling": {"ok": False, "cases": [{"case": "failure_tool_call", "ok": False, "error": error, "body_preview": ""}]},
        }


def run_mcp_probe(spec: dict[str, Any]) -> dict[str, Any]:
    transport = spec.get("transport", "stdio")
    if transport == "stdio":
        try:
            return anyio.run(run_mcp_stdio_probe_async, spec)
        except Exception as e:
            error = str(e)
            return {
                "ok": False,
                "error": error,
                "health": {"ok": False, "error": error, "body_preview": ""},
                "smoke": {"ok": False, "cases": [{"case": "safe_tool_call", "ok": False, "error": error, "body_preview": ""}]},
                "integration": {"ok": False, "cases": [{"case": "integration_tool_call", "ok": False, "error": error, "body_preview": ""}]},
                "failure_handling": {"ok": False, "cases": [{"case": "failure_tool_call", "ok": False, "error": error, "body_preview": ""}]},
            }
    elif transport in ("sse", "streamable_http"):
        try:
            return anyio.run(run_mcp_http_probe_async, spec)
        except Exception as e:
            error = str(e)
            return {
                "ok": False,
                "transport": transport,
                "error": error,
                "health": {"ok": False, "error": error, "body_preview": ""},
                "smoke": {"ok": False, "cases": []},
                "integration": {"ok": False, "cases": []},
                "failure_handling": {"ok": False, "cases": []},
            }
    else:
        return {
            "ok": False,
            "error": f"Unsupported MCP transport: {transport}",
            "health": {"ok": False, "error": f"Unsupported MCP transport: {transport}", "body_preview": ""},
            "smoke": {"ok": False, "cases": []},
            "integration": {"ok": False, "cases": []},
            "failure_handling": {"ok": False, "cases": []},
        }


def validate_profile(contract: dict[str, Any]) -> dict[str, Any]:
    profile = contract.get("contract_type") or contract.get("profile")
    if not profile:
        return {
            "ok": True,
            "profile": None,
            "message": "No contract profile declared; runtime checks still executed.",
            "missing_required": [],
            "missing_recommended": [],
        }
    rules = PROFILE_RULES.get(profile)
    if not rules:
        return {
            "ok": False,
            "profile": profile,
            "message": f"Unknown contract profile: {profile}",
            "known_profiles": sorted(PROFILE_RULES.keys()),
            "missing_required": [],
            "missing_recommended": [],
        }

    mcp_spec = contract.get("mcp") if isinstance(contract.get("mcp"), dict) else {}

    def field_present(field: str) -> bool:
        if field in contract:
            return True
        if profile == "mcp_server":
            if field == "integration" and mcp_spec.get("integration_tool"):
                return True
            if field == "failures" and mcp_spec.get("failure_tool"):
                return True
        return False

    missing_required = [field for field in rules["required"] if not field_present(field)]
    missing_recommended = [field for field in rules.get("recommended", []) if not field_present(field)]
    return {
        "ok": not missing_required,
        "profile": profile,
        "description": rules["description"],
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
    }


def index_checks(checks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in checks}


def case_group_has_output(details: Any) -> bool:
    if not isinstance(details, dict):
        return False
    cases = details.get("cases")
    if not isinstance(cases, list) or not cases:
        return False
    for case in cases:
        if case.get("body_preview"):
            return True
    return False


def health_has_output(details: Any) -> bool:
    return isinstance(details, dict) and bool(details.get("body_preview"))


def commands_reproducible(commands: Any) -> tuple[bool, dict[str, Any]]:
    cmd = commands if isinstance(commands, dict) else {}
    required = ["start", "stop", "health"]
    optional_one_of = ["smoke", "test"]
    missing_required = [key for key in required if not cmd.get(key)]
    has_test = any(cmd.get(key) for key in optional_one_of)
    return (not missing_required and has_test), {
        "required": required,
        "optional_one_of": optional_one_of,
        "missing_required": missing_required,
        "has_test_or_smoke": has_test,
        "available": sorted(cmd.keys()),
    }


def verdict_block_ok(spec: Any) -> tuple[bool, dict[str, Any]]:
    if not isinstance(spec, dict):
        return False, {"error": "missing or invalid verdict block"}
    claim = str(spec.get("claim", "")).strip()
    reason = str(spec.get("reason", "")).strip()
    next_action = str(spec.get("next", "")).strip()
    ok = bool(claim and reason)
    return ok, {
        "claim": claim,
        "reason": reason,
        "next": next_action,
    }


def render_gate_reason(status: str, failed: list[str], verdict_meta: dict[str, Any]) -> str:
    claim = verdict_meta.get("claim") or "runtime readiness"
    reason = verdict_meta.get("reason") or "Evidence is incomplete."
    if status == "PROMOTE":
        return f"{claim}: all 10 Tachikoma checklist items are evidenced. {reason}".strip()
    if failed:
        failed_text = ", ".join(failed)
        return f"{claim}: HOLD because checklist evidence is incomplete for {failed_text}. {reason}".strip()
    return f"{claim}: HOLD because checklist evidence is incomplete. {reason}".strip()


def score_promote_gate(contract: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    checks = index_checks(payload.get("checks", []))
    commands_ok, commands_details = commands_reproducible(contract.get("commands"))
    verdict_ok, verdict_meta = verdict_block_ok(contract.get("verdict"))

    artifacts_ok = bool(checks.get("artifacts", {}).get("ok"))
    outputs_ok = health_has_output(checks.get("health", {}).get("details")) and (
        case_group_has_output(checks.get("smoke", {}).get("details"))
        or case_group_has_output(checks.get("failure_handling", {}).get("details"))
        or case_group_has_output(checks.get("integration", {}).get("details"))
    )
    health_ok = bool(checks.get("health", {}).get("ok")) and bool(checks.get("smoke", {}).get("ok"))
    packaging_ok = bool(checks.get("packaging", {}).get("ok"))
    failure_ok = bool(checks.get("failure_handling", {}).get("ok"))
    logs_ok = bool(checks.get("logs", {}).get("ok"))
    guards_ok = bool(checks.get("guards", {}).get("ok")) if "guards" in checks else False
    integration_ok = bool(checks.get("integration", {}).get("ok"))

    score_map = {
        "artifacts": {"ok": artifacts_ok, "evidence": checks.get("artifacts", {}).get("details")},
        "execution": {"ok": commands_ok, "evidence": commands_details},
        "outputs": {
            "ok": outputs_ok,
            "evidence": {
                "health": checks.get("health", {}).get("details"),
                "smoke": checks.get("smoke", {}).get("details"),
                "failure_handling": checks.get("failure_handling", {}).get("details"),
                "integration": checks.get("integration", {}).get("details"),
            },
        },
        "health": {"ok": health_ok, "evidence": {"health": checks.get("health", {}), "smoke": checks.get("smoke", {})}},
        "packaging": {"ok": packaging_ok, "evidence": checks.get("packaging", {}).get("details")},
        "failure_handling": {"ok": failure_ok, "evidence": checks.get("failure_handling", {}).get("details")},
        "logs": {"ok": logs_ok, "evidence": checks.get("logs", {}).get("details")},
        "guards": {"ok": guards_ok, "evidence": checks.get("guards", {}).get("details")},
        "integration": {"ok": integration_ok, "evidence": checks.get("integration", {}).get("details")},
        "verdict": {"ok": verdict_ok, "evidence": verdict_meta},
    }

    checklist = []
    score = 0
    failed_labels: list[str] = []
    failed_keys: list[str] = []
    for key, label in PROMOTE_CATEGORIES:
        ok = bool(score_map[key]["ok"])
        if ok:
            score += 1
        else:
            failed_labels.append(label)
            failed_keys.append(key)
        checklist.append({
            "key": key,
            "label": label,
            "ok": ok,
            "score": 1 if ok else 0,
            "max": 1,
            "evidence": score_map[key]["evidence"],
        })

    critical_failures = [key for key in failed_keys if key in CRITICAL_GATE_CATEGORIES]
    ready = score == len(PROMOTE_CATEGORIES) and not critical_failures
    status = "PROMOTE" if ready else "HOLD"
    reason = render_gate_reason(status, failed_labels, verdict_meta)
    next_action = verdict_meta.get("next") or ("Notify Tachikoma" if ready else "Close the missing checklist evidence and rerun the contract")
    summary_line = f"[{status}] {payload.get('name')} | {reason} | Next: {next_action} | Checklist: {score}/{len(PROMOTE_CATEGORIES)}"

    return {
        "score": score,
        "max_score": len(PROMOTE_CATEGORIES),
        "ready": ready,
        "status": status,
        "critical_failures": critical_failures,
        "failed_categories": failed_keys,
        "checklist": checklist,
        "reason": reason,
        "next_action": next_action,
        "summary_line": summary_line,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a runtime contract JSON file")
    parser.add_argument("contract", help="Path to contract JSON")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on failed runtime checks")
    args = parser.parse_args()

    contract_path = Path(args.contract).resolve()
    contract = load_json(contract_path)
    report = Report(contract.get("name", contract_path.stem), contract_path)

    profile_details = validate_profile(contract)
    report.add("contract_profile", profile_details["ok"], profile_details)

    if contract.get("artifacts"):
        details = check_paths(contract["artifacts"])
        report.add("artifacts", details["ok"], details)

    if contract.get("deps"):
        details = check_deps(contract["deps"])
        report.add("deps", details["ok"], details)

    if contract.get("packaging"):
        details = check_paths(contract["packaging"])
        report.add("packaging", details["ok"], details)

    if contract.get("mcp"):
        details = run_mcp_probe(contract["mcp"])
        report.add("mcp_transport", details.get("ok", False), {
            "transport": details.get("transport", contract.get("mcp", {}).get("transport", "stdio")),
            "error": details.get("error"),
            "initialize": details.get("initialize"),
            "tools_list": details.get("tools_list"),
        })
        report.add("health", details["health"]["ok"], details["health"])
        report.add("smoke", details["smoke"]["ok"], details["smoke"])
        if contract.get("mcp", {}).get("failure_tool"):
            report.add("failure_handling", details["failure_handling"]["ok"], details["failure_handling"])
        if contract.get("mcp", {}).get("integration_tool"):
            report.add("integration", details["integration"]["ok"], details["integration"])

    elif contract.get("health"):
        details = eval_http_case({"name": "health", **contract["health"]})
        report.add("health", details["ok"], details)

    if contract.get("smoke"):
        details = eval_http_group(contract["smoke"])
        report.add("smoke", details["ok"], details)

    if contract.get("failures"):
        details = eval_http_group(contract["failures"])
        report.add("failure_handling", details["ok"], details)

    if contract.get("integration"):
        details = eval_http_group(contract["integration"])
        report.add("integration", details["ok"], details)

    if contract.get("logs"):
        details = check_logs(contract["logs"])
        report.add("logs", details["ok"], details)

    if contract.get("guards"):
        details = check_guard(contract["guards"])
        report.add("guards", details["ok"], details)

    payload = report.payload(extra={
        "contract_type": contract.get("contract_type") or contract.get("profile"),
        "commands": contract.get("commands", {}),
        "notes": contract.get("notes", []),
        "verdict_claim": contract.get("verdict", {}).get("claim") if isinstance(contract.get("verdict"), dict) else None,
    })
    payload["promote_gate"] = score_promote_gate(contract, payload)

    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if args.strict and not payload["summary"]["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
