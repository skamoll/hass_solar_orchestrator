# Contributing to Solar Excess Orchestrator

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest -v
```
