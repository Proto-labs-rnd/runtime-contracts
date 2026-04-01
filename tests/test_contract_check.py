#!/usr/bin/env python3
"""Self-contained unit tests for runtime_contract_check.

Run: python3 -m pytest tests/test_contract_check.py -v
     or: python3 tests/test_contract_check.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the project root is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runtime_contract_check import (
    check_deps,
    check_logs,
    check_paths,
    eval_http_case,
    json_subset,
    load_json,
    score_promote_gate,
    validate_profile,
    verdict_block_ok,
    PROMOTE_CATEGORIES,
    Report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Tiny test HTTP server."""

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length) if length else b""
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = {}

        if "query" not in data:
            body = json.dumps({"error": "missing query"}).encode()
            self.send_response(400)
        else:
            body = json.dumps({"route": "test", "query": data["query"]}).encode()
            self.send_response(200)

        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence request logs


@pytest.fixture(scope="module")
def http_server():
    """Start a local HTTP server for integration tests."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


# ---------------------------------------------------------------------------
# 1. Unit tests — pure functions
# ---------------------------------------------------------------------------

class TestPureFunctions:
    def test_check_paths_all_exist(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        result = check_paths([str(f1), str(f2)])
        assert result["ok"] is True
        assert len(result["items"]) == 2

    def test_check_paths_missing(self, tmp_path):
        result = check_paths([str(tmp_path / "nonexistent.txt")])
        assert result["ok"] is False

    def test_check_deps_known(self):
        result = check_deps(["python3"])
        assert result["ok"] is True

    def test_check_deps_unknown(self):
        result = check_deps(["this_tool_does_not_exist_12345"])
        assert result["ok"] is False

    def test_check_logs_contains(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("request latency=12ms error=nil\n")
        result = check_logs({"path": str(log), "contains": ["latency", "error"]})
        assert result["ok"] is True

    def test_check_logs_missing_content(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("nothing interesting\n")
        result = check_logs({"path": str(log), "contains": ["latency"]})
        assert result["ok"] is False
        assert "latency" in result["missing"]

    def test_json_subset_true(self):
        assert json_subset({"a": 1}, {"a": 1, "b": 2}) is True

    def test_json_subset_false(self):
        assert json_subset({"a": 1}, {"a": 2}) is False

    def test_validate_profile_http_api(self):
        contract = {"contract_type": "http_api", "commands": {}, "artifacts": [], "deps": [], "packaging": [], "health": {}, "smoke": [], "failures": [], "logs": {}, "guards": {}}
        result = validate_profile(contract)
        assert result["ok"] is True
        assert result["profile"] == "http_api"

    def test_validate_profile_unknown(self):
        result = validate_profile({"contract_type": "unknown_type"})
        assert result["ok"] is False

    def test_validate_profile_no_profile(self):
        result = validate_profile({})
        assert result["ok"] is True
        assert result["profile"] is None

    def test_verdict_block_ok_valid(self):
        ok, meta = verdict_block_ok({"claim": "Ready", "reason": "All checks pass"})
        assert ok is True
        assert meta["claim"] == "Ready"

    def test_verdict_block_ok_missing(self):
        ok, meta = verdict_block_ok({})
        assert ok is False


# ---------------------------------------------------------------------------
# 2. HTTP integration tests
# ---------------------------------------------------------------------------

class TestHTTPIntegration:
    def test_health_endpoint(self, http_server):
        result = eval_http_case({
            "name": "health",
            "url": f"http://127.0.0.1:{http_server}/health",
            "expect_status": 200,
            "contains": ["ok"],
        })
        assert result["ok"] is True
        assert result["status"] == 200

    def test_smoke_post(self, http_server):
        result = eval_http_case({
            "name": "smoke",
            "url": f"http://127.0.0.1:{http_server}/route",
            "method": "POST",
            "json": {"query": "hello"},
            "expect_status": 200,
            "contains": ["route"],
        })
        assert result["ok"] is True

    def test_failure_missing_query(self, http_server):
        result = eval_http_case({
            "name": "missing_query",
            "url": f"http://127.0.0.1:{http_server}/route",
            "method": "POST",
            "json": {},
            "expect_status": 400,
        })
        assert result["ok"] is True
        assert result["status"] == 400

    def test_not_found(self, http_server):
        result = eval_http_case({
            "name": "404",
            "url": f"http://127.0.0.1:{http_server}/nonexistent",
            "expect_status": 404,
        })
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# 3. Score / gate tests
# ---------------------------------------------------------------------------

class TestPromoteGate:
    def test_perfect_score(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_text("latency error request\n")
        contract = {
            "commands": {"start": "x", "stop": "x", "health": "x", "smoke": "x"},
            "verdict": {"claim": "Ready", "reason": "All good", "next": "Ship it"},
        }
        checks = [
            {"name": "artifacts", "ok": True, "details": {"ok": True}},
            {"name": "health", "ok": True, "details": {"ok": True, "body_preview": "ok"}},
            {"name": "smoke", "ok": True, "details": {"ok": True, "cases": [{"body_preview": "x"}]}},
            {"name": "failure_handling", "ok": True, "details": {"ok": True}},
            {"name": "packaging", "ok": True, "details": {"ok": True}},
            {"name": "logs", "ok": True, "details": {"ok": True}},
            {"name": "guards", "ok": True, "details": {"ok": True}},
            {"name": "integration", "ok": True, "details": {"ok": True}},
        ]
        payload = {"checks": checks}
        gate = score_promote_gate(contract, payload)
        assert gate["score"] == 10
        assert gate["ready"] is True
        assert gate["status"] == "PROMOTE"

    def test_incomplete_score(self):
        contract = {"commands": {}, "verdict": {}}
        payload = {"checks": []}
        gate = score_promote_gate(contract, payload)
        assert gate["score"] < 10
        assert gate["ready"] is False
        assert gate["status"] == "HOLD"


# ---------------------------------------------------------------------------
# 4. Full CLI run against local server
# ---------------------------------------------------------------------------

class TestCLIRun:
    def test_cli_http_contract(self, http_server, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("latency=1ms error=none request_id=abc\n")
        contract = {
            "name": "test-api",
            "contract_type": "http_api",
            "commands": {
                "start": "python3 app.py",
                "stop": "kill $PID",
                "health": f"curl http://127.0.0.1:{http_server}/health",
                "smoke": f"curl http://127.0.0.1:{http_server}/route",
            },
            "artifacts": [str(tmp_path / "app.py")],
            "deps": ["python3"],
            "packaging": [str(tmp_path / "requirements.txt")],
            "health": {
                "url": f"http://127.0.0.1:{http_server}/health",
                "expect_status": 200,
                "contains": ["ok"],
            },
            "smoke": [
                {
                    "name": "route",
                    "url": f"http://127.0.0.1:{http_server}/route",
                    "method": "POST",
                    "json": {"query": "test"},
                    "expect_status": 200,
                    "contains": ["route"],
                }
            ],
            "failures": [
                {
                    "name": "missing_query",
                    "url": f"http://127.0.0.1:{http_server}/route",
                    "method": "POST",
                    "json": {},
                    "expect_status": 400,
                }
            ],
            "integration": [
                {
                    "name": "health_reachable",
                    "url": f"http://127.0.0.1:{http_server}/health",
                    "expect_status": 200,
                }
            ],
            "logs": {"path": str(log), "contains": ["latency", "error"]},
            "guards": {"transport": "stdio"},
            "verdict": {"claim": "Test API works", "reason": "All checks pass"},
        }
        contract_path = tmp_path / "contract.json"
        contract_path.write_text(json.dumps(contract))

        # Create fake artifacts so paths exist
        (tmp_path / "app.py").write_text("# app")
        (tmp_path / "requirements.txt").write_text("httpx\n")

        result = subprocess.run(
            [sys.executable, str(ROOT / "runtime_contract_check.py"), str(contract_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        report = json.loads(result.stdout)
        assert report["summary"]["ok"] is True
        assert report["promote_gate"]["score"] == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
