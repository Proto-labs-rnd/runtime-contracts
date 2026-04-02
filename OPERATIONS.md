# OPERATIONS.md — Runtime Contracts

## Installation

```bash
cd projects/runtime-contracts
pip install -e .
# Optional YAML config support:
pip install -e ".[config]"
# Development:
pip install -e ".[dev]"
```

External deps: `anyio`, `httpx`, `mcp` (auto-installed).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTRACT_CHECK_TIMEOUT` | 10 | HTTP/MCP request timeout (seconds) |
| `CONTRACT_CHECK_VERBOSE` | 0 | Set to `1` for debug logging |

Contract files are JSON (or YAML with `[config]` extra). Templates bundled in `runtime_contracts/templates/`.

## Usage

```bash
contract-check templates                                    # list templates
contract-check validate contract.json                       # validate
contract-check validate contract.json --json-out out.json   # CI JSON report
contract-check validate contract.json --report md --strict  # strict mode + Markdown
```

## Health Check

```bash
contract-check templates && echo "CLI OK"
```

## Troubleshooting

- **"Module mcp not found"**: Ensure `pip install -e .` completed (installs `mcp>=1.0`).
- **MCP stdio validation hangs**: The target MCP server must respond to `initialize` within timeout. Use `--timeout` to increase.
