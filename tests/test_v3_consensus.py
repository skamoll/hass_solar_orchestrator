"""Unit tests for v0.3.0 Consejo de Sabios unanimous consensus (V3-M01..M13)."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

if importlib.util.find_spec("homeassistant") is None:
    _pkg = types.ModuleType("custom_components.hass_solar_orchestrator")
    _pkg.__path__ = [
        str(
            Path(__file__).resolve().parent.parent / "custom_components" / "hass_solar_orchestrator"
        )
    ]
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules["custom_components.hass_solar_orchestrator"] = _pkg

from custom_components.hass_solar_orchestrator.const import (
    DEFAULT_HEATER_NOMINAL_W,
    OperatingMode,
    TelemetrySource,
)
from custom_components.hass_solar_orchestrator.engine import evaluate
from custom_components.hass_solar_orchestrator.models import (
    EnergySnapshot,
    OrchestratorConfig,
    OrchestratorState,
)


def _now() -> datetime:
    return datetime(2026, 11, 23, 13, 0, 0)


def test_v3_m01_termo_1054w_and_thermostat_idle() -> None:
    """V3-M01 + Claude R10: 1054W default, <100W fresh enters idle; unavailable != idle."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR)
    assert cfg.heater_nominal_w == pytest.approx(DEFAULT_HEATER_NOMINAL_W)
    assert cfg.heater_nominal_w == pytest.approx(1054.0)

    t0 = _now()
    # 1. Active heating (>800 W)
    _, st = evaluate(
        EnergySnapshot(
            now=t0,
            solar_power_w=3000.0,
            load_power_w=1200.0,
            grid_power_w=-1800.0,
            local_sample_time=t0,
            telemetry_source=TelemetrySource.MODBUS,
            heater_on=True,
            heater_power_w=1054.0,
            heater_power_sample_time=t0,
        ),
        cfg,
        OrchestratorState(),
    )
    assert st.heater_seen_active_draw is True
    assert st.heater_thermostat_idle is False

    # 2. Thermostat opens (<100 W) at t0+60s -> not yet 300s
    _, st = evaluate(
        EnergySnapshot(
            now=t0 + timedelta(seconds=60),
            solar_power_w=3000.0,
            load_power_w=500.0,
            grid_power_w=-2500.0,
            local_sample_time=t0 + timedelta(seconds=60),
            telemetry_source=TelemetrySource.MODBUS,
            heater_on=True,
            heater_power_w=2.0,
            heater_power_sample_time=t0 + timedelta(seconds=60),
        ),
        cfg,
        st,
    )
    assert st.heater_thermostat_idle is False

    # 3. Still <100 W at t0+365s -> enters thermostat idle
    plan, st = evaluate(
        EnergySnapshot(
            now=t0 + timedelta(seconds=365),
            solar_power_w=3000.0,
            load_power_w=500.0,
            grid_power_w=-2500.0,
            local_sample_time=t0 + timedelta(seconds=365),
            telemetry_source=TelemetrySource.MODBUS,
            heater_on=True,
            heater_power_w=2.0,
            heater_power_sample_time=t0 + timedelta(seconds=365),
        ),
        cfg,
        st,
    )
    assert st.heater_thermostat_idle is True
    assert plan.heater_thermostat_idle is True

    # 4. Unavailable (None) NEVER counts as idle
    plan_none, st_none = evaluate(
        EnergySnapshot(
            now=t0 + timedelta(seconds=400),
            solar_power_w=3000.0,
            load_power_w=500.0,
            grid_power_w=-2500.0,
            local_sample_time=t0 + timedelta(seconds=400),
            telemetry_source=TelemetrySource.MODBUS,
            heater_on=True,
            heater_power_w=None,
        ),
        cfg,
        st,
    )
    assert st_none.heater_thermostat_idle is False
    assert plan_none.heater_thermostat_idle is False


def test_v3_m02_shower_pattern_veto_and_90min_cap() -> None:
    """V3-M02: Abrupt temp drop + flow blocks opt turn_off, capped at 90 min, yields to safety."""
    t0 = _now()
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, heater_target_temp_c=45.0)
    st = OrchestratorState(heater_solar_owned=True)

    # Initial reading at 46 °C (above target)
    _, st = evaluate(
        EnergySnapshot(
            now=t0,
            solar_power_w=2000.0,
            load_power_w=1200.0,
            grid_power_w=-800.0,
            local_sample_time=t0,
            telemetry_source=TelemetrySource.MODBUS,
            heater_on=True,
            heater_temp_c=46.0,
            heater_flow_l_min=4.0,
        ),
        cfg,
        st,
    )

    # 2 min later: abrupt -2.0 °C drop -> shower_veto_active blocks turn_off!
    plan, st = evaluate(
        EnergySnapshot(
            now=t0 + timedelta(minutes=2),
            solar_power_w=2000.0,
            load_power_w=1200.0,
            grid_power_w=-800.0,
            local_sample_time=t0 + timedelta(minutes=2),
            telemetry_source=TelemetrySource.MODBUS,
            heater_on=True,
            heater_temp_c=44.0,
            heater_flow_l_min=4.0,
        ),
        cfg,
        st,
    )
    assert plan.shower_veto_active is True
    assert not any(a.target == "heater" and a.action == "turn_off" for a in plan.actions)

    # 91 min later: hard cap expired even if sensor stays stuck
    plan_end, st_end = evaluate(
        EnergySnapshot(
            now=t0 + timedelta(minutes=91),
            solar_power_w=2000.0,
            load_power_w=1200.0,
            grid_power_w=-800.0,
            local_sample_time=t0 + timedelta(minutes=91),
            telemetry_source=TelemetrySource.MODBUS,
            heater_on=True,
            heater_temp_c=46.0,
            heater_flow_l_min=0.0,
        ),
        cfg,
        st,
    )
    assert plan_end.shower_veto_active is False


def test_v3_m04_m05_byd_unplugged_and_storm_alert() -> None:
    """V3-M04 & V3-M05: BYD at home unplugged alert & multi-day storm alert."""
    t0 = _now()
    cfg = OrchestratorConfig(mode=OperatingMode.OBSERVING)
    forecast = (
        {"condition": "rainy", "temperature": 15},
        {"condition": "pouring", "temperature": 14},
        {"condition": "sunny", "temperature": 19},
    )
    # Arrival at t0
    _, st = evaluate(
        EnergySnapshot(
            now=t0,
            solar_power_w=3000.0,
            load_power_w=500.0,
            grid_power_w=-2500.0,
            local_sample_time=t0,
            telemetry_source=TelemetrySource.MODBUS,
            ev_at_home=True,
            ev_connected=False,
            ev_soc=60.0,
            weather_daily_forecast=forecast,
        ),
        cfg,
        OrchestratorState(),
    )
    # 11 min later at home with surplus >= 1400 W & SoC < 85%
    plan, _ = evaluate(
        EnergySnapshot(
            now=t0 + timedelta(minutes=11),
            solar_power_w=3000.0,
            load_power_w=500.0,
            grid_power_w=-2500.0,
            local_sample_time=t0 + timedelta(minutes=11),
            telemetry_source=TelemetrySource.MODBUS,
            ev_at_home=True,
            ev_connected=False,
            ev_soc=60.0,
            weather_daily_forecast=forecast,
        ),
        cfg,
        st,
    )
    assert plan.byd_unplugged_surplus_alert is True
    assert plan.storm_alert is True


def test_v3_m10_granular_load_modes_gate() -> None:
    """V3-M10: Heater active + Pool observing emits only heater actions, zero pool writes."""
    t0 = _now()
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        heater_hold_seconds=0,
        pool_hold_seconds=0,
        load_modes={"heater": "active", "pool": "observing", "ev": "observing"},
    )
    _, st = evaluate(
        EnergySnapshot(
            now=t0,
            solar_power_w=4000.0,
            load_power_w=500.0,
            grid_power_w=-3500.0,
            local_sample_time=t0,
            telemetry_source=TelemetrySource.MODBUS,
            heater_temp_c=35.0,
        ),
        cfg,
        OrchestratorState(),
    )
    plan, _ = evaluate(
        EnergySnapshot(
            now=t0 + timedelta(seconds=1),
            solar_power_w=4000.0,
            load_power_w=500.0,
            grid_power_w=-3500.0,
            local_sample_time=t0 + timedelta(seconds=1),
            telemetry_source=TelemetrySource.MODBUS,
            heater_temp_c=35.0,
        ),
        cfg,
        st,
    )
    assert any(a.target == "heater" and a.action == "turn_on" for a in plan.actions)
    assert not any(a.target == "pool" for a in plan.actions)
