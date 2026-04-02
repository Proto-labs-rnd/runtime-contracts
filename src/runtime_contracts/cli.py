"""CLI entry point — argument parsing, signal handling, validation orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .checks import (
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
)
from .config import Config, load_config
from .mcp import run_mcp_probe
from .reporting import Report, html_report, markdown_report, write_report
from .scoring import score_promote_gate, validate_profile

logger = logging.getLogger("runtime_contracts")

# Graceful shutdown flag
_shutdown_requested = False


def _handle_signal(signum: int, frame: Any) -> None:
    """Set shutdown flag on SIGTERM/SIGINT."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Received signal %d, requesting graceful shutdown", signum)


def setup_logging(level: str) -> None:
    """Configure structured logging for the runtime_contracts package."""
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file as a dict."""
    return json.loads(path.read_text())


def load_contract(path: Path) -> dict[str, Any]:
    """Load and validate a contract JSON file."""
    try:
        return load_json(path)
    except FileNotFoundError as exc:
        raise ValueError(f"contract file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid contract JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def default_timeout_seconds() -> int:
    """Get default timeout from env var or fallback to 10."""
    raw = os.environ.get("CONTRACT_CHECK_TIMEOUT", "10")
    try:
        return max(1, int(raw))
    except ValueError:
        return 10


def apply_default_timeouts(contract: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    """Deep-clone contract and fill in default timeouts where missing."""
    cloned = json.loads(json.dumps(contract))

    def set_case_timeout(case: dict[str, Any]) -> None:
        if isinstance(case, dict) and "timeout" not in case:
            case["timeout"] = timeout_seconds

    if isinstance(cloned.get("health"), dict):
        set_case_timeout(cloned["health"])

    for key in ("smoke", "failures", "integration"):
        section = cloned.get(key)
        if isinstance(section, list):
            for case in section:
                set_case_timeout(case)
        elif isinstance(section, dict):
            for case in section.get("cases", []):
                set_case_timeout(case)

    if isinstance(cloned.get("mcp"), dict) and "timeout_seconds" not in cloned["mcp"]:
        cloned["mcp"]["timeout_seconds"] = timeout_seconds

    return cloned


def bundled_templates_dir() -> Path:
    """Return the path to bundled contract templates."""
    return Path(__file__).resolve().parent.parent.parent / "templates"


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="contract-check",
        description="Validate runtime contracts and score promotion readiness",
    )
    parser.add_argument("--version", action="version", version=f"contract-check {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress all output except errors",
    )

    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser("validate", help="Validate a contract file")
    validate_parser.add_argument("contract", help="Path to contract JSON")
    validate_parser.add_argument("--strict", action="store_true", help="Exit non-zero on failed runtime checks")
    validate_parser.add_argument("--timeout", type=int, default=None, help="Default timeout in seconds")
    validate_parser.add_argument("--json-out", help="Write JSON report to this file")
    validate_parser.add_argument("--report", choices=["md", "html"], help="Also write a human-readable report")
    validate_parser.add_argument("--report-out", help="Destination path for --report output")

    subparsers.add_parser("templates", help="List bundled contract templates")

    show_parser = subparsers.add_parser("show-template", help="Print a bundled template to stdout")
    show_parser.add_argument("name", help="Template filename, e.g. http_api.template.json")

    subparsers.add_parser("list-checks", help="List all available check types")

    return parser


def run_validation(args: argparse.Namespace, config: Config) -> int:
    """Execute the full validation pipeline on a contract."""
    contract_path = Path(args.contract).resolve()
    contract = apply_default_timeouts(
        load_contract(contract_path),
        args.timeout or config.default_timeout,
    )
    report = Report(contract.get("name", contract_path.stem), contract_path)
    logger.info("Validating contract: %s", contract_path)

    # Profile validation
    profile_details = validate_profile(contract)
    report.add("contract_profile", profile_details["ok"], profile_details)

    if _shutdown_requested:
        logger.warning("Shutdown requested during validation")
        return 2

    # Artifact / dep / packaging checks
    if contract.get("artifacts"):
        details = check_paths(contract["artifacts"])
        report.add("artifacts", details["ok"], details)

    if contract.get("deps"):
        details = check_deps(contract["deps"])
        report.add("deps", details["ok"], details)

    if contract.get("packaging"):
        details = check_paths(contract["packaging"])
        report.add("packaging", details["ok"], details)

    # New probe checks
    if contract.get("dns"):
        details = check_dns(contract["dns"])
        report.add("dns", details["ok"], details)

    if contract.get("tls"):
        details = check_tls(contract["tls"])
        report.add("tls", details["ok"], details)

    if contract.get("disk"):
        details = check_disk(contract["disk"])
        report.add("disk", details["ok"], details)

    if contract.get("process"):
        details = check_process(contract["process"])
        report.add("process", details["ok"], details)

    # MCP or HTTP health/smoke/integration
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

    # Build payload
    payload = report.payload(extra={
        "contract_type": contract.get("contract_type") or contract.get("profile"),
        "commands": contract.get("commands", {}),
        "notes": contract.get("notes", []),
        "verdict_claim": (contract.get("verdict", {}).get("claim")
                          if isinstance(contract.get("verdict"), dict) else None),
    })
    payload["promote_gate"] = score_promote_gate(contract, payload)

    # Write outputs
    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = Path(config.report_output_dir) / out_path
        write_report(out_path, json.dumps(payload, indent=2, ensure_ascii=False))
        logger.info("JSON report written to %s", out_path)

    if args.report:
        report_content = markdown_report(payload) if args.report == "md" else html_report(payload)
        report_path = Path(args.report_out) if args.report_out else contract_path.with_suffix(f".{args.report}")
        if not report_path.is_absolute():
            report_path = Path(config.report_output_dir) / report_path
        write_report(report_path, report_content)
        logger.info("%s report written to %s", args.report.upper(), report_path)

    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if args.strict and not payload["summary"]["ok"]:
        return 1
    return 0


def main() -> int:
    """CLI entry point."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    argv = sys.argv[1:]
    known_commands = {"validate", "templates", "show-template"}
    if argv and not argv[0].startswith("-") and argv[0] not in known_commands:
        argv = ["validate", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)

    # Load config
    config = load_config()

    # Determine log level
    if getattr(args, "verbose", False):
        log_level = "DEBUG"
    elif getattr(args, "quiet", False):
        log_level = "ERROR"
    else:
        log_level = config.log_level
    setup_logging(log_level)

    if args.command == "templates":
        for path in sorted(bundled_templates_dir().glob("*.json")):
            print(path.name)
        return 0

    if args.command == "show-template":
        template_path = bundled_templates_dir() / args.name
        if not template_path.exists():
            print(f"template not found: {args.name}", file=sys.stderr)
            return 2
        print(template_path.read_text())
        return 0

    if args.command == "list-checks":
        checks = [
            ("artifacts", "Verify required files exist"),
            ("deps", "Verify CLI dependencies on PATH"),
            ("packaging", "Verify packaging files exist"),
            ("health", "HTTP health endpoint or MCP handshake"),
            ("smoke", "Happy-path test cases"),
            ("failures", "Expected failure/error cases"),
            ("integration", "Cross-service integration proof"),
            ("logs", "Log file exists with expected markers"),
            ("guards", "Network guards (transport type or bind address)"),
            ("dns", "DNS resolution probe"),
            ("tls", "TLS certificate expiry probe"),
            ("disk", "Disk usage probe"),
            ("process", "Process liveness probe (by name or pidfile)"),
            ("mcp", "MCP transport probe (stdio, sse, streamable_http)"),
            ("verdict", "Human claim + reason + next action"),
        ]
        print(f"{'Check':<20} {'Description'}")
        print("-" * 60)
        for name, desc in checks:
            print(f"{name:<20} {desc}")
        return 0

    if args.command == "validate":
        return run_validation(args, config)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
