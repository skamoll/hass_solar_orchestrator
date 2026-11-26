"""Unit tests for the pure-Python Solar Excess Orchestrator engine."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import UTC, datetime, timedelta
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

from custom_components.hass_solar_orchestrator.const import OperatingMode, TelemetrySource
from custom_components.hass_solar_orchestrator.engine import (
    TRYDAN_MODE_MODULATED,
    TRYDAN_MODE_SOLAR_ONLY,
    compute_usable_surplus_w,
    evaluate,
)
from custom_components.hass_solar_orchestrator.models import (
    EnergySnapshot,
    OrchestratorConfig,
    OrchestratorState,
)


def _ts(offset_s: int = 0) -> datetime:
    return datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_s)


def _base_snapshot(offset_s: int = 0, **kwargs: object) -> EnergySnapshot:
    now = _ts(offset_s)
    defaults = {
        "now": now,
        "solar_power_w": 3500.0,
        "load_power_w": 800.0,
        "grid_power_w": -2700.0,
        "battery_power_w": 0.0,
        "battery_soc": 80.0,
        "local_sample_time": now,
        "telemetry_source": TelemetrySource.MODBUS,
    }
    defaults.update(kwargs)
    return EnergySnapshot(**defaults)  # type: ignore[arg-type]


def test_usable_surplus_respects_battery_soc_floor() -> None:
    """Reclaim battery charging power only when battery SoC >= house_soc_floor."""
    cfg = OrchestratorConfig(house_soc_floor=30)
    low_soc = _base_snapshot(grid_power_w=0.0, battery_power_w=-2000.0, battery_soc=25.0)
    assert compute_usable_surplus_w(low_soc, cfg) == 0.0

    high_soc = _base_snapshot(grid_power_w=-200.0, battery_power_w=-2000.0, battery_soc=50.0)
    assert compute_usable_surplus_w(high_soc, cfg) == 2200.0


def test_observing_mode_computes_plan_without_actions() -> None:
    """OBSERVING (dry-run) must never emit actuator actions."""
    cfg = OrchestratorConfig(mode=OperatingMode.OBSERVING)
    snap = _base_snapshot(grid_power_w=-2500.0, ev_connected=True, ev_mode="FV + Minima Potencia")
    plan, _ = evaluate(snap, cfg, OrchestratorState())
    assert plan.effective_mode == OperatingMode.OBSERVING
    assert plan.usable_surplus_w == 2500.0
    assert plan.actions == ()


def test_degraded_mode_blocks_stepups_and_enforces_safe_mode3() -> None:
    """Stale local Modbus > 30s enters DEGRADED, blocks starts, and forces Trydan Mode 3."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED, stale_timeout_seconds=30)
    stale_snap = _base_snapshot(
        offset_s=45,
        local_sample_time=_ts(0),
        grid_power_w=450.0,
        ev_connected=True,
        ev_mode="FV + Minima Potencia",
        ev_min_amps_a=12,
        heater_on=True,
    )
    state = OrchestratorState(heater_solar_owned=True)
    plan, next_state = evaluate(stale_snap, cfg, state)
    assert plan.degraded is True
    assert next_state.degraded is True
    assert any(a.target == "ev_mode" and a.value == TRYDAN_MODE_SOLAR_ONLY for a in plan.actions)
    assert any(a.target == "heater" and a.action == "turn_off" for a in plan.actions)


def test_ev_modulated_hysteresis_settle_and_safety_drop() -> None:
    """EV start holds for 30s, steps up only with deadband+settle, and drops on >300W import."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED)
    state = OrchestratorState()

    # t=0: enough surplus to start (>= 7*222 + 150 = 1704 W), enters holding
    snap0 = _base_snapshot(0, grid_power_w=-1800.0, ev_connected=True, ev_mode="FV Exclusivo")
    plan0, state = evaluate(snap0, cfg, state)
    assert plan0.actions == ()
    assert "ev_start_holding" in plan0.reasons

    # t=35: hold satisfied -> starts Mode 2 at <= 8A
    snap1 = _base_snapshot(35, grid_power_w=-1800.0, ev_connected=True, ev_mode="FV Exclusivo")
    plan1, state = evaluate(snap1, cfg, state)
    assert any(a.target == "ev_mode" and a.value == TRYDAN_MODE_MODULATED for a in plan1.actions)
    assert any(a.target == "ev_min_amps" and a.value == 8 for a in plan1.actions)

    # t=40: within settle_seconds (10s), no new command allowed even on 350W import
    snap2 = _base_snapshot(
        40,
        grid_power_w=350.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1776.0,
        ev_current_a=8.0,
        ev_min_amps_a=8,
        ev_mode="FV + Minima Potencia",
    )
    plan2, state = evaluate(snap2, cfg, state)
    assert "ev_waiting_settle_before_drop" in plan2.reasons
    assert plan2.actions == ()

    # t=50: settle elapsed -> safety step-down executes
    snap3 = _base_snapshot(
        50,
        grid_power_w=350.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1776.0,
        ev_current_a=8.0,
        ev_min_amps_a=8,
        ev_mode="FV + Minima Potencia",
    )
    plan3, _ = evaluate(snap3, cfg, state)
    assert any(a.target == "ev_min_amps" and a.value == 6 for a in plan3.actions)


def test_ev_anti_chattering_min_run_and_min_stop() -> None:
    """EV holds 6A until min_run_seconds (180s) unless emergency import (>=1000W)."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED)
    state = OrchestratorState(ev_started_at=_ts(0), last_ev_command_at=_ts(0))

    # t=60 (< 180s): 400W import at 6A holds 6A instead of pausing
    snap = _base_snapshot(
        60,
        grid_power_w=400.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
    )
    plan, state = evaluate(snap, cfg, state)
    assert "ev_hold_min_run_6a" in plan.reasons
    assert plan.actions == ()

    # t=200 (>= 180s): pauses cleanly to Mode 3
    snap2 = _base_snapshot(
        200,
        grid_power_w=400.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
    )
    plan2, _ = evaluate(snap2, cfg, state)
    assert any(a.target == "ev_mode" and a.value == TRYDAN_MODE_SOLAR_ONLY for a in plan2.actions)


def test_confirmed_handoff_waits_for_off_and_fresh_sample() -> None:
    """Power handoff from pool to EV waits for pool off + post-off local sample."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED)
    state = OrchestratorState(pool_solar_owned=True)

    # EV starts charging while pool is on -> pool turns off and sets handoff_waiting_for_off
    snap0 = _base_snapshot(
        0,
        grid_power_w=-1800.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
        pool_on=True,
    )
    plan0, state = evaluate(snap0, cfg, state)
    assert any(a.target == "pool" and a.action == "turn_off" for a in plan0.actions)
    assert state.handoff_waiting_for_off == "pool"

    # t=10: pool confirmed off, first post-off sample records handoff_off_confirmed_at
    snap1 = _base_snapshot(
        10,
        grid_power_w=-2050.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
        pool_on=False,
    )
    plan1, state = evaluate(snap1, cfg, state)
    assert "handoff_waiting_post_off_sample:pool" in plan1.reasons

    # t=20: fresh local Modbus sample arrives after handoff_off_confirmed_at -> handoff clears
    snap2 = _base_snapshot(
        20,
        grid_power_w=-2050.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
        pool_on=False,
    )
    plan2, state = evaluate(snap2, cfg, state)
    assert state.handoff_waiting_for_off is None
    assert "ev_step_up_holding" in plan2.reasons


def test_pool_daily_quota_cooldown_and_midnight_reset() -> None:
    """Pool stops at 360 min/day and resets cleanly on the next day."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, pool_daily_max_minutes=360)
    state = OrchestratorState(
        pool_solar_owned=True,
        pool_minutes_today=359.5,
        pool_last_tick=_ts(0),
        pool_last_day="2026-11-23",
    )
    snap = _base_snapshot(60, pool_on=True, heater_temp_c=50.0)
    plan, state = evaluate(snap, cfg, state)
    assert any(a.target == "pool" and a.action == "turn_off" for a in plan.actions)
    assert "pool_daily_quota_reached" in plan.reasons

    next_day = _ts(0) + timedelta(days=1)
    snap_next = _base_snapshot(0, now=next_day, local_sample_time=next_day, heater_temp_c=50.0)
    _, state_next = evaluate(snap_next, cfg, state)
    assert state_next.pool_minutes_today == 0.0


def test_heater_respects_manual_override_and_measured_power() -> None:
    """Manual heater (heater_solar_owned=False) is never turned off by EV priority."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR)
    state = OrchestratorState(heater_solar_owned=False)
    snap = _base_snapshot(
        0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=2500.0,
        heater_on=True,
        heater_power_w=0.0,
        heater_temp_c=42.0,
    )
    plan, _ = evaluate(snap, cfg, state)
    assert not any(a.target == "heater" and a.action == "turn_off" for a in plan.actions)
    assert "(0 W)" in plan.decision_summary


def test_grid_import_cost_export_revenue_and_net_cost() -> None:
    """Grid import cost, export revenue, and net cost integrate cleanly and reset daily."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.OBSERVING,
        tariff_mode="manual_20td",
        tariff_p1_eur=0.20,
        tariff_p2_eur=0.15,
        tariff_p3_eur=0.10,
        export_comp_eur=0.08,
    )
    # _ts(0) is 2026-11-23 12:00:00 (Monday, P1)
    state = OrchestratorState(
        savings_last_tick=_ts(0), savings_last_day="2026-11-23", savings_last_month="2026-11"
    )
    snap_import = _base_snapshot(600, grid_power_w=2000.0, solar_power_w=0.0)
    plan1, state = evaluate(snap_import, cfg, state)
    # 2 kW * 1/6 h * 0.20 EUR/kWh = 0.0667 EUR
    assert plan1.grid_import_cost_today_eur == 0.07
    assert plan1.grid_export_revenue_today_eur == 0.0
    assert plan1.net_cost_today_eur == 0.07

    snap_export = _base_snapshot(1200, grid_power_w=-3000.0, solar_power_w=3500.0)
    plan2, state = evaluate(snap_export, cfg, state)
    # + 3 kW * 1/6 h * 0.08 EUR/kWh = 0.04 EUR
    assert plan2.grid_import_cost_today_eur == 0.07
    assert plan2.grid_export_revenue_today_eur == 0.04
    assert plan2.net_cost_today_eur == 0.03
    assert plan2.net_cost_month_eur == 0.03

    next_day = _ts(0) + timedelta(days=1)
    snap_next = _base_snapshot(0, now=next_day, local_sample_time=next_day, grid_power_w=0.0)
    plan_next, _ = evaluate(snap_next, cfg, state)
    assert plan_next.grid_import_cost_today_eur == 0.0
    assert plan_next.grid_export_revenue_today_eur == 0.0
    assert plan_next.net_cost_today_eur == 0.0
    assert plan_next.grid_import_cost_month_eur == 0.07
    assert plan_next.grid_export_revenue_month_eur == 0.04


def test_grid_costs_none_in_conservative_tariff_mode() -> None:
    """Conservative tariff mode omits monetary figures instead of inventing them."""
    cfg = OrchestratorConfig(mode=OperatingMode.OBSERVING, tariff_mode="conservative")
    state = OrchestratorState(
        savings_last_tick=_ts(0), savings_last_day="2026-11-23", savings_last_month="2026-11"
    )
    snap = _base_snapshot(60, grid_power_w=1500.0)
    plan, _ = evaluate(snap, cfg, state)
    assert plan.grid_import_cost_today_eur is None
    assert plan.grid_export_revenue_today_eur is None
    assert plan.net_cost_today_eur is None
