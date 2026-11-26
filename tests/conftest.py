"""Make pure engine modules importable in the lightweight test matrix."""

import importlib.util
import sys
import types
from pathlib import Path

if importlib.util.find_spec("homeassistant") is None:
    _pkg = types.ModuleType("custom_components.hass_solar_orchestrator")
    _pkg.__path__ = [
        str(
            Path(__file__).resolve().parent.parent / "custom_components" / "hass_solar_orchestrator"
        )
    ]
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules["custom_components.hass_solar_orchestrator"] = _pkg
