# Runtime Contracts 🧪

Declarative validation for services before promoting to production.

Before promoting an R&D prototype, run a contract check to verify it's actually working:
- API responding? Health checks passing? Logs clean? Error handling robust?
- Outputs a **score /10** — only promote at 10/10

## Components
| File | Description |
|------|-------------|
| `runtime-contract-check.py` | Validate services against declarative contracts (JSON). Supports HTTP, worker, MCP stdio |
| `verify-session.sh` | Verify R&D session coherence (runtime + disk = truth) |
| `session-guard.sh` | Monitor context window usage (OK / WARNING / DELEGATE) |
| `mesh_status_mcp.py` | MCP server exposing agent mesh status (6 tools) |

## Quick Start

```bash
# Validate a service against a contract
python3 runtime-contract-check.py contract.json

# Check session coherence
./verify-session.sh

# Start mesh status MCP server
python3 mesh_status_mcp.py
```

## Contract Template (JSON)
```json
{
  "service": "my-api",
  "checks": {
    "health": { "type": "http", "url": "http://localhost:8080/health", "expect": 200 },
    "smoke": { "type": "http", "url": "http://localhost:8080/api/test", "expect": 200 }
  }
}
```

## License
MIT
