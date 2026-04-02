# Contributing

## Setup
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Test
```bash
pytest
python -m py_compile runtime_contract_check.py
```

## Guidelines
- Keep contracts backward-compatible when possible
- Add a test for every new check type or CLI flag
- Prefer deterministic fixtures over real external services
- Preserve machine-readable JSON output stability
