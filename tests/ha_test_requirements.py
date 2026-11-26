"""Print the pip requirements of the HA core components the integration loads in tests.

Home Assistant installs component requirements (e.g. home-assistant-frontend) at
runtime, which tests disable with ``skip_pip``. CI runs this script against the
installed Home Assistant so the pins always match that HA version.
"""

from __future__ import annotations

import json
from pathlib import Path

import homeassistant

ROOT = Path(__file__).resolve().parent.parent
CORE = Path(homeassistant.__file__).parent / "components"
# Imported at runtime by onboarding -> analytics, but not declared as a manifest dependency.
EXTRA_COMPONENTS = ("hassio",)


def main() -> None:
    own = json.loads((ROOT / "custom_components/hass_solar_orchestrator/manifest.json").read_text())
    todo = [*own.get("dependencies", []), *own.get("after_dependencies", []), *EXTRA_COMPONENTS]
    seen: set[str] = set()
    reqs: set[str] = set()
    while todo:
        domain = todo.pop()
        if domain in seen:
            continue
        seen.add(domain)
        manifest = CORE / domain / "manifest.json"
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text())
        reqs.update(data.get("requirements", []))
        todo += [*data.get("dependencies", []), *data.get("after_dependencies", [])]
    print("\n".join(sorted(reqs)))


if __name__ == "__main__":
    main()
