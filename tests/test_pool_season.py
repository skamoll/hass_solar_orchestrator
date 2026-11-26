"""Tests for pool season window, transitions, legacy compat, config flow, and panel."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.hass_solar_orchestrator.const import OperatingMode
from custom_components.hass_solar_orchestrator.engine import evaluate
from custom_components.hass_solar_orchestrator.models import (
    EnergySnapshot,
    OrchestratorConfig,
    OrchestratorState,
    is_pool_in_season,
    is_valid_month_day,
)

PANEL_JS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hass_solar_orchestrator"
    / "frontend"
    / "solar-orchestrator-panel.js"
)


def _snap(
    now: datetime,
    *,
    grid_w: float = -2000.0,
    solar_w: float = 3000.0,
    pool_on: bool = False,
    pool_power_w: float | None = None,
    heater_on: bool = False,
    heater_temp_c: float | None = 55.0,
    tz: ZoneInfo | None = None,
) -> EnergySnapshot:
    return EnergySnapshot(
        now=now,
        local_sample_time=now,
        solar_power_w=solar_w,
        load_power_w=max(0.0, solar_w + grid_w),
        grid_power_w=grid_w,
        battery_power_w=0.0,
        battery_soc=80.0,
        pool_on=pool_on,
        pool_power_w=pool_power_w,
        heater_on=heater_on,
        heater_temp_c=heater_temp_c,
        calendar_timezone=tz,
    )


def _active_cfg(**kwargs: object) -> OrchestratorConfig:
    return OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        load_modes={"pool": "active", "heater": "active", "ev": "active"},
        **kwargs,  # type: ignore[arg-type]
    )


def test_season_window_normal_and_wrapping() -> None:
    """Normal summer window and year-wrapping winter window evaluate boundaries accurately."""
    summer = OrchestratorConfig(
        pool_season_enabled=True,
        pool_season_start="05-01",
        pool_season_end="09-30",
    )
    assert is_pool_in_season(datetime(2026, 4, 30, 12, 0, tzinfo=UTC), summer) is False
    assert is_pool_in_season(datetime(2026, 5, 1, 0, 0, tzinfo=UTC), summer) is True
    assert is_pool_in_season(datetime(2026, 7, 15, 12, 0, tzinfo=UTC), summer) is True
    assert is_pool_in_season(datetime(2026, 9, 30, 23, 59, tzinfo=UTC), summer) is True
    assert is_pool_in_season(datetime(2026, 10, 1, 0, 0, tzinfo=UTC), summer) is False

    wrapping = OrchestratorConfig(
        pool_season_enabled=True,
        pool_season_start="11-01",
        pool_season_end="03-31",
    )
    assert is_pool_in_season(datetime(2026, 10, 31, 23, 59, tzinfo=UTC), wrapping) is False
    assert is_pool_in_season(datetime(2026, 11, 1, 0, 0, tzinfo=UTC), wrapping) is True
    assert is_pool_in_season(datetime(2026, 12, 31, 23, 59, tzinfo=UTC), wrapping) is True
    assert is_pool_in_season(datetime(2027, 1, 1, 0, 0, tzinfo=UTC), wrapping) is True
    assert is_pool_in_season(datetime(2027, 3, 31, 23, 59, tzinfo=UTC), wrapping) is True
    assert is_pool_in_season(datetime(2027, 4, 1, 0, 0, tzinfo=UTC), wrapping) is False

    assert is_valid_month_day("02-29") is True
    assert is_valid_month_day("02-30") is False
    assert is_valid_month_day("13-01") is False
    assert is_valid_month_day("5-1") is False


def test_legacy_config_without_season_fields_always_in_season() -> None:
    """Existing configs without pool_season_* fields behave as always in season."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR)
    assert cfg.pool_season_enabled is False
    jan = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert is_pool_in_season(jan, cfg) is True

    state = OrchestratorState(started_at=jan - timedelta(minutes=10))
    plan, state = evaluate(_snap(jan, grid_w=-1500.0), cfg, state)
    assert plan.pool_in_season is True
    assert state.pool_export_since == jan


def test_out_of_season_unmanaged_plain_socket() -> None:
    """Out of season: never start, never shed manual load, no quota, no priority blocking."""
    cfg = _active_cfg(
        pool_season_enabled=True,
        pool_season_start="05-01",
        pool_season_end="09-30",
        priority_order=("house", "pool", "ev", "heater"),
        pool_daily_max_minutes=60,
    )
    nov = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    state = OrchestratorState(
        started_at=nov - timedelta(minutes=20),
        pool_last_tick=nov - timedelta(minutes=1),
        pool_minutes_today=120.0,  # exceeds daily max, must be ignored out of season
    )

    # 1. Manual switch ON with high grid import & exceeded quota: never shed, no quota accumulation
    plan, state = evaluate(
        _snap(nov, grid_w=6000.0, solar_w=0.0, pool_on=True, pool_power_w=900.0),
        cfg,
        state,
    )
    assert plan.pool_in_season is False
    assert plan.actions == ()
    assert plan.simulated_actions == ()
    assert state.pool_minutes_today == 120.0
    pool_flex = next(f for f in plan.flex_loads if f.load_id == "pool")
    assert pool_flex.state == "out_of_season"
    assert "Fuera de temporada" in pool_flex.reason

    # 2. Switch OFF with large surplus & pool ranked above heater: never starts or blocks heater
    cfg_heater = _active_cfg(
        pool_season_enabled=True,
        pool_season_start="05-01",
        pool_season_end="09-30",
        priority_order=("house", "pool", "ev", "heater"),
        heater_target_temp_c=60.0,
        heater_hold_seconds=60,
        pool_hold_seconds=60,
    )
    state2 = OrchestratorState(started_at=nov - timedelta(minutes=20))
    t0 = nov
    plan, state2 = evaluate(
        _snap(t0, grid_w=-1500.0, pool_on=False, heater_on=False, heater_temp_c=40.0),
        cfg_heater,
        state2,
    )
    assert state2.pool_export_since is None
    assert state2.heater_export_since == t0
    assert "heater_waits_for_priority:pool" not in plan.reasons

    t1 = t0 + timedelta(seconds=65)
    plan, state2 = evaluate(
        _snap(t1, grid_w=-1500.0, pool_on=False, heater_on=False, heater_temp_c=40.0),
        cfg_heater,
        state2,
    )
    assert any(a.target == "heater" and a.action == "turn_on" for a in plan.actions)
    assert not any(a.target == "pool" for a in plan.actions)


def test_season_transition_end_solar_owned_vs_manual_and_shadow() -> None:
    """Ending season releases solar-owned pool once, never touches manual, respects shadow."""
    tz = ZoneInfo("Europe/Madrid")
    cfg = _active_cfg(
        pool_season_enabled=True,
        pool_season_start="05-01",
        pool_season_end="09-30",
    )
    oct1 = datetime(2026, 10, 1, 10, 0, tzinfo=tz)

    # 1. Active mode, pool ON & solar_owned=True -> turns off once and clears ownership
    state = OrchestratorState(started_at=oct1 - timedelta(hours=1), pool_solar_owned=True)
    plan, state = evaluate(_snap(oct1, pool_on=True, pool_power_w=250.0, tz=tz), cfg, state)
    assert plan.pool_in_season is False
    assert any(
        a.target == "pool" and a.action == "turn_off" and a.reason == "pool_out_of_season"
        for a in plan.actions
    )
    assert state.pool_solar_owned is False

    # Next tick: even if user turns it back on manually, never touched again
    plan2, state = evaluate(
        _snap(oct1 + timedelta(seconds=10), pool_on=True, pool_power_w=250.0, tz=tz),
        cfg,
        state,
    )
    assert not any(a.target == "pool" for a in plan2.actions)

    # 2. Manual pool ON (pool_solar_owned=False) -> never touched
    state_manual = OrchestratorState(started_at=oct1 - timedelta(hours=1), pool_solar_owned=False)
    plan_manual, _ = evaluate(
        _snap(oct1, pool_on=True, pool_power_w=250.0, tz=tz), cfg, state_manual
    )
    assert not any(a.target == "pool" for a in plan_manual.actions)

    # 3. Shadow / observing mode -> simulated_actions only, 0 physical actions, ownership untouched
    cfg_shadow = OrchestratorConfig(
        mode=OperatingMode.OBSERVING,
        pool_season_enabled=True,
        pool_season_start="05-01",
        pool_season_end="09-30",
    )
    state_shadow = OrchestratorState(started_at=oct1 - timedelta(hours=1), pool_solar_owned=True)
    plan_shadow, state_shadow = evaluate(
        _snap(oct1, pool_on=True, pool_power_w=250.0, tz=tz), cfg_shadow, state_shadow
    )
    assert plan_shadow.actions == ()
    assert any(
        a.target == "pool" and a.action == "turn_off" and a.reason == "pool_out_of_season"
        for a in plan_shadow.simulated_actions
    )
    assert state_shadow.pool_solar_owned is True


def test_season_transition_start_resumes_management() -> None:
    """When season starts, normal pool management resumes on the next tick."""
    cfg = _active_cfg(
        pool_season_enabled=True,
        pool_season_start="05-01",
        pool_season_end="09-30",
        pool_hold_seconds=60,
    )
    apr30 = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    may1 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    state = OrchestratorState(started_at=apr30 - timedelta(hours=1))

    plan_out, state = evaluate(_snap(apr30, grid_w=-1500.0), cfg, state)
    assert plan_out.pool_in_season is False
    assert state.pool_export_since is None

    plan_in, state = evaluate(_snap(may1, grid_w=-1500.0), cfg, state)
    assert plan_in.pool_in_season is True
    assert state.pool_export_since == may1

    plan_start, state = evaluate(_snap(may1 + timedelta(seconds=65), grid_w=-1500.0), cfg, state)
    assert any(a.target == "pool" and a.action == "turn_on" for a in plan_start.actions)
    assert state.pool_solar_owned is True


@pytest.mark.asyncio
async def test_config_and_options_flow_pool_season_validation() -> None:
    """Config flow sets defaults; options flow validates MM-DD and preserves legacy default."""
    if importlib.util.find_spec("homeassistant") is None:
        pytest.skip("homeassistant package not installed")

    from custom_components.hass_solar_orchestrator.config_flow import (
        SolarOrchestratorConfigFlow,
        SolarOrchestratorOptionsFlow,
    )

    flow = SolarOrchestratorConfigFlow()
    res = await flow.async_step_user(
        {
            "solar_power_entity": "sensor.pv",
            "load_power_entity": "sensor.load",
            "grid_power_entity": "sensor.grid",
        }
    )
    assert res["type"] == "create_entry"
    assert res["options"]["pool_season_enabled"] is True
    assert res["options"]["pool_season_start"] == "05-01"
    assert res["options"]["pool_season_end"] == "09-30"

    # Legacy entry without pool_season_* in options
    legacy_entry = SimpleNamespace(
        options={
            "eval_interval_seconds": 10,
            "stale_timeout_seconds": 30,
            "heater_start_export_w": 1150.0,
            "pool_start_export_w": 1050.0,
            "import_shed_threshold_w": 300.0,
        }
    )
    from unittest.mock import patch

    opt_flow = SolarOrchestratorOptionsFlow()
    with patch.object(SolarOrchestratorOptionsFlow, "config_entry", new=legacy_entry, create=True):
        # Initial form defaults to pool_season_enabled=False for legacy entries
        init_form = await opt_flow.async_step_init()
        assert init_form["type"] == "form"

        # Invalid MM-DD rejected
        bad = await opt_flow.async_step_init(
            {
                "eval_interval_seconds": 10,
                "stale_timeout_seconds": 30,
                "heater_start_export_w": 1150.0,
                "pool_start_export_w": 1050.0,
                "import_shed_threshold_w": 300.0,
                "pool_season_enabled": True,
                "pool_season_start": "02-30",
                "pool_season_end": "13-01",
            }
        )
        assert bad["type"] == "form"
        assert bad["errors"]["pool_season_start"] == "invalid_month_day"
        assert bad["errors"]["pool_season_end"] == "invalid_month_day"

        # Valid wrapping window accepted
        good = await opt_flow.async_step_init(
            {
                "eval_interval_seconds": 10,
                "stale_timeout_seconds": 30,
                "heater_start_export_w": 1150.0,
                "pool_start_export_w": 1050.0,
                "import_shed_threshold_w": 300.0,
                "pool_season_enabled": True,
                "pool_season_start": "11-01",
                "pool_season_end": "03-31",
            }
        )
        assert good["type"] == "create_entry"
        assert good["data"]["pool_season_enabled"] is True
        assert good["data"]["pool_season_start"] == "11-01"
        assert good["data"]["pool_season_end"] == "03-31"


@pytest.mark.asyncio
async def test_coordinator_legacy_compat_and_snapshot_fields() -> None:
    """Coordinator defaults legacy configs to always-in-season and exposes season in snapshot."""
    if importlib.util.find_spec("homeassistant") is None:
        pytest.skip("homeassistant package not installed")

    from homeassistant.exceptions import ServiceValidationError

    from custom_components.hass_solar_orchestrator.coordinator import (
        SolarOrchestratorCoordinator,
    )

    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    entry = SimpleNamespace(entry_id="test_legacy", data={}, options={})
    coord = SolarOrchestratorCoordinator(hass, entry)
    coord._store = MagicMock()
    coord._store.async_load = AsyncMock(return_value={"mode": "auto_solar"})
    coord._store.async_save = AsyncMock()
    coord.async_request_refresh = AsyncMock()

    await coord.async_load_state()
    assert coord.config.pool_season_enabled is False

    snap_payload = coord.get_panel_snapshot()
    assert snap_payload["snapshot"]["pool_in_season"] is True
    assert snap_payload["plan"]["pool_in_season"] is True
    assert snap_payload["config"]["pool_season_enabled"] is False
    assert snap_payload["config"]["pool_season_start"] == "05-01"
    assert snap_payload["config"]["pool_season_end"] == "09-30"

    with pytest.raises(ServiceValidationError):
        await coord.async_save_settings({"pool_season_start": "99-99"})

    await coord.async_save_settings(
        {
            "pool_season_enabled": True,
            "pool_season_start": "11-01",
            "pool_season_end": "03-31",
        }
    )
    assert coord.config.pool_season_enabled is True
    assert coord.config.pool_season_start == "11-01"
    assert coord.config.pool_season_end == "03-31"


def test_panel_js_pool_season_and_telemetry_weather_status() -> None:
    """Verify panel renders out-of-season pool, cloud_fallback, and weather failure in es/en."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

class FakeEl {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.listeners = {};
    this.style = {};
    this.dataset = {};
    this.attrs = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.checked = false;
    this.textContent = "";
    this._innerHTML = "";
    this.id = "";
    this.className = "";
    this.classList = { toggle: () => {}, contains: () => false };
  }
  set innerHTML(v) {
    this._innerHTML = String(v);
    this._byId = new Map();
    const re = /<([a-z0-9-]+)\b([^>]*)>/gi;
    let m;
    while ((m = re.exec(this._innerHTML))) {
      const idM = /\bid="([^"]+)"/.exec(m[2]);
      if (idM) {
        const el = new FakeEl(m[1]);
        el.id = idM[1];
        const cfgM = /\bdata-cfg="([^"]+)"/.exec(m[2]);
        if (cfgM) el.dataset.cfg = cfgM[1];
        const typeM = /\btype="([^"]+)"/.exec(m[2]);
        if (typeM) el.type = typeM[1];
        this._byId.set(el.id, el);
      }
    }
  }
  get innerHTML() { return this._innerHTML; }
  getElementById(id) { return this._byId ? (this._byId.get(id) || null) : null; }
  querySelectorAll(sel) {
    return sel === "[data-cfg]" && this._byId
      ? [...this._byId.values()].filter((e) => e.dataset.cfg)
      : [];
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
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), {
  HTMLElement: FakeHTMLElement,
  customElements: { define: (n, c) => { defined[n] = c; } },
  window: {
    location: { hash: "" }, setInterval: () => 1, clearInterval: () => {},
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: (fn) => { fn(); return 1; }, cancelAnimationFrame: () => {},
    addEventListener: () => {}, removeEventListener: () => {},
  },
  document: {
    hidden: false, addEventListener: () => {}, removeEventListener: () => {},
    createElement: (t) => new FakeEl(t),
  },
  URL: { createObjectURL: () => "blob:1", revokeObjectURL: () => {} },
  Blob: class {},
  console,
});

const Panel = defined["solar-orchestrator-panel"];
const panel = new Panel();
panel._mountOnce();

const snap = {
  entry_id: "e1",
  mode: "auto_solar",
  load_modes: { pool: "active", heater: "active", ev: "active" },
  conflicts: [],
  snapshot: {
    solar_w: 1500, house_w: 500, grid_w: -1000, battery_w: 0, battery_soc: 80,
    pool_in_season: false,
    telemetry_status: "cloud_fallback",
    weather_fetch_failed: true,
    weather_failures: 3,
    weather_last_error: "TimeoutError",
    weather_last_error_at: "2026-11-26T10:15:00+00:00",
    weather_next_retry_at: "2026-11-26T10:35:00+00:00",
  },
  plan: {
    traffic_light: "warning",
    human_headline: "Excedente 1000 W",
    pool_in_season: false,
    telemetry_status: "cloud_fallback",
    alerts: [],
  },
  config: {
    pool_season_enabled: true,
    pool_season_start: "05-01",
    pool_season_end: "09-30",
  },
  blackbox: [],
};

// Spanish
panel._hass = { user: { is_admin: true }, language: "es" };
panel._snapshot = snap;
panel._patchDOM();
assert.strictEqual(
  panel.shadowRoot.getElementById("badge-pool-mode").textContent,
  "fuera de temporada"
);
assert.strictEqual(panel.shadowRoot.getElementById("val-pool-season").hidden, false);
assert.ok(panel.shadowRoot.getElementById("val-pool-season").textContent.includes("05-01"));
assert.strictEqual(panel.shadowRoot.getElementById("st-telemetry-status").hidden, false);
assert.ok(
  panel.shadowRoot.getElementById("st-telemetry-status").textContent.includes(
    "Datos de la nube: arranques bloqueados"
  )
);
assert.strictEqual(panel.shadowRoot.getElementById("st-weather-error").hidden, false);
assert.ok(
  panel.shadowRoot.getElementById("st-weather-error").textContent.includes("2026-11-26 10:15")
);
assert.ok(
  panel.shadowRoot.getElementById("st-weather-error").textContent.includes("2026-11-26 10:35")
);
assert.strictEqual(panel.shadowRoot.getElementById("inp-pool-season-enabled").checked, true);

// English
panel._hass = { user: { is_admin: true }, language: "en" };
panel._patchDOM();
assert.strictEqual(
  panel.shadowRoot.getElementById("badge-pool-mode").textContent,
  "out of season"
);
assert.ok(
  panel.shadowRoot.getElementById("st-telemetry-status").textContent.includes(
    "Cloud fallback: starts blocked"
  )
);
assert.ok(
  panel.shadowRoot.getElementById("st-weather-error").textContent.includes(
    "Weather fetch failing"
  )
);
"""
    res = subprocess.run([node, "-e", harness, str(PANEL_JS)], capture_output=True, text=True)
    assert res.returncode == 0, f"Node harness failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}"
