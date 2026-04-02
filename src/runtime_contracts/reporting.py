"""Report generation — JSON, Markdown, and HTML output."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """Return the current time as an ISO 8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_report(path: Path, content: str) -> None:
    """Write report content to a file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class Report:
    """Accumulates check results and produces a payload dict."""

    def __init__(self, name: str, contract_path: Path) -> None:
        self.name = name
        self.contract_path = str(contract_path)
        self.generated_at = now_iso()
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, details: Any) -> None:
        """Add a check result."""
        self.checks.append({"name": name, "ok": ok, "details": details})

    def payload(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build the final report payload."""
        passed = sum(1 for c in self.checks if c["ok"])
        payload: dict[str, Any] = {
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


def markdown_report(payload: dict[str, Any]) -> str:
    """Generate a Markdown report from the payload."""
    lines = [
        f"# Runtime Contract Report — {payload.get('name', 'unknown')}",
        "",
        f"- Contract: `{payload.get('contract_path', '')}`",
        f"- Generated: `{payload.get('generated_at', '')}`",
        f"- Summary: **{payload.get('summary', {}).get('passed', 0)}/{payload.get('summary', {}).get('total', 0)}** checks passed",
        f"- Promote gate: **{payload.get('promote_gate', {}).get('status', 'HOLD')}** ({payload.get('promote_gate', {}).get('score', 0)}/{payload.get('promote_gate', {}).get('max_score', 10)})",
        "",
        "## Checks",
        "",
        "| Check | OK | Notes |",
        "|-------|----|-------|",
    ]
    for check in payload.get("checks", []):
        details = check.get("details") or {}
        note = details.get("error") or details.get("case") or details.get("path") or details.get("transport") or ""
        note = str(note).replace("|", "\\|")[:120]
        lines.append(f"| {check['name']} | {'✅' if check['ok'] else '❌'} | {note} |")
    lines.extend([
        "",
        "## Promote Gate",
        "",
        payload.get("promote_gate", {}).get("summary_line", ""),
        "",
    ])
    return "\n".join(lines)


def html_report(payload: dict[str, Any]) -> str:
    """Generate an HTML report from the payload."""
    rows = []
    for check in payload.get("checks", []):
        details = check.get("details") or {}
        note = details.get("error") or details.get("case") or details.get("path") or details.get("transport") or ""
        rows.append(
            f"<tr><td>{check['name']}</td><td>{'OK' if check['ok'] else 'FAIL'}</td>"
            f"<td>{str(note)[:200]}</td></tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Runtime Contract Report</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:.5rem;text-align:left}}.ok{{color:#137333}}.fail{{color:#b3261e}}</style>
</head><body>
<h1>Runtime Contract Report — {payload.get('name','unknown')}</h1>
<p><strong>Summary:</strong> {payload.get('summary',{}).get('passed',0)}/{payload.get('summary',{}).get('total',0)} checks passed</p>
<p><strong>Promote gate:</strong> {payload.get('promote_gate',{}).get('status','HOLD')} ({payload.get('promote_gate',{}).get('score',0)}/{payload.get('promote_gate',{}).get('max_score',10)})</p>
<table><thead><tr><th>Check</th><th>Status</th><th>Notes</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<pre>{payload.get('promote_gate',{}).get('summary_line','')}</pre>
</body></html>"""
