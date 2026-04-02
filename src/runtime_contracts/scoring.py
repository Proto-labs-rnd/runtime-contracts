"""Promote-gate scoring and contract profile validation."""

from __future__ import annotations

from typing import Any

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

CRITICAL_GATE_CATEGORIES = {
    "artifacts", "execution", "outputs", "health",
    "packaging", "failure_handling", "logs", "guards",
}


def validate_profile(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate the contract against its declared profile rules."""
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

    missing_required = [f for f in rules["required"] if not field_present(f)]
    missing_recommended = [f for f in rules.get("recommended", []) if not field_present(f)]
    return {
        "ok": not missing_required,
        "profile": profile,
        "description": rules["description"],
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
    }


def index_checks(checks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index check results by name."""
    return {check["name"]: check for check in checks}


def case_group_has_output(details: Any) -> bool:
    """Check whether a case group has any body_preview output."""
    if not isinstance(details, dict):
        return False
    cases = details.get("cases")
    if not isinstance(cases, list) or not cases:
        return False
    return any(case.get("body_preview") for case in cases)


def health_has_output(details: Any) -> bool:
    """Check whether a health check has body_preview output."""
    return isinstance(details, dict) and bool(details.get("body_preview"))


def commands_reproducible(commands: Any) -> tuple[bool, dict[str, Any]]:
    """Verify that the commands block has start/stop/health + smoke or test."""
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
    """Validate the verdict block has claim + reason."""
    if not isinstance(spec, dict):
        return False, {"error": "missing or invalid verdict block"}
    claim = str(spec.get("claim", "")).strip()
    reason = str(spec.get("reason", "")).strip()
    next_action = str(spec.get("next", "")).strip()
    ok = bool(claim and reason)
    return ok, {"claim": claim, "reason": reason, "next": next_action}


def render_gate_reason(status: str, failed: list[str], verdict_meta: dict[str, Any]) -> str:
    """Render a human-readable gate reason string."""
    claim = verdict_meta.get("claim") or "runtime readiness"
    reason = verdict_meta.get("reason") or "Evidence is incomplete."
    if status == "PROMOTE":
        return f"{claim}: all 10 Tachikoma checklist items are evidenced. {reason}".strip()
    if failed:
        failed_text = ", ".join(failed)
        return f"{claim}: HOLD because checklist evidence is incomplete for {failed_text}. {reason}".strip()
    return f"{claim}: HOLD because checklist evidence is incomplete. {reason}".strip()


def score_promote_gate(contract: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Score the contract against the Tachikoma 10-point promote checklist."""
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
    next_action = verdict_meta.get("next") or (
        "Notify Tachikoma" if ready else "Close the missing checklist evidence and rerun the contract"
    )
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
