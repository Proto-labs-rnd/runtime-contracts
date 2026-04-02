# Runtime Contracts

Declarative runtime validator for services, workers, and MCP servers.
It checks what is *actually running* and scores promotion-readiness with a built-in `/10` gate.

## Why

Tests tell you code *can* work. Runtime contracts prove the deployed thing *does* work:

- Required files and dependencies exist
- Health and smoke endpoints respond correctly
- Failure paths return expected errors (not stacktraces)
- Logs contain observability markers
- Network guards (loopback binding, transport type) are enforced
- MCP handshake works end-to-end (initialize → tools/list → tool call)
- DNS resolves, TLS certs are valid, disk has space, processes are alive

## Features

- **HTTP API validation** via stdlib `urllib` — no external HTTP client needed
- **MCP validation** for `stdio`, `sse`, and `streamable_http` transports
- **Infrastructure probes**: DNS resolution, TLS cert expiry, disk usage, process liveness
- **Profile-aware contracts**: `http_api`, `worker`, `mcp_server` with required/recommended fields
- **Promote gate scoring** across 10 dimensions (artifacts, execution, outputs, health, packaging, failure handling, logs, guards, integration, verdict)
- **Multi-format reports**: JSON (CI-friendly), Markdown, HTML
- **Bundled templates**: `http_api`, `worker`, `mcp_server`, `mcp_server_http`
- **Configurable**: YAML config file, environment variables, CLI flags
- **Structured logging** with `-v/--verbose` and `-q/--quiet`
- **Signal handling**: graceful shutdown on SIGTERM/SIGINT

## Install

```bash
pip install -e .
# Optional: YAML config support
pip install -e ".[config]"
# Development
pip install -e ".[dev]"
```

## Quickstart

List bundled templates:

```bash
contract-check templates
```

Validate a contract:

```bash
contract-check validate examples/http_api.contract.json
```

CI-friendly JSON + Markdown reports:

```bash
contract-check validate contract.json \
  --json-out build/runtime-report.json \
  --report md \
  --report-out build/runtime-report.md
```

List all available check types:

```bash
contract-check list-checks
```

## CLI Reference

```
contract-check validate <contract.json> [--strict] [--timeout N] [--json-out PATH] [--report md|html] [--report-out PATH]
contract-check templates
contract-check show-template <name>
contract-check list-checks
```

### Flags

| Flag | Description |
|------|-------------|
| `--strict` | Exit non-zero if any runtime check fails |
| `--timeout N` | Default timeout (seconds) for checks missing explicit timeouts |
| `--json-out PATH` | Write machine-readable JSON report |
| `--report md\|html` | Generate human-readable report |
| `--report-out PATH` | Destination path for report |
| `-v / --verbose` | Enable DEBUG logging |
| `-q / --quiet` | Suppress all output except errors |
| `--version` | Print version |

## Contract Format

A contract is a JSON file describing what to validate.

### Core Fields

| Field | Type | Purpose |
|-------|------|---------|
| `name` | string | Service name in reports |
| `contract_type` | string | `http_api`, `worker`, or `mcp_server` |
| `commands` | object | `start`, `stop`, `health`, `smoke` commands for evidence |
| `artifacts` | string[] | Files that must exist |
| `deps` | string[] | Executables required on PATH |
| `packaging` | string[] | Packaging files that must exist |
| `health` | object | Health check (HTTP or MCP) |
| `smoke` | array/object | Happy-path checks |
| `failures` | array/object | Expected failure checks |
| `integration` | array/object | Cross-service integration proof |
| `logs` | object | Log file and expected markers |
| `guards` | object | Network guards (transport type or bind address) |
| `verdict` | object | Human claim + reason + next action |
| `mcp` | object | MCP transport configuration |

### Infrastructure Probes

| Field | Type | What it checks |
|-------|------|---------------|
| `dns` | `{"hostname": "example.com"}` | DNS resolution with latency |
| `tls` | `{"host": "x", "port": 443, "min_days": 7}` | TLS cert expiry |
| `disk` | `{"path": "/data", "min_free_percent": 10}` | Disk usage |
| `process` | `{"name": "nginx"}` or `{"pidfile": "/run/x.pid"}` | Process liveness |

### HTTP Check Case

```json
{
  "name": "search-ok",
  "method": "POST",
  "url": "http://127.0.0.1:8080/search",
  "json": {"query": "hello"},
  "expect_status": 200,
  "contains": ["results"],
  "expect_json": {"success": true},
  "timeout": 10
}
```

### MCP Configuration

```json
{
  "mcp": {
    "transport": "stdio",
    "command": "python3",
    "args": ["my_server.py"],
    "safe_tool": {"name": "list_items", "arguments": {}},
    "failure_tool": {
      "name": "get_item",
      "arguments": {"id": -1},
      "expect_error": true
    },
    "integration_tool": {"name": "search", "arguments": {"q": "test"}},
    "timeout_seconds": 15
  }
}
```

For SSE/HTTP transports:
```json
{
  "mcp": {
    "transport": "sse",
    "url": "http://localhost:3001/sse",
    "headers": {"Authorization": "Bearer token"},
    "safe_tool": {"name": "list"},
    "timeout_seconds": 10
  }
}
```

## Promote Gate

The gate scores 10 checklist items:

| # | Category | Description |
|---|----------|-------------|
| 1 | Artifacts | Required files exist |
| 2 | Execution | Commands are reproducible (start/stop/health + smoke) |
| 3 | Outputs | Health and smoke produce real output |
| 4 | Health | Health + smoke checks pass |
| 5 | Packaging | Packaging files present |
| 6 | Failure handling | Failure paths behave correctly |
| 7 | Logs | Log file exists with expected markers |
| 8 | Guards | Network constraints verified |
| 9 | Integration | Cross-service integration proven |
| 10 | Verdict | Human claim + reason documented |

**10/10 = PROMOTE**, anything less = HOLD.

## Configuration

### YAML Config File

Location: `~/.config/contract-check/config.yaml`

```yaml
default_timeout: 15
log_level: INFO
report_output_dir: ./reports
```

### Environment Variables

| Variable | Overrides |
|----------|-----------|
| `CONTRACT_CHECK_TIMEOUT` | Default timeout |
| `CONTRACT_CHECK_LOG_LEVEL` | Log level |
| `CONTRACT_CHECK_REPORT_DIR` | Report output directory |

Priority: CLI flags > env vars > config file > defaults.

## Architecture

```
src/runtime_contracts/
  __init__.py       # Version
  __main__.py       # python -m entry
  checks.py         # Core check functions (HTTP, filesystem, deps, guards, DNS, TLS, disk, process)
  cli.py            # CLI argument parsing, signal handling, validation orchestration
  config.py         # YAML + env + defaults configuration
  mcp.py            # MCP transport probing (stdio, SSE, streamable HTTP)
  reporting.py      # JSON, Markdown, HTML report generation
  scoring.py        # Promote gate scoring and profile validation
templates/          # Bundled contract templates
examples/           # Runnable examples
tests/              # Self-contained unit + integration tests
```

### Dependencies

- **anyio** + **mcp** — MCP transport probing
- **httpx** — MCP HTTP client (transitive)
- **pyyaml** (optional) — YAML config file support

No external HTTP client needed for HTTP checks (uses stdlib `urllib`).

## Development

```bash
pip install -e ".[dev]"
pytest
pytest --cov=runtime_contracts
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor workflow.

## License

MIT
