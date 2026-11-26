# Contributing to Solar Excess Orchestrator

## Development Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install ".[dev]"
python tests/ha_test_requirements.py > ha-requirements.txt
pip install -r ha-requirements.txt
ruff check .
ruff format --check .
pytest -v
```
