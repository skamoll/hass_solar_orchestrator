"""Structural tests for Solar Excess Orchestrator manifest, HACS, and translations."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMP = ROOT / "custom_components" / "hass_solar_orchestrator"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_and_hacs() -> None:
    """Validate manifest.json and hacs.json required fields."""
    manifest = _load_json(COMP / "manifest.json")
    hacs = _load_json(ROOT / "hacs.json")

    assert manifest["domain"] == "hass_solar_orchestrator"
    assert manifest["config_flow"] is True
    assert manifest["categories"] == ["energy"]
    assert hacs["name"] == "Solar Excess Orchestrator"


def test_translations_parity() -> None:
    """Ensure strings.json, en.json, and es.json have identical key structures."""
    strings = _load_json(COMP / "strings.json")
    en = _load_json(COMP / "translations" / "en.json")
    es = _load_json(COMP / "translations" / "es.json")

    assert strings == en
    assert set(strings.keys()) == set(es.keys())
    for platform in ("sensor", "binary_sensor", "select", "number", "switch"):
        assert set(strings["entity"][platform].keys()) == set(es["entity"][platform].keys())
    assert set(strings["options"]["step"]["init"]["data"].keys()) == set(
        es["options"]["step"]["init"]["data"].keys()
    )
    assert set(strings["issues"].keys()) == set(es["issues"].keys())


def test_translation_keys_exist() -> None:
    """Every translation_key in platforms exists in strings.json."""
    strings = _load_json(COMP / "strings.json")
    for fname, plat in (
        ("sensor.py", "sensor"),
        ("binary_sensor.py", "binary_sensor"),
        ("number.py", "number"),
        ("switch.py", "switch"),
    ):
        src = (COMP / fname).read_text(encoding="utf-8")
        keys = set(re.findall(r'translation_key="([^"]+)"', src))
        assert keys <= set(strings["entity"][plat].keys())
