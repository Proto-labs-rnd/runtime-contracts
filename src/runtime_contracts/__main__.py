"""Allow `python -m runtime_contracts` to run the CLI."""
from runtime_contracts.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
