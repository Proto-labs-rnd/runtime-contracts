#!/usr/bin/env python3
"""
Mesh Status MCP Server

Exposes homelab mesh status via MCP tools:
- Agent health and capabilities
- Docker container status
- Router V3 stats and routing
- Service overview

Usage:
  python3 mesh_status_mcp.py
  # Or add to OpenClaw MCP config:
  # {"mcpServers":{"mesh-status":{"command":"python3","args":["tools/mesh_status_mcp.py"]}}}
"""

import subprocess
import json
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mesh-status")

ROUTER_URL = "http://127.0.0.1:8905"

# ── Agent Registry ──────────────────────────────────────────
AGENTS = {
    "tachikoma": {"role": "Orchestrator", "emoji": "🤖", "workspace": "workspace"},
    "orion": {"role": "SRE/Ops", "emoji": "🔧", "workspace": "workspace-sre"},
    "proto": {"role": "R&D/Labs", "emoji": "🧪", "workspace": "workspace-labs"},
    "specter": {"role": "Research", "emoji": "📚", "workspace": "workspace-research"},
    "aegis": {"role": "Security", "emoji": "🛡️", "workspace": "workspace-security"},
}


# ── Tools ────────────────────────────────────────────────────

@mcp.tool()
def get_mesh_overview() -> str:
    """Get a complete overview of the homelab mesh: agents, services, health."""
    lines = ["🏠 **Homelab Mesh Overview**\n"]

    # Agents
    lines.append("**Agents:**")
    for name, info in AGENTS.items():
        lines.append(f"  {info['emoji']} **{name.title()}** — {info['role']}")

    # Docker
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"],
            capture_output=True, text=True, timeout=10
        )
        containers = [l for l in result.stdout.strip().split("\n") if l]
        lines.append(f"\n**Docker containers:** {len(containers)} running")
        for c in containers[:10]:
            parts = c.split("|")
            name, status = parts[0], parts[1]
            icon = "✅" if "Up" in status else "⚠️"
            lines.append(f"  {icon} {name} — {status}")
        if len(containers) > 10:
            lines.append(f"  ... and {len(containers)-10} more")
    except Exception:
        lines.append("\n**Docker:** Unable to query")

    # Router
    try:
        r = httpx.get(f"{ROUTER_URL}/health", timeout=5)
        if r.status_code == 200:
            data = r.json()
            lines.append(f"\n**Router V3:** ✅ healthy, {data.get('total_queries', 0)} queries served")
        else:
            lines.append(f"\n**Router V3:** ⚠️ status {r.status_code}")
    except Exception:
        lines.append("\n**Router V3:** ❌ unreachable")

    return "\n".join(lines)


@mcp.tool()
def get_agent_info(agent_name: str) -> str:
    """Get detailed info about a specific agent: role, capabilities, status.
    
    Args:
        agent_name: One of: tachikoma, orion, proto, specter, aegis
    """
    agent_name = agent_name.lower().strip()
    if agent_name not in AGENTS:
        return f"Unknown agent '{agent_name}'. Available: {', '.join(AGENTS.keys())}"

    info = AGENTS[agent_name]
    lines = [f"{info['emoji']} **{agent_name.title()}** — {info['role']}"]

    # Check workspace exists
    import os
    ws = f"/mnt/shared-storage/openclaw/{info['workspace']}"
    if os.path.isdir(ws):
        files = os.listdir(ws)
        lines.append(f"  Workspace: `{ws}` ({len(files)} items)")
        # Check for EXPERIMENTS.md
        if "EXPERIMENTS.md" in files:
            lines.append("  Has experiments backlog: ✅")
        if "MEMORY.md" in files:
            lines.append("  Has long-term memory: ✅")
    else:
        lines.append(f"  Workspace: not found at {ws}")

    # Check Docker containers for this agent
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={agent_name}", "--format", "{{.Names}} {{.Status}}"],
            capture_output=True, text=True, timeout=10
        )
        containers = [l for l in result.stdout.strip().split("\n") if l]
        if containers:
            lines.append(f"  Docker containers: {len(containers)}")
            for c in containers:
                lines.append(f"    {c}")
        else:
            lines.append("  Docker containers: none")
    except Exception:
        pass

    return "\n".join(lines)


@mcp.tool()
def list_services() -> str:
    """List all running Docker services with their status and ports."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Image}}|{{.Ports}}"],
            capture_output=True, text=True, timeout=10
        )
        containers = [l for l in result.stdout.strip().split("\n") if l]
        if not containers:
            return "No containers running."

        lines = [f"**Running services ({len(containers)}):**\n"]
        for c in containers:
            parts = c.split("|")
            name = parts[0]
            status = parts[1] if len(parts) > 1 else "?"
            image = parts[2] if len(parts) > 2 else "?"
            ports = parts[3] if len(parts) > 3 else ""
            icon = "✅" if "Up" in status else "⚠️"
            port_str = f" → {ports}" if ports else ""
            lines.append(f"{icon} **{name}** ({image}){port_str}")
            lines.append(f"   {status}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error querying Docker: {e}"


@mcp.tool()
def route_message(query: str) -> str:
    """Route a message to the best agent using the Router V3.
    
    Args:
        query: The message/query to route
    """
    try:
        r = httpx.post(
            f"{ROUTER_URL}/route",
            json={"query": query},
            timeout=10
        )
        if r.status_code != 200:
            return f"Router error: {r.status_code}"

        data = r.json()
        agent_info = AGENTS.get(data["agent"], {"emoji": "❓", "role": "unknown"})
        return (
            f"**Route:** {data['route']}\n"
            f"**Agent:** {agent_info['emoji']} {data['agent'].title()} ({agent_info['role']})\n"
            f"**Confidence:** {data['confidence']:.1%}\n"
            f"**Method:** {data['method']}\n"
            f"**Latency:** {data['latency_ms']:.1f}ms"
        )
    except httpx.ConnectError:
        return "❌ Router V3 is unreachable. Is the container running?"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_router_stats() -> str:
    """Get Router V3 statistics: query count, route distribution, method distribution."""
    try:
        r = httpx.get(f"{ROUTER_URL}/stats", timeout=5)
        if r.status_code != 200:
            return f"Router error: {r.status_code}"
        data = r.json()

        lines = [f"**Router V3 Stats** (queries: {data['total_queries']})\n"]
        lines.append("**Route distribution:**")
        for route, count in sorted(data.get("route_distribution", {}).items(), key=lambda x: -x[1]):
            agent = data.get("route_agent_map", {}).get(route, "?")
            lines.append(f"  {route} → {agent}: {count}")

        lines.append("\n**Method distribution:**")
        for method, count in sorted(data.get("method_distribution", {}).items(), key=lambda x: -x[1]):
            lines.append(f"  {method}: {count}")

        return "\n".join(lines)
    except httpx.ConnectError:
        return "❌ Router V3 unreachable"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def check_container_health(container_name: str) -> str:
    """Check the health and recent logs of a specific Docker container.
    
    Args:
        container_name: Name of the Docker container to check
    """
    try:
        # Status
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.State.Status}}|{{.State.Health.Status}}|{{.State.StartedAt}}|{{.Config.Image}}",
             container_name],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return f"Container '{container_name}' not found."

        parts = result.stdout.strip().split("|")
        status = parts[0] if len(parts) > 0 else "?"
        health = parts[1] if len(parts) > 1 else "N/A"
        started = parts[2] if len(parts) > 2 else "?"
        image = parts[3] if len(parts) > 3 else "?"

        icon = {"running": "✅", "restarting": "⚠️"}.get(status, "❌")
        health_icon = {"healthy": "💚", "unhealthy": "🔴"}.get(health, "")

        lines = [
            f"{icon} **{container_name}** ({image})",
            f"  Status: {status} {health_icon}{health}",
            f"  Started: {started}",
        ]

        # Recent logs (last 10 lines)
        log_result = subprocess.run(
            ["docker", "logs", "--tail", "10", container_name],
            capture_output=True, text=True, timeout=10
        )
        if log_result.stdout.strip():
            lines.append("\n**Recent logs:**")
            for line in log_result.stdout.strip().split("\n")[-5:]:
                lines.append(f"  `{line[:100]}`")

        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
