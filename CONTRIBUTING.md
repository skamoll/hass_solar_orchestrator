# Contributing to Solar Excess Orchestrator

## Development Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install ".[dev]"
ruff check .
ruff format --check .
pytest -v
```
