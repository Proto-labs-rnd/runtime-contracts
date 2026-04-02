# Changelog

## 0.4.0 - 2026-04-02
- **New:** `list-checks` subcommand showing all available check types
- **Tests:** 42 → 123 tests (coverage 46% → 66%, reporting 100%, scoring 95%, config 95%)
- **Examples:** Added `worker.contract.json` and `mcp_server.contract.json` examples
- **README:** Complete rewrite with accurate architecture, all probe types, config reference, CLI reference table
- **Tests:** Added edge case tests for all probe types, CLI subcommands, config loading, profile validation, verdict, MCP helpers

## 0.3.0 - 2026-04-01
- **Breaking:** Restructured into `src/runtime_contracts/` package (canonical layout)
- New probe checks: `dns`, `tls`, `disk`, `process`
- Config system: YAML file (`~/.config/contract-check/config.yaml`) + env overrides (`CONTRACT_CHECK_*`)
- Structured logging via stdlib `logging` (replace print statements)
- CLI flags: `-v/--verbose`, `-q/--quiet`
- Signal handling: graceful shutdown on SIGTERM/SIGINT
- Entry point changed: `runtime_contracts.cli:main`
- Added optional `pyyaml` dependency for config file support

## 0.2.0 - 2026-04-01
- Added packaged CLI via `pyproject.toml` and `contract-check` console script
- Added `validate`, `templates`, and `show-template` subcommands
- Added configurable default timeout via `--timeout` and `CONTRACT_CHECK_TIMEOUT`
- Added Markdown and HTML report export (`--json-out`, `--report`, `--report-out`)
- Improved contract loading errors with JSON line/column diagnostics
- Expanded automated tests for reports, timeouts, templates, and CLI outputs

## 0.1.0 - 2026-03-30
- Initial open-sourceable validator extracted from Labs experiments
- HTTP, MCP stdio, MCP SSE/streamable HTTP probes
- Promote gate scoring and bundled contract templates
