# Runtime Contracts

Declarative runtime validator with a built-in **PROMOTE gate scorer**.  
Write a JSON contract describing what your service *should* look like at runtime — artifacts, health checks, smoke tests, failure handling, logs, guards — and get a structured report plus a `/10` readiness score.

## Features

- **Declarative contracts** — Describe expected runtime state in JSON
- **HTTP API validation** — Health, smoke, failure, and integration endpoint checks
- **MCP server validation** — Native stdio and HTTP/SSE transport probes with `initialize → tools/list → safe tool call` handshake
- **PROMOTE gate `/10`** — 10-item checklist scoring artifacts, execution, outputs, health, packaging, failure handling, logs, guards, integration, and verdict
- **Profile-aware** — Built-in profiles for `http_api`, `worker`, and `mcp_server` with required/recommended field validation
- **Zero dependencies for HTTP checks** — Uses stdlib `urllib` for HTTP, optional `httpx` + `mcp` for MCP probes

## Install

```bash
pip install -r requirements.txt
```

Minimal (HTTP-only, no MCP):
```
# No extra deps needed — uses stdlib urllib
```

## Quick Start

1. Create a contract JSON (or use a template from `templates/`):

```json
{
  "name": "my-api",
  "contract_type": "http_api",
  "artifacts": ["app.py", "README.md"],
  "deps": ["python3"],
  "packaging": ["requirements.txt"],
  "health": {
    "url": "http://127.0.0.1:8080/health",
    "expect_status": 200,
    "contains": ["ok"]
  },
  "smoke": [
    {
      "name": "happy_path",
      "method": "POST",
      "url": "http://127.0.0.1:8080/api",
      "json": {"query": "test"},
      "expect_status": 200
    }
  ],
  "logs": {"path": "/tmp/my-api.log", "contains": ["request", "error"]},
  "guards": {"host": "127.0.0.1", "port": 8080},
  "verdict": {
    "claim": "API is ready for promotion",
    "reason": "All runtime checks pass on loopback"
  }
}
```

2. Run the validator:

```bash
python runtime_contract_check.py contract.json
python runtime_contract_check.py contract.json --strict  # exit 1 on failures
```

3. Check the report:

```json
{
  "summary": {"passed": 8, "failed": 0, "total": 8, "ok": true},
  "promote_gate": {
    "score": 10,
    "max_score": 10,
    "ready": true,
    "status": "PROMOTE",
    "checklist": [...]
  }
}
```

## Contract Fields

| Field | Description |
|-------|-------------|
| `name` | Service name |
| `contract_type` | Profile: `http_api`, `worker`, or `mcp_server` |
| `artifacts` | List of file paths that must exist |
| `deps` | List of CLI tools that must be on PATH |
| `packaging` | List of packaging files that must exist |
| `health` | HTTP check or MCP handshake proof |
| `smoke` | Happy-path request group |
| `failures` | Expected-failure request group |
| `integration` | Integration proof request group |
| `logs` | Log file + expected content strings |
| `guards` | Network binding or transport constraint |
| `verdict` | Human claim + reason + next action |
| `mcp` | MCP server probe config (see below) |

## MCP Server Validation

For MCP servers, use the `mcp_server` profile with a `mcp` block:

```json
{
  "name": "my-mcp-server",
  "contract_type": "mcp_server",
  "mcp": {
    "transport": "stdio",
    "command": "python3",
    "args": ["my_server.py"],
    "safe_tool": {"name": "list_items"},
    "integration_tool": {
      "name": "process_data",
      "arguments": {"input": "test"},
      "contains": ["result"]
    },
    "failure_tool": {
      "name": "list_items",
      "arguments": {"invalid": true},
      "expect_error": true
    },
    "stderr_log": "/tmp/my-mcp.log"
  },
  "logs": {"path": "/tmp/my-mcp.log", "contains": ["initialized"]},
  "guards": {"transport": "stdio"},
  "verdict": {"claim": "MCP server is production-ready", "reason": "Handshake + tool calls pass"}
}
```

For remote MCP servers over HTTP/SSE:

```json
{
  "mcp": {
    "transport": "sse",
    "url": "http://127.0.0.1:3000/sse",
    "safe_tool": {"name": "list_items"}
  }
}
```

## PROMOTE Gate Checklist

The scorer evaluates 10 items (1 point each):

1. **Artifacts** — Declared files exist
2. **Execution** — Start/stop/health commands are specified
3. **Outputs** — Health and smoke responses contain expected content
4. **Health** — Health + smoke checks pass
5. **Packaging** — Packaging files exist
6. **Failure handling** — Failure cases behave correctly
7. **Logs** — Log files contain expected markers
8. **Guards** — Network/transport constraints satisfied
9. **Integration** — Integration proof passes
10. **Verdict** — Human claim + reason documented

Score `/10` → `PROMOTE` status. Anything less → `HOLD`.

## Templates

Starter contracts in `templates/`:

- `http_api.template.json` — HTTP API service
- `worker.template.json` — Background worker
- `mcp_server.template.json` — MCP server (stdio)
- `mcp_server_http.template.json` — MCP server (HTTP/SSE)

## Running Tests

```bash
python -m pytest tests/ -v
```

## Architecture

```
runtime_contract_check.py    # Main validator (CLI entry point)
├── Contract loading & profile validation
├── HTTP checks (stdlib urllib)
├── MCP checks (anyio + mcp SDK)
├── Path/deps/log/guard checks
├── PROMOTE gate scorer (/10)
└── JSON report output

templates/                    # Contract templates by profile
tests/                        # Self-contained test suite
```

## License

MIT
