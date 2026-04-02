#!/usr/bin/env python3
"""Self-contained unit + integration tests for runtime_contracts.

Run: python3 -m pytest tests/ -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure src/ is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from runtime_contracts.checks import (
    check_deps,
    check_disk,
    check_dns,
    check_guard,
    check_logs,
    check_paths,
    check_process,
    check_tls,
    eval_http_case,
    eval_http_group,
    normalize_case_group,
    json_subset,
    safe_json_loads,
)
from runtime_contracts.cli import (
    apply_default_timeouts,
    bundled_templates_dir,
    build_parser,
    default_timeout_seconds,
    load_contract,
    load_json,
    run_validation,
    main as cli_main,
)
from runtime_contracts.reporting import Report, html_report, markdown_report, write_report
from runtime_contracts.scoring import (
    PROMOTE_CATEGORIES,
    PROFILE_RULES,
    commands_reproducible,
    render_gate_reason,
    score_promote_gate,
    validate_profile,
    verdict_block_ok,
)
from runtime_contracts.config import Config, load_config, DEFAULT_CONFIG_PATH
from runtime_contracts.mcp import (
    dump_model,
    mcp_content_preview,
    mcp_case,
    choose_safe_mcp_tool,
    run_mcp_probe,
    _empty_mcp_result,
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
        pass


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
# 1. Unit tests — pure functions (checks)
# ---------------------------------------------------------------------------

class TestCheckPaths:
    def test_all_exist(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        result = check_paths([str(f1), str(f2)])
        assert result["ok"] is True
        assert len(result["items"]) == 2

    def test_missing(self, tmp_path):
        result = check_paths([str(tmp_path / "nonexistent.txt")])
        assert result["ok"] is False

    def test_empty_list(self):
        result = check_paths([])
        assert result["ok"] is True

    def test_partial_missing(self, tmp_path):
        existing = tmp_path / "exists.txt"
        existing.write_text("x")
        result = check_paths([str(existing), str(tmp_path / "nope.txt")])
        assert result["ok"] is False
        assert len(result["items"]) == 2


class TestCheckDeps:
    def test_known(self):
        result = check_deps(["python3"])
        assert result["ok"] is True

    def test_unknown(self):
        result = check_deps(["this_tool_does_not_exist_12345"])
        assert result["ok"] is False

    def test_mixed(self):
        result = check_deps(["python3", "this_tool_does_not_exist_12345"])
        assert result["ok"] is False
        assert len(result["items"]) == 2

    def test_empty(self):
        result = check_deps([])
        assert result["ok"] is True


class TestCheckLogs:
    def test_contains(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("request latency=12ms error=nil\n")
        result = check_logs({"path": str(log), "contains": ["latency", "error"]})
        assert result["ok"] is True

    def test_missing_content(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("nothing interesting\n")
        result = check_logs({"path": str(log), "contains": ["latency"]})
        assert result["ok"] is False
        assert "latency" in result["missing"]

    def test_file_missing(self, tmp_path):
        result = check_logs({"path": str(tmp_path / "no.log"), "contains": []})
        assert result["ok"] is False

    def test_empty_contains(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("whatever\n")
        result = check_logs({"path": str(log), "contains": []})
        assert result["ok"] is True

    def test_tail_preview(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("\n".join(f"line {i}" for i in range(30)))
        result = check_logs({"path": str(log), "contains": []})
        assert "line 29" in result["tail_preview"]


class TestJsonHelpers:
    def test_subset_true(self):
        assert json_subset({"a": 1}, {"a": 1, "b": 2}) is True

    def test_subset_false(self):
        assert json_subset({"a": 1}, {"a": 2}) is False

    def test_subset_non_dict(self):
        assert json_subset("x", "y") is False

    def test_safe_loads_valid(self):
        assert safe_json_loads('{"x": 1}') == {"x": 1}

    def test_safe_loads_invalid(self):
        assert safe_json_loads("not json") is None


class TestEvalHttpCase:
    def test_healthy(self, http_server):
        result = eval_http_case({
            "name": "health",
            "url": f"http://127.0.0.1:{http_server}/health",
            "expect_status": 200,
            "contains": ["ok"],
        })
        assert result["ok"] is True
        assert result["status"] == 200

    def test_post_smoke(self, http_server):
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

    def test_connection_refused(self):
        result = eval_http_case({
            "name": "offline",
            "url": "http://127.0.0.1:1/impossible",
            "timeout": 1,
        })
        assert result["ok"] is False
        assert result.get("error")

    def test_expect_json_match(self, http_server):
        result = eval_http_case({
            "name": "json_check",
            "url": f"http://127.0.0.1:{http_server}/health",
            "expect_json": {"status": "ok"},
        })
        assert result["ok"] is True

    def test_expect_json_mismatch(self, http_server):
        result = eval_http_case({
            "name": "json_fail",
            "url": f"http://127.0.0.1:{http_server}/health",
            "expect_json": {"status": "fail"},
        })
        assert result["ok"] is False

    def test_body_not_contains(self, http_server):
        result = eval_http_case({
            "name": "missing_text",
            "url": f"http://127.0.0.1:{http_server}/health",
            "contains": ["this_text_is_not_there"],
        })
        assert result["ok"] is False

    def test_latency_present(self, http_server):
        result = eval_http_case({
            "name": "lat",
            "url": f"http://127.0.0.1:{http_server}/health",
        })
        assert "latency_ms" in result

    def test_post_with_body(self, http_server):
        result = eval_http_case({
            "name": "body_test",
            "url": f"http://127.0.0.1:{http_server}/route",
            "method": "POST",
            "body": "raw text",
            "expect_status": 200,
        })
        # Should not crash even with raw body
        assert "ok" in result


class TestNormalizeCaseGroup:
    def test_list(self):
        cases, meta = normalize_case_group([{"url": "http://x"}])
        assert len(cases) == 1
        assert meta == {}

    def test_dict_with_cases(self):
        cases, meta = normalize_case_group({"cases": [{"url": "http://x"}], "timeout": 5})
        assert len(cases) == 1
        assert meta["timeout"] == 5

    def test_invalid(self):
        cases, meta = normalize_case_group("bad")
        assert cases == []
        assert meta.get("invalid") is True

    def test_empty_dict(self):
        cases, meta = normalize_case_group({})
        assert cases == []
        assert meta == {}


class TestEvalHttpGroup:
    def test_list_group(self, http_server):
        result = eval_http_group([
            {"name": "g1", "url": f"http://127.0.0.1:{http_server}/health", "expect_status": 200},
        ])
        assert result["ok"] is True

    def test_dict_group(self, http_server):
        result = eval_http_group({
            "cases": [{"name": "g2", "url": f"http://127.0.0.1:{http_server}/health", "expect_status": 200}],
        })
        assert result["ok"] is True

    def test_empty_group(self):
        result = eval_http_group([])
        assert result["ok"] is False

    def test_failing_case(self, http_server):
        result = eval_http_group([
            {"name": "fail", "url": f"http://127.0.0.1:{http_server}/health", "expect_status": 500},
        ])
        assert result["ok"] is False


class TestCheckGuard:
    def test_stdio_transport(self):
        result = check_guard({"transport": "stdio"})
        assert result["ok"] is True

    def test_sse_transport(self):
        result = check_guard({"transport": "sse"})
        assert result["ok"] is True

    def test_streamable_http_transport(self):
        result = check_guard({"transport": "streamable_http"})
        assert result["ok"] is True

    def test_missing_port(self):
        result = check_guard({"host": "0.0.0.0"})
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 2. New probe checks
# ---------------------------------------------------------------------------

class TestNewProbes:
    def test_dns_localhost(self):
        result = check_dns({"hostname": "localhost"})
        assert result["ok"] is True
        assert "127.0.0.1" in result["addresses"]

    def test_dns_missing_hostname(self):
        result = check_dns({})
        assert result["ok"] is False
        assert "hostname" in result["error"]

    def test_dns_unresolvable(self):
        result = check_dns({"hostname": "this.domain.does.not.exist.invalid"})
        assert result["ok"] is False

    def test_dns_has_latency(self):
        result = check_dns({"hostname": "localhost"})
        assert "latency_ms" in result

    def test_disk_root(self):
        result = check_disk({"path": "/", "min_free_percent": 0})
        assert result["ok"] is True
        assert "free_gb" in result

    def test_disk_missing_path(self):
        result = check_disk({"path": "/nonexistent/path/xyz"})
        assert result["ok"] is False

    def test_disk_low_threshold(self):
        result = check_disk({"path": "/", "min_free_percent": 99.9})
        # Likely fails unless disk is nearly empty
        assert "free_percent" in result

    def test_process_by_name_systemd(self):
        result = check_process({"name": "systemd"})
        assert "ok" in result
        assert "name" in result

    def test_process_missing_spec(self):
        result = check_process({})
        assert result["ok"] is False

    def test_process_pidfile_not_found(self, tmp_path):
        result = check_process({"pidfile": str(tmp_path / "no.pid")})
        assert result["ok"] is False

    def test_process_pidfile_stale(self, tmp_path):
        pidfile = tmp_path / "stale.pid"
        pidfile.write_text("999999999")
        result = check_process({"pidfile": str(pidfile)})
        assert result["ok"] is False

    def test_tls_google(self):
        result = check_tls({"host": "google.com", "port": 443, "min_days": 0})
        assert "remaining_days" in result or "error" in result

    def test_tls_missing_host(self):
        result = check_tls({})
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 3. Score / gate tests
# ---------------------------------------------------------------------------

class TestPromoteGate:
    def test_perfect_score(self):
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

    def test_partial_commands(self):
        ok, details = commands_reproducible({"start": "x", "stop": "x", "health": "x"})
        assert ok is False
        assert "smoke" in details["optional_one_of"]

    def test_full_commands(self):
        ok, _ = commands_reproducible({"start": "x", "stop": "x", "health": "x", "test": "x"})
        assert ok is True

    def test_render_gate_reason_promote(self):
        reason = render_gate_reason("PROMOTE", [], {"claim": "Ready", "reason": "All pass"})
        assert "PROMOTE" not in reason  # render doesn't include status word, just the claim
        assert "Ready" in reason

    def test_render_gate_reason_hold_with_failures(self):
        reason = render_gate_reason("HOLD", ["Artifacts", "Logs"], {"claim": "Test", "reason": "Incomplete"})
        assert "Artifacts" in reason

    def test_render_gate_reason_hold_no_failures(self):
        reason = render_gate_reason("HOLD", [], {"claim": "X", "reason": "Y"})
        assert "incomplete" in reason.lower()


class TestProfileValidation:
    def test_http_api_valid(self):
        contract = {"contract_type": "http_api", "commands": {}, "artifacts": [], "deps": [], "packaging": [], "health": {}, "smoke": [], "failures": [], "logs": {}, "guards": {}}
        result = validate_profile(contract)
        assert result["ok"] is True
        assert result["profile"] == "http_api"

    def test_unknown_profile(self):
        result = validate_profile({"contract_type": "unknown_type"})
        assert result["ok"] is False
        assert "known_profiles" in result

    def test_no_profile(self):
        result = validate_profile({})
        assert result["ok"] is True
        assert result["profile"] is None

    def test_worker_missing_required(self):
        result = validate_profile({"contract_type": "worker", "commands": {}})
        assert result["ok"] is False
        assert len(result["missing_required"]) > 0

    def test_mcp_server_with_failure_tool(self):
        result = validate_profile({"contract_type": "mcp_server", "commands": {}, "artifacts": [], "deps": [], "packaging": [], "logs": {}, "mcp": {}, "failures": []})
        assert result["ok"] is True

    def test_mcp_server_failure_via_mcp_spec(self):
        result = validate_profile({"contract_type": "mcp_server", "commands": {}, "artifacts": [], "deps": [], "packaging": [], "logs": {}, "mcp": {"failure_tool": {"name": "x"}}})
        assert result["ok"] is True
        assert "failures" not in result.get("missing_required", [])

    def test_profile_rules_completeness(self):
        """Ensure all profiles have description, required, recommended."""
        for name, rules in PROFILE_RULES.items():
            assert "description" in rules
            assert "required" in rules
            assert "recommended" in rules


class TestVerdictBlock:
    def test_valid(self):
        ok, meta = verdict_block_ok({"claim": "Ready", "reason": "All checks pass"})
        assert ok is True
        assert meta["claim"] == "Ready"

    def test_missing(self):
        ok, meta = verdict_block_ok({})
        assert ok is False

    def test_non_dict(self):
        ok, meta = verdict_block_ok("bad")
        assert ok is False

    def test_with_next(self):
        ok, meta = verdict_block_ok({"claim": "C", "reason": "R", "next": "Ship"})
        assert ok is True
        assert meta["next"] == "Ship"


# ---------------------------------------------------------------------------
# 4. Reporting tests
# ---------------------------------------------------------------------------

class TestReporting:
    def test_report_add_and_payload(self):
        r = Report("test", Path("/tmp/test.json"))
        r.add("health", True, {"status": 200})
        r.add("smoke", False, {"error": "timeout"})
        payload = r.payload()
        assert payload["summary"]["passed"] == 1
        assert payload["summary"]["failed"] == 1
        assert payload["summary"]["total"] == 2

    def test_report_with_extra(self):
        r = Report("test", Path("/tmp/x.json"))
        r.add("x", True, {})
        payload = r.payload(extra={"contract_type": "http_api"})
        assert payload["contract_type"] == "http_api"

    def test_markdown_report(self):
        payload = {
            "name": "demo",
            "contract_path": "/tmp/demo.json",
            "generated_at": "2026-04-01T10:00:00+0000",
            "summary": {"passed": 2, "total": 3},
            "checks": [{"name": "health", "ok": True, "details": {"path": "/tmp/x"}}],
            "promote_gate": {"status": "HOLD", "score": 6, "max_score": 10, "summary_line": "[HOLD] demo"},
        }
        md = markdown_report(payload)
        assert "# Runtime Contract Report" in md
        assert "health" in md

    def test_html_report(self):
        payload = {
            "name": "demo",
            "summary": {"passed": 1, "total": 1},
            "checks": [{"name": "x", "ok": True, "details": {}}],
            "promote_gate": {"status": "PROMOTE", "score": 10, "max_score": 10, "summary_line": "[PROMOTE]"},
        }
        html = html_report(payload)
        assert "<html>" in html
        assert "PROMOTE" in html

    def test_write_report(self, tmp_path):
        out = tmp_path / "sub" / "report.md"
        write_report(out, "hello")
        assert out.read_text() == "hello"


# ---------------------------------------------------------------------------
# 5. Config tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_config(self):
        config = Config()
        assert config.default_timeout == 10
        assert config.log_level == "WARNING"

    def test_from_dict(self):
        config = Config.from_dict({"default_timeout": 30, "log_level": "debug"})
        assert config.default_timeout == 30
        assert config.log_level == "DEBUG"

    def test_from_dict_ignores_unknown(self):
        config = Config.from_dict({"unknown_key": "x", "default_timeout": 5})
        assert config.default_timeout == 5

    def test_env_override_timeout(self, monkeypatch):
        config = Config()
        monkeypatch.setenv("CONTRACT_CHECK_TIMEOUT", "42")
        config.apply_env_overrides()
        assert config.default_timeout == 42

    def test_env_override_report_dir(self, monkeypatch):
        config = Config()
        monkeypatch.setenv("CONTRACT_CHECK_REPORT_DIR", "/tmp/reports")
        config.apply_env_overrides()
        assert config.report_output_dir == "/tmp/reports"

    def test_env_override_log_level(self, monkeypatch):
        config = Config()
        monkeypatch.setenv("CONTRACT_CHECK_LOG_LEVEL", "debug")
        config.apply_env_overrides()
        assert config.log_level == "DEBUG"

    def test_env_override_invalid_timeout(self, monkeypatch):
        config = Config()
        monkeypatch.setenv("CONTRACT_CHECK_TIMEOUT", "not_a_number")
        config.apply_env_overrides()
        assert config.default_timeout == 10  # unchanged

    def test_validate_invalid_log_level(self):
        config = Config(log_level="INVALID")
        warnings = config.validate()
        assert len(warnings) == 1
        assert config.log_level == "WARNING"

    def test_validate_timeout_too_low(self):
        config = Config(default_timeout=0)
        warnings = config.validate()
        assert len(warnings) == 1
        assert config.default_timeout == 1

    def test_validate_ok(self):
        config = Config(default_timeout=5, log_level="INFO")
        assert config.validate() == []

    def test_load_config_no_file(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.default_timeout == 10

    def test_load_config_from_yaml(self, tmp_path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("default_timeout: 25\nlog_level: debug\n")
        config = load_config(yaml_file)
        assert config.default_timeout == 25
        assert config.log_level == "DEBUG"

    def test_load_config_invalid_yaml(self, tmp_path):
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text("{{invalid yaml")
        config = load_config(yaml_file)
        # Should fallback to defaults
        assert config.default_timeout == 10


# ---------------------------------------------------------------------------
# 6. CLI tests
# ---------------------------------------------------------------------------

class TestCLIParsing:
    def test_build_parser(self):
        parser = build_parser()
        assert parser.prog == "contract-check"

    def test_default_timeout_env(self, monkeypatch):
        monkeypatch.setenv("CONTRACT_CHECK_TIMEOUT", "20")
        assert default_timeout_seconds() == 20

    def test_default_timeout_invalid_env(self, monkeypatch):
        monkeypatch.setenv("CONTRACT_CHECK_TIMEOUT", "abc")
        assert default_timeout_seconds() == 10

    def test_default_timeout_no_env(self, monkeypatch):
        monkeypatch.delenv("CONTRACT_CHECK_TIMEOUT", raising=False)
        assert default_timeout_seconds() == 10

    def test_apply_default_timeouts_sets_missing(self):
        contract = {"health": {"url": "http://x"}, "smoke": [{"url": "http://y"}], "mcp": {"transport": "stdio"}}
        updated = apply_default_timeouts(contract, 7)
        assert updated["health"]["timeout"] == 7
        assert updated["smoke"][0]["timeout"] == 7
        assert updated["mcp"]["timeout_seconds"] == 7

    def test_apply_default_timeouts_preserves_existing(self):
        contract = {"health": {"url": "http://x", "timeout": 30}}
        updated = apply_default_timeouts(contract, 7)
        assert updated["health"]["timeout"] == 30

    def test_apply_default_timeouts_dict_smoke(self):
        contract = {"smoke": {"cases": [{"url": "http://x"}]}}
        updated = apply_default_timeouts(contract, 5)
        assert updated["smoke"]["cases"][0]["timeout"] == 5

    def test_load_contract_valid(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text('{"name": "test"}')
        assert load_contract(path)["name"] == "test"

    def test_load_contract_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            load_contract(tmp_path / "no.json")

    def test_load_contract_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('{"name": }')
        with pytest.raises(ValueError, match="line"):
            load_contract(path)

    def test_load_json(self, tmp_path):
        path = tmp_path / "test.json"
        path.write_text('{"key": "value"}')
        assert load_json(path) == {"key": "value"}

    def test_bundled_templates_dir_exists(self):
        d = bundled_templates_dir()
        assert d.exists()
        assert len(list(d.glob("*.json"))) >= 3


class TestCLISubcommands:
    def test_templates(self):
        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", "templates"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 0
        assert "http_api.template.json" in result.stdout

    def test_show_template(self):
        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", "show-template", "http_api.template.json"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 0
        assert "http_api" in result.stdout

    def test_show_template_missing(self):
        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", "show-template", "nonexistent.json"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 2

    def test_validate_http_contract(self, http_server, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("latency=1ms error=none request_id=abc\n")
        app_py = tmp_path / "app.py"
        app_py.write_text("# app")
        req = tmp_path / "requirements.txt"
        req.write_text("httpx\n")
        contract = {
            "name": "test-api",
            "contract_type": "http_api",
            "commands": {"start": "x", "stop": "x", "health": "x", "smoke": "x"},
            "artifacts": [str(app_py)],
            "deps": ["python3"],
            "packaging": [str(req)],
            "health": {"url": f"http://127.0.0.1:{http_server}/health", "expect_status": 200, "contains": ["ok"]},
            "smoke": [{"name": "route", "url": f"http://127.0.0.1:{http_server}/route", "method": "POST", "json": {"query": "test"}, "expect_status": 200, "contains": ["route"]}],
            "failures": [{"name": "missing_query", "url": f"http://127.0.0.1:{http_server}/route", "method": "POST", "json": {}, "expect_status": 400}],
            "integration": [{"name": "health_reachable", "url": f"http://127.0.0.1:{http_server}/health", "expect_status": 200}],
            "logs": {"path": str(log), "contains": ["latency", "error"]},
            "guards": {"transport": "stdio"},
            "verdict": {"claim": "Test API works", "reason": "All checks pass"},
        }
        contract_path = tmp_path / "contract.json"
        contract_path.write_text(json.dumps(contract))

        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", str(contract_path)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["summary"]["ok"] is True
        assert report["promote_gate"]["score"] == 10

    def test_validate_with_reports(self, http_server, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("latency=1ms error=none\n")
        app_py = tmp_path / "app.py"
        app_py.write_text("# app")
        req = tmp_path / "requirements.txt"
        req.write_text("httpx\n")
        contract = {
            "name": "test-api",
            "commands": {"start": "x", "stop": "x", "health": "x", "smoke": "x"},
            "artifacts": [str(app_py)],
            "deps": ["python3"],
            "packaging": [str(req)],
            "health": {"url": f"http://127.0.0.1:{http_server}/health", "expect_status": 200, "contains": ["ok"]},
            "smoke": [{"name": "route", "url": f"http://127.0.0.1:{http_server}/route", "method": "POST", "json": {"query": "test"}, "expect_status": 200}],
            "failures": [{"name": "missing_query", "url": f"http://127.0.0.1:{http_server}/route", "method": "POST", "json": {}, "expect_status": 400}],
            "integration": [{"name": "health_reachable", "url": f"http://127.0.0.1:{http_server}/health", "expect_status": 200}],
            "logs": {"path": str(log), "contains": ["latency", "error"]},
            "guards": {"transport": "stdio"},
            "verdict": {"claim": "Works", "reason": "All pass"},
        }
        contract_path = tmp_path / "contract.json"
        contract_path.write_text(json.dumps(contract))
        json_out = tmp_path / "report.json"
        md_out = tmp_path / "report.md"

        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", "validate", str(contract_path),
             "--json-out", str(json_out), "--report", "md", "--report-out", str(md_out)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(json_out.read_text())["name"] == "test-api"
        assert "# Runtime Contract Report" in md_out.read_text()

    def test_validate_strict_mode_fails(self, http_server, tmp_path):
        contract = {
            "name": "fail-test",
            "artifacts": [str(tmp_path / "nonexistent.txt")],
            "deps": ["nonexistent_tool_xyz"],
        }
        contract_path = tmp_path / "contract.json"
        contract_path.write_text(json.dumps(contract))

        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", "validate", str(contract_path), "--strict"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 1

    def test_no_command_shows_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# 7. MCP unit tests (mocked)
# ---------------------------------------------------------------------------

class TestMCPHelpers:
    def test_dump_model_plain(self):
        assert dump_model("hello") == "hello"

    def test_dump_model_with_model_dump(self):
        class FakeModel:
            def model_dump(self, **kw):
                return {"x": 1}
        assert dump_model(FakeModel()) == {"x": 1}

    def test_mcp_content_preview_text(self):
        result = {"content": [{"type": "text", "text": "hello world"}]}
        assert mcp_content_preview(result) == "hello world"

    def test_mcp_content_preview_structured(self):
        result = {"structuredContent": {"key": "val"}}
        assert "key" in mcp_content_preview(result)

    def test_mcp_content_preview_empty(self):
        assert mcp_content_preview({}) == ""

    def test_mcp_case(self):
        c = mcp_case("test", True, {"tool": "list", "body_preview": "ok"})
        assert c["case"] == "test"
        assert c["ok"] is True

    def test_choose_safe_mcp_tool_explicit(self):
        spec = {"safe_tool": {"name": "my_tool", "arguments": {"x": 1}}}
        result = choose_safe_mcp_tool(spec, [])
        assert result["name"] == "my_tool"
        assert result["arguments"] == {"x": 1}

    def test_choose_safe_mcp_tool_auto(self):
        tools = [
            {"name": "list_items", "inputSchema": {"required": []}},
            {"name": "delete_item", "inputSchema": {"required": []}},
        ]
        result = choose_safe_mcp_tool({}, tools)
        assert result["name"] == "list_items"

    def test_choose_safe_mcp_tool_none(self):
        tools = [{"name": "create_item", "inputSchema": {"required": ["name"]}}]
        result = choose_safe_mcp_tool({}, tools)
        assert result is None

    def test_run_mcp_probe_unsupported_transport(self):
        result = run_mcp_probe({"transport": "websocket"})
        assert result["ok"] is False
        assert "Unsupported" in result["error"]

    def test_empty_mcp_result(self):
        result = _empty_mcp_result("stdio", "test error")
        assert result["ok"] is False
        assert result["health"]["ok"] is False

    def test_empty_mcp_result_with_url(self):
        result = _empty_mcp_result("sse", "err", url="http://x")
        assert result["url"] == "http://x"


# ---------------------------------------------------------------------------
# 8. Edge case / integration
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_contract_with_no_checks(self, tmp_path):
        contract = {"name": "empty"}
        path = tmp_path / "c.json"
        path.write_text(json.dumps(contract))
        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", "validate", str(path)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 0
        report = json.loads(result.stdout)
        assert report["summary"]["total"] == 1  # contract_profile always checked

    def test_contract_with_probes(self, tmp_path):
        contract = {
            "name": "probe-test",
            "dns": {"hostname": "localhost"},
            "disk": {"path": "/", "min_free_percent": 0},
            "process": {"name": "systemd"},
        }
        path = tmp_path / "c.json"
        path.write_text(json.dumps(contract))
        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", "validate", str(path)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 0
        report = json.loads(result.stdout)
        assert report["summary"]["passed"] >= 2

    def test_version_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "runtime_contracts.cli", "--version"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert "contract-check" in result.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
