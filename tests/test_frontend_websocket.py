"""Frontend WebSocket API and sidebar panel JS tests."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("homeassistant") is None:
    pytest.skip("homeassistant package not installed", allow_module_level=True)

from homeassistant.exceptions import Unauthorized

from custom_components.hass_solar_orchestrator.const import DOMAIN
from custom_components.hass_solar_orchestrator.websocket_api import (
    async_register_websocket_api,
    ws_blackbox,
    ws_entities_scan,
    ws_get_snapshot,
    ws_validate_entity,
)

PANEL_JS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hass_solar_orchestrator"
    / "frontend"
    / "solar-orchestrator-panel.js"
)


def _make_hass(
    *,
    entries: dict | None = None,
    states: dict[str, tuple[str, dict]] | None = None,
) -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    if entries is not None:
        hass.data[DOMAIN] = entries
    state_map = {
        eid: SimpleNamespace(entity_id=eid, state=val, attributes=attrs)
        for eid, (val, attrs) in (states or {}).items()
    }
    hass.states.get = lambda eid: state_map.get(eid)
    return hass


def _make_conn(*, is_admin: bool | None = True) -> MagicMock:
    conn = MagicMock()
    conn.user = None if is_admin is None else SimpleNamespace(is_admin=is_admin)
    return conn


def test_register_websocket_api_idempotent() -> None:
    """Registering WebSocket commands twice only registers once."""
    hass = _make_hass()
    registered: list[object] = []

    from custom_components.hass_solar_orchestrator import websocket_api as ws_mod

    orig = ws_mod.websocket_api.async_register_command
    ws_mod.websocket_api.async_register_command = lambda _h, cmd: registered.append(cmd)
    try:
        async_register_websocket_api(hass)
        async_register_websocket_api(hass)
    finally:
        ws_mod.websocket_api.async_register_command = orig

    assert len(registered) == 4


def test_ws_get_snapshot_and_entry_id_matching() -> None:
    """Snapshot returns data + entries list for valid entry and not_found for unknown entry_id."""
    coord1 = MagicMock()
    coord1.entry = SimpleNamespace(title="Casa Principal")
    coord1.get_panel_snapshot.return_value = {"entry_id": "entry_1", "mode": "observing"}
    coord2 = MagicMock()
    coord2.entry = SimpleNamespace(title="Casa Campo")
    coord2.get_panel_snapshot.return_value = {"entry_id": "entry_2", "mode": "auto_solar"}
    hass = _make_hass(entries={"entry_1": coord1, "entry_2": coord2})
    conn = _make_conn(is_admin=False)

    ws_get_snapshot(hass, conn, {"id": 1, "type": f"{DOMAIN}/snapshot"})
    res = conn.send_result.call_args[0][1]
    assert res["entry_id"] == "entry_1"
    assert res["entries"] == [
        {"entry_id": "entry_1", "title": "Casa Principal"},
        {"entry_id": "entry_2", "title": "Casa Campo"},
    ]

    conn.reset_mock()
    ws_get_snapshot(hass, conn, {"id": 2, "type": f"{DOMAIN}/snapshot", "entry_id": "missing"})
    conn.send_error.assert_called_once()
    assert conn.send_error.call_args[0][1] == "not_found"


def test_ws_get_snapshot_handles_internal_error() -> None:
    """Unexpected coordinator errors return internal_error instead of crashing."""
    coord = MagicMock()
    coord.get_panel_snapshot.side_effect = RuntimeError("boom")
    hass = _make_hass(entries={"entry_1": coord})
    conn = _make_conn(is_admin=True)

    ws_get_snapshot(hass, conn, {"id": 3, "type": f"{DOMAIN}/snapshot"})
    conn.send_error.assert_called_once()
    assert conn.send_error.call_args[0][1] == "internal_error"


def test_ws_entities_scan_requires_admin() -> None:
    """Non-admin and unauthenticated connections cannot scan all HA entities."""
    coord = MagicMock()
    coord.scan_entities.return_value = {"roles": {}, "conflicts": []}
    hass = _make_hass(entries={"entry_1": coord})

    with pytest.raises(Unauthorized):
        ws_entities_scan(
            hass, _make_conn(is_admin=False), {"id": 1, "type": f"{DOMAIN}/entities_scan"}
        )

    with pytest.raises(Unauthorized):
        ws_entities_scan(
            hass, _make_conn(is_admin=None), {"id": 2, "type": f"{DOMAIN}/entities_scan"}
        )

    admin_conn = _make_conn(is_admin=True)
    ws_entities_scan(hass, admin_conn, {"id": 3, "type": f"{DOMAIN}/entities_scan"})
    admin_conn.send_result.assert_called_once_with(3, {"roles": {}, "conflicts": []})


def test_ws_validate_entity_permissions_and_edge_cases() -> None:
    """validate_entity requires admin and safely handles empty, kW, and NaN/Inf states."""
    hass = _make_hass(
        states={
            "sensor.grid_kw": ("-1.5", {"unit_of_measurement": "kW"}),
            "sensor.nan": ("nan", {"unit_of_measurement": "W"}),
            "sensor.inf": ("inf", {"unit_of_measurement": "W"}),
        }
    )

    with pytest.raises(Unauthorized):
        ws_validate_entity(
            hass,
            _make_conn(is_admin=False),
            {"id": 1, "type": f"{DOMAIN}/validate_entity", "entity_id": "sensor.grid_kw"},
        )

    conn = _make_conn(is_admin=True)
    ws_validate_entity(
        hass, conn, {"id": 2, "type": f"{DOMAIN}/validate_entity", "entity_id": "   "}
    )
    assert conn.send_result.call_args[0][1]["exists"] is False

    conn.reset_mock()
    ws_validate_entity(
        hass, conn, {"id": 3, "type": f"{DOMAIN}/validate_entity", "entity_id": "sensor.grid_kw"}
    )
    res_kw = conn.send_result.call_args[0][1]
    assert res_kw["exists"] is True
    assert res_kw["normalized_w"] == -1500.0

    for idx, eid in enumerate(("sensor.nan", "sensor.inf"), start=4):
        conn.reset_mock()
        ws_validate_entity(
            hass, conn, {"id": idx, "type": f"{DOMAIN}/validate_entity", "entity_id": eid}
        )
        res = conn.send_result.call_args[0][1]
        assert res["normalized_w"] is None


def test_ws_blackbox_filtering() -> None:
    """ws_blackbox forwards equipment, event_type, and collapse options."""
    coord = MagicMock()
    coord.get_filtered_blackbox.return_value = [{"type": "action", "target": "ev"}]
    hass = _make_hass(entries={"entry_1": coord})
    conn = _make_conn(is_admin=False)

    ws_blackbox(
        hass,
        conn,
        {
            "id": 10,
            "type": f"{DOMAIN}/blackbox",
            "equipment": "ev",
            "event_type": "action",
            "collapse": False,
        },
    )
    coord.get_filtered_blackbox.assert_called_once_with(
        equipment="ev", event_type="action", collapse=False
    )
    conn.send_result.assert_called_once_with(10, {"entries": [{"type": "action", "target": "ev"}]})


def test_panel_js_syntax_and_dom_behavior() -> None:
    """Verify panel JS syntax and core DOM, escaping, and permission behaviors in Node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")

    syntax = subprocess.run([node, "--check", str(PANEL_JS)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

class FakeEl {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.style = {};
    this.dataset = {};
    this.attrs = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this._innerHTML = "";
    this.id = "";
    this.classList = {
      _set: new Set(),
      toggle: (k, force) => {
        if (force === undefined) {
          if (this.classList._set.has(k)) this.classList._set.delete(k);
          else this.classList._set.add(k);
        } else if (force) this.classList._set.add(k);
        else this.classList._set.delete(k);
      },
      contains: (k) => this.classList._set.has(k),
    };
  }
  set innerHTML(v) {
    this._innerHTML = String(v);
    this._parseIds(this._innerHTML);
  }
  get innerHTML() { return this._innerHTML; }
  _parseIds(html) {
    this._byId = new Map();
    const re = /<([a-z0-9-]+)\b([^>]*)>/gi;
    let m;
    while ((m = re.exec(html))) {
      const tag = m[1];
      const attrs = m[2];
      const idM = /\bid="([^"]+)"/.exec(attrs);
      if (idM) {
        const el = new FakeEl(tag);
        el.id = idM[1];
        const cfgM = /\bdata-cfg="([^"]+)"/.exec(attrs);
        if (cfgM) el.dataset.cfg = cfgM[1];
        const typeM = /\btype="([^"]+)"/.exec(attrs);
        if (typeM) el.type = typeM[1];
        this._byId.set(el.id, el);
      }
    }
  }
  getElementById(id) {
    return this._byId ? (this._byId.get(id) || null) : null;
  }
  querySelectorAll(sel) {
    if (sel === "[data-cfg]" && this._byId) {
      return [...this._byId.values()].filter((e) => e.dataset.cfg);
    }
    return [];
  }
  addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  closest() { return null; }
}

class FakeHTMLElement {
  constructor() {
    this.isConnected = true;
    this.shadowRoot = new FakeEl("shadow-root");
    this.shadowRoot.activeElement = null;
  }
  attachShadow() { return this.shadowRoot; }
}

const defined = {};
const sandbox = {
  HTMLElement: FakeHTMLElement,
  customElements: { define: (name, cls) => { defined[name] = cls; } },
  window: {
    location: { hash: "" },
    setInterval: () => 1,
    clearInterval: () => {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    requestAnimationFrame: (fn) => { fn(); return 1; },
    cancelAnimationFrame: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
  },
  document: {
    hidden: false,
    addEventListener: () => {},
    removeEventListener: () => {},
    createElement: (t) => new FakeEl(t),
  },
  URL: { createObjectURL: () => "blob:1", revokeObjectURL: () => {} },
  Blob: class {},
  console,
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);

const Panel = defined["solar-orchestrator-panel"];
assert.ok(Panel, "Panel custom element registered");

const panel = new Panel();
panel._mountOnce();

// 1. Escaping check
assert.strictEqual(
  panel._esc('<img src=x onerror="a">'),
  "&lt;img src=x onerror=&quot;a&quot;&gt;"
);

// 2. Patch DOM with activeElement === null renders granular-loads-list & alerts-list (escaped)
const snapshot = {
  entry_id: "entry_abc",
  mode: "observing",
  load_modes: { pool: "observing", heater: "active", ev: "observing" },
  conflicts: [{ load: "ev", entity_id: "automation.<bad>" }],
  snapshot: { solar_w: 2500, house_w: 500, grid_w: -2000, battery_w: 0, battery_soc: 80 },
  plan: {
    traffic_light: "optimal",
    human_headline: "Todo bien",
    alerts: [{ alert_id: "a1", severity: "warning", title: "Aviso <x>", detail: "Detalle & más" }],
  },
  config: { contracted_power_w: 4600 },
  blackbox: [],
};
const wsCalls = [];
const svcCalls = [];
panel._hass = {
  user: { is_admin: true },
  callWS: async (msg) => {
    wsCalls.push(msg);
    return msg.type.endsWith("/blackbox") ? { entries: [] } : snapshot;
  },
  callService: async (dom, svc, data) => { svcCalls.push({ dom, svc, data }); },
};
panel._snapshot = snapshot;
panel._patchDOM();

const glHtml = panel.shadowRoot.getElementById("granular-loads-list").innerHTML;
assert.ok(
  glHtml.includes("Depuradora"),
  "granular-loads-list rendered even when activeElement is null"
);
assert.ok(glHtml.includes("automation.&lt;bad&gt;"), "conflict entity_id is HTML-escaped");

const alertsHtml = panel.shadowRoot.getElementById("alerts-list").innerHTML;
assert.ok(alertsHtml.includes("Aviso &lt;x&gt;"), "alert title rendered and escaped");
assert.ok(alertsHtml.includes("Detalle &amp; más"), "alert detail rendered and escaped");

// 3. Admin service call forwards config_entry_id and pins entry_id across WS calls
(async () => {
  await panel._fetchSnapshot();
  assert.strictEqual(panel._selectedEntryId, "entry_abc");
  await panel._callService("set_mode", { mode: "auto_solar" });
  assert.strictEqual(svcCalls.length, 1);
  assert.strictEqual(svcCalls[0].data.config_entry_id, "entry_abc");
  await panel._refreshBlackbox();
  assert.strictEqual(wsCalls.at(-1).entry_id, "entry_abc");

  // 4. Fail-closed admin default & non-admin permissions check
  panel._hass = null;
  assert.strictEqual(panel._isAdmin(), false, "fail-closed when hass is null");
  panel._hass = { user: {} };
  assert.strictEqual(panel._isAdmin(), false, "fail-closed when is_admin is missing");

  let serviceCalled = false;
  panel._hass = {
    user: { is_admin: false },
    callWS: async () => snapshot,
    callService: async () => { serviceCalled = true; },
  };
  panel._patchDOM();
  assert.strictEqual(panel._isAdmin(), false);
  assert.strictEqual(panel.shadowRoot.getElementById("config-readonly-banner").hidden, false);
  assert.strictEqual(panel.shadowRoot.getElementById("btn-scan-entities").disabled, true);
  assert.strictEqual(panel.shadowRoot.getElementById("btn-manual-total").disabled, true);

  const ok = await panel._callService("set_mode", { mode: "auto_solar" });
  assert.strictEqual(ok, false);
  assert.strictEqual(serviceCalled, false, "non-admin blocked before calling HA service");

  // 5. Compact selector visibility + entry switching clears state & rejects late responses
  assert.strictEqual(
    panel.shadowRoot.getElementById("sel-entry").hidden, true, "hidden for 1 entry"
  );

  let resolveLate;
  const latePromise = new Promise((r) => { resolveLate = r; });
  const snap2 = {
    ...snapshot,
    entry_id: "entry_2",
    entries: [
      { entry_id: "entry_abc", title: "Casa 1" },
      { entry_id: "entry_2", title: "Casa 2" },
    ],
  };
  panel._hass = {
    user: { is_admin: true },
    callWS: (msg) => (msg.entry_id === "entry_2" ? Promise.resolve(snap2) : latePromise),
    callService: async () => { serviceCalled = true; },
  };
  panel._snapshot = { ...snapshot, entries: snap2.entries };
  panel._patchDOM();
  assert.strictEqual(
    panel.shadowRoot.getElementById("sel-entry").hidden, false, "shown for >1 entry"
  );

  // Set dirty draft, scan, blackbox, and start an in-flight fetch for entry_abc
  panel._draft = { contracted_power_w: 5000 };
  panel._dirty = true;
  panel._scan = { roles: { a: {} } };
  panel.shadowRoot.getElementById("bb-list").innerHTML = "<div>old</div>";
  const oldFetch = panel._fetchSnapshot(true);

  // Switch to entry_2 before oldFetch resolves
  panel._selectEntry("entry_2");
  assert.strictEqual(Object.keys(panel._draft).length, 0);
  assert.strictEqual(panel._dirty, false);
  assert.strictEqual(panel._scan, null);
  assert.strictEqual(panel.shadowRoot.getElementById("bb-list").innerHTML, "");

  // Cannot command wrong entry if snapshot still points to entry_abc
  panel._snapshot = { ...snapshot, entry_id: "entry_abc" };
  serviceCalled = false;
  const blocked = await panel._callService("set_mode", { mode: "auto_solar" });
  assert.strictEqual(blocked, false);
  assert.strictEqual(serviceCalled, false);

  // Resolve late response from entry_abc and confirm it is discarded
  resolveLate({ ...snapshot, entry_id: "entry_abc" });
  await oldFetch;
  assert.strictEqual(panel._selectedEntryId, "entry_2");

  // 6. Accessible labels for all config inputs
  const html = panel.shadowRoot.innerHTML;
  const configHtml = html.slice(html.indexOf('id="view-config"'));
  const labels = [...configHtml.matchAll(/<label\b([^>]*)>/g)].map((m) => m[1]);
  assert.ok(labels.length >= 10);
  for (const attrs of labels) {
    assert.ok(/\bfor="[^"]+"/.test(attrs), `Config label missing for attribute: ${attrs}`);
  }
})();
"""
    res = subprocess.run(
        [node, "-e", harness, str(PANEL_JS)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Node harness failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}"
