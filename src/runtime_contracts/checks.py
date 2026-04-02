"""Core check functions — HTTP, filesystem, deps, guards, DNS, TLS, disk, process."""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def safe_json_loads(text: str) -> Any:
    """Parse JSON string, returning None on failure."""
    try:
        return json.loads(text)
    except Exception:
        return None


def run_http(spec: dict[str, Any]) -> dict[str, Any]:
    """Execute an HTTP request and return status, latency, body, and error info."""
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
    """Check whether *actual* dict contains all key-value pairs from *expected*."""
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        if key not in actual or actual[key] != value:
            return False
    return True


def eval_http_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run a single HTTP test case and evaluate expectations."""
    result = run_http(case)
    ok = not bool(result.get("error"))

    expected_status = case.get("expect_status")
    if expected_status is not None and result["status"] != expected_status:
        ok = False

    for needle in case.get("contains", []):
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


def normalize_case_group(spec: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize smoke/failures/integration spec into (cases_list, meta_dict)."""
    if isinstance(spec, list):
        return spec, {}
    if isinstance(spec, dict):
        cases = spec.get("cases", [])
        meta = {k: v for k, v in spec.items() if k != "cases"}
        return cases, meta
    return [], {"invalid": True}


def eval_http_group(spec: Any) -> dict[str, Any]:
    """Evaluate a group of HTTP test cases."""
    cases, meta = normalize_case_group(spec)
    results = [eval_http_case(case) for case in cases]
    ok = bool(results) and all(case["ok"] for case in results)
    return {"ok": ok, "meta": meta, "cases": results}


def check_paths(paths: list[str]) -> dict[str, Any]:
    """Verify that all listed paths exist."""
    items = []
    ok = True
    for path in paths:
        exists = Path(path).exists()
        items.append({"path": path, "exists": exists})
        ok = ok and exists
    return {"ok": ok, "items": items}


def check_deps(deps: list[str]) -> dict[str, Any]:
    """Verify that all listed CLI dependencies are resolvable on PATH."""
    items = []
    ok = True
    for dep in deps:
        resolved = shutil.which(dep)
        items.append({"dep": dep, "resolved": resolved})
        ok = ok and bool(resolved)
    return {"ok": ok, "items": items}


def check_logs(spec: dict[str, Any]) -> dict[str, Any]:
    """Verify a log file exists and contains expected strings."""
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
    """Verify network guards — transport type or bind address checks."""
    if spec.get("transport") in ("stdio", "sse", "streamable_http"):
        return {
            "ok": True,
            "transport": spec.get("transport", "stdio"),
            "note": spec.get("note", f"{spec.get('transport', 'stdio')} transport — no local port binding required"),
        }
    host = spec.get("host", "127.0.0.1")
    port = int(spec.get("port", 0))
    if not port:
        return {"ok": False, "error": "Missing 'port' for guard check on network transport"}
    cmd = ["ss", "-ltn", f"( sport = :{port} )"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    listen_ok = proc.returncode == 0 and f"{host}:{port}" in stdout
    details: dict[str, Any] = {
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


def check_dns(spec: dict[str, Any]) -> dict[str, Any]:
    """Resolve a hostname and report IP addresses with latency.

    Contract field::

        "dns": {"hostname": "example.com"}
    """
    hostname = spec.get("hostname", "")
    if not hostname:
        return {"ok": False, "error": "Missing 'hostname' in dns spec"}
    started = time.time()
    try:
        addrs = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        latency_ms = round((time.time() - started) * 1000, 2)
        ips = list({addr[4][0] for addr in addrs})
        return {
            "ok": bool(ips),
            "hostname": hostname,
            "addresses": ips,
            "latency_ms": latency_ms,
        }
    except socket.gaierror as e:
        return {"ok": False, "hostname": hostname, "error": str(e)}


def check_tls(spec: dict[str, Any]) -> dict[str, Any]:
    """Connect to host:port and check TLS certificate expiry.

    Contract field::

        "tls": {"host": "example.com", "port": 443, "min_days": 7}
    """
    host = spec.get("host", "")
    port = int(spec.get("port", 443))
    min_days = int(spec.get("min_days", 7))
    if not host:
        return {"ok": False, "error": "Missing 'host' in tls spec"}

    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        if not cert:
            return {"ok": False, "host": host, "error": "No certificate returned"}
        import datetime as _dt
        not_after_str = dict(x[0] for x in cert.get("notAfter", "").split(",")[-1:] or []).get("notAfter", cert.get("notAfter", ""))
        # Parse date like "May  7 12:00:00 2026 GMT"
        expires = _dt.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        remaining = (expires - _dt.datetime.utcnow()).days
        return {
            "ok": remaining >= min_days,
            "host": host,
            "port": port,
            "expires": cert["notAfter"],
            "remaining_days": remaining,
            "min_days": min_days,
            "subject": dict(x[0] for x in cert.get("subject", ())),
        }
    except Exception as e:
        return {"ok": False, "host": host, "error": str(e)}


def check_disk(spec: dict[str, Any]) -> dict[str, Any]:
    """Check disk usage at a given path.

    Contract field::

        "disk": {"path": "/data", "min_free_percent": 10}
    """
    path = spec.get("path", "/")
    min_free = int(spec.get("min_free_percent", 10))
    try:
        usage = shutil.disk_usage(path)
        free_percent = round((usage.free / usage.total) * 100, 1)
        return {
            "ok": free_percent >= min_free,
            "path": path,
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "free_percent": free_percent,
            "min_free_percent": min_free,
        }
    except Exception as e:
        return {"ok": False, "path": path, "error": str(e)}


def check_process(spec: dict[str, Any]) -> dict[str, Any]:
    """Check if a process is running by name or pidfile.

    Contract field::

        "process": {"name": "nginx"}  or  "process": {"pidfile": "/run/nginx.pid"}
    """
    name = spec.get("name")
    pidfile = spec.get("pidfile")
    if not name and not pidfile:
        return {"ok": False, "error": "Provide 'name' or 'pidfile' in process spec"}

    if pidfile:
        try:
            pid = int(Path(pidfile).read_text().strip())
            os.kill(pid, 0)  # signal 0 = existence check
            return {"ok": True, "pidfile": pidfile, "pid": pid}
        except FileNotFoundError:
            return {"ok": False, "pidfile": pidfile, "error": "pidfile not found"}
        except (ValueError, ProcessLookupError):
            return {"ok": False, "pidfile": pidfile, "error": "process from pidfile not running"}
        except PermissionError:
            return {"ok": False, "pidfile": pidfile, "error": "no permission to signal process"}

    # By name — use pgrep
    try:
        result = subprocess.run(
            ["pgrep", "-x", name],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().split() if p.strip().isdigit()]
        return {
            "ok": bool(pids),
            "name": name,
            "pids": pids,
        }
    except Exception as e:
        return {"ok": False, "name": name, "error": str(e)}
