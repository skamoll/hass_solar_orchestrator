"""Unit tests for the pure-Python Solar Excess Orchestrator engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    state = OrchestratorState(pool_solar_owned=True, ev_step_up_since=_ts(-35))

    # EV needs pool's 250 W to step up -> pool turns off and sets handoff_waiting_for_off
    snap0 = _base_snapshot(
        0,
        grid_power_w=-200.0,
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
        grid_power_w=-450.0,
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

    # t=20: fresh local Modbus sample arrives -> handoff clears and EV steps up immediately
    snap2 = _base_snapshot(
        20,
        grid_power_w=-450.0,
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
    assert any(a.target == "ev_min_amps" and a.value == 7 for a in plan2.actions)


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


# --- Regression tests: energy optimization, priorities and fail-safe audit ---------------


def _actions_for(plan: object, target: str) -> list:
    return [a for a in plan.actions if a.target == target]  # type: ignore[attr-defined]


def test_stale_battery_reading_is_not_reclaimed_as_surplus() -> None:
    """A frozen battery-charging value must not be counted as usable surplus."""
    cfg = OrchestratorConfig(house_soc_floor=20, stale_timeout_seconds=30)
    snap = _base_snapshot(
        120,
        grid_power_w=-200.0,
        battery_power_w=-3000.0,
        battery_soc=60.0,
        battery_sample_time=_ts(0),
    )
    assert compute_usable_surplus_w(snap, cfg) == 200.0
    fresh = _base_snapshot(
        120,
        grid_power_w=-200.0,
        battery_power_w=-3000.0,
        battery_soc=60.0,
        battery_sample_time=_ts(115),
    )
    assert compute_usable_surplus_w(fresh, cfg) == 3200.0


def test_force_ev_caps_amps_by_icp_headroom_instead_of_max_amps() -> None:
    """FORCE_EV must not command 32 A (~7.1 kW) over a 5.75 kW contracted power."""
    cfg = OrchestratorConfig(mode=OperatingMode.FORCE_EV)
    snap = _base_snapshot(
        grid_power_w=1000.0,
        ev_connected=True,
        ev_mode="FV Exclusivo",
        ev_min_amps_a=6,
        ev_soc=50.0,
    )
    plan, _ = evaluate(snap, cfg, OrchestratorState())
    amps = [a.value for a in _actions_for(plan, "ev_min_amps")]
    # limit 5750-500 = 5250 W; room 4250 W minus 150 W step-up margin -> floor(4100/222) = 18 A
    assert amps == [18]
    assert plan.recommended_ev_amps == 18


def test_force_ev_steps_down_immediately_on_icp_overload() -> None:
    """Step-down under ICP overload ignores settle timers (safety)."""
    cfg = OrchestratorConfig(mode=OperatingMode.FORCE_EV)
    state = OrchestratorState(last_ev_command_at=_ts(-2))
    snap = _base_snapshot(
        grid_power_w=6000.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=3552.0,
        ev_current_a=16.0,
        ev_min_amps_a=16,
        ev_mode="FV + Minima Potencia",
        ev_soc=50.0,
    )
    plan, _ = evaluate(snap, cfg, state)
    # non-EV import 2448 W -> floor((5250 - 2448) / 222) = 12 A
    assert [a.value for a in _actions_for(plan, "ev_min_amps")] == [12]


def test_force_ev_pauses_to_mode_3_when_min_amps_do_not_fit_icp() -> None:
    """If even 6 A would exceed the ICP limit, FORCE_EV falls back to solar-only."""
    cfg = OrchestratorConfig(mode=OperatingMode.FORCE_EV)
    snap = _base_snapshot(
        grid_power_w=7000.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
        ev_soc=50.0,
    )
    plan, next_state = evaluate(snap, cfg, OrchestratorState())
    assert any(a.target == "ev_mode" and a.value == TRYDAN_MODE_SOLAR_ONLY for a in plan.actions)
    assert "force_ev_paused_icp_overload" in plan.reasons
    assert next_state.ev_stopped_at == snap.now


def _solar_heater_state(**kwargs: object) -> OrchestratorState:
    return OrchestratorState(
        heater_solar_owned=True,
        heater_last_changed_at=_ts(-3600),
        **kwargs,  # type: ignore[arg-type]
    )


def test_heater_sheds_after_sustained_grid_import() -> None:
    """A solar-started heater must not keep importing ~1 kW once surplus disappears."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, aux_shed_hold_seconds=600)
    common = {"grid_power_w": 800.0, "heater_on": True, "heater_temp_c": 40.0}
    plan0, state = evaluate(_base_snapshot(0, **common), cfg, _solar_heater_state())
    assert "heater_shed_holding" in plan0.reasons
    assert not _actions_for(plan0, "heater")

    plan1, state = evaluate(_base_snapshot(600, **common), cfg, state)
    offs = _actions_for(plan1, "heater")
    assert len(offs) == 1 and offs[0].action == "turn_off"
    assert offs[0].reason == "heater_grid_or_bat"
    assert state.heater_solar_owned is False


def test_heater_sheds_on_house_battery_discharge() -> None:
    """Heater draining the house battery counts as over budget too."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, aux_shed_hold_seconds=0)
    common = {
        "grid_power_w": 0.0,
        "battery_power_w": 900.0,
        "heater_on": True,
        "heater_temp_c": 40.0,
    }
    _, state = evaluate(_base_snapshot(0, **common), cfg, _solar_heater_state())
    plan, _ = evaluate(_base_snapshot(10, **common), cfg, state)
    assert any(a.reason == "heater_grid_or_bat" for a in _actions_for(plan, "heater"))


def test_heater_shed_defers_to_lower_priority_pool_first() -> None:
    """With default priority (ev > heater > pool) the pool is shed before the heater."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, aux_shed_hold_seconds=0)
    state = _solar_heater_state(pool_solar_owned=True, pool_last_changed_at=_ts(-3600))
    common = {"grid_power_w": 800.0, "heater_on": True, "heater_temp_c": 40.0, "pool_on": True}
    plan, _ = evaluate(_base_snapshot(0, **common), cfg, state)
    assert "heater_shed_deferred_lower_priority_on" in plan.reasons
    assert not _actions_for(plan, "heater")


def test_real_icp_overload_sheds_aux_immediately_despite_cycle_lock() -> None:
    """Measured import above contracted-margin cuts solar-owned aux at once."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR)
    state = OrchestratorState(heater_solar_owned=True, heater_last_changed_at=_ts(-30))
    snap = _base_snapshot(grid_power_w=5600.0, heater_on=True, heater_temp_c=40.0)
    plan, next_state = evaluate(snap, cfg, state)
    offs = _actions_for(plan, "heater")
    assert len(offs) == 1 and offs[0].reason == "icp_overload"
    assert next_state.heater_solar_owned is False


def test_safe_hold_sheds_solar_owned_aux_after_60s_degraded() -> None:
    """Persistent loss of telemetry releases solar-owned loads instead of running blind."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, stale_timeout_seconds=30)
    state = _solar_heater_state(degraded_since=_ts(0))
    snap = _base_snapshot(
        70, local_sample_time=_ts(0), grid_power_w=-500.0, heater_on=True, heater_temp_c=40.0
    )
    plan, _ = evaluate(snap, cfg, state)
    assert plan.degraded is True
    assert any(a.reason == "safe_hold_unverified_surplus" for a in _actions_for(plan, "heater"))


def test_shadow_simulation_never_claims_solar_ownership() -> None:
    """A simulated start must not later let an active mode switch off a manual load."""
    cfg = OrchestratorConfig(mode=OperatingMode.OBSERVING, heater_hold_seconds=0)
    common = {"grid_power_w": -3000.0, "heater_temp_c": 35.0}
    _, state = evaluate(_base_snapshot(0, **common), cfg, OrchestratorState())
    plan, state = evaluate(_base_snapshot(10, **common), cfg, state)
    assert any(a.target == "heater" and a.action == "turn_on" for a in plan.simulated_actions)
    assert plan.actions == ()
    assert state.heater_solar_owned is False

    # User switches the heater on by hand, then activates auto mode: hands off.
    cfg.mode = OperatingMode.AUTO_SOLAR
    plan2, _ = evaluate(
        _base_snapshot(20, grid_power_w=800.0, heater_on=True, heater_temp_c=60.0), cfg, state
    )
    assert not _actions_for(plan2, "heater")


def test_observing_load_does_not_create_phantom_handoff_lock() -> None:
    """An observing pool's simulated shed must not block real EV/heater starts."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        load_modes={"pool": "observing", "heater": "active", "ev": "active"},
    )
    state = OrchestratorState(pool_solar_owned=True, pool_last_changed_at=_ts(-3600))
    snap = _base_snapshot(
        grid_power_w=5600.0,
        pool_on=True,
        pool_power_w=250.0,
    )
    plan, next_state = evaluate(snap, cfg, state)
    assert any(a.target == "pool" and a.action == "turn_off" for a in plan.simulated_actions)
    assert not _actions_for(plan, "pool")
    assert next_state.handoff_waiting_for_off is None


def test_priority_order_controls_heater_yield_to_ev() -> None:
    """Heater yields to the EV only when the EV outranks it in priority_order."""
    snap = _base_snapshot(
        grid_power_w=-200.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
        heater_on=True,
        heater_temp_c=40.0,
    )
    default_cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED)
    state = _solar_heater_state(ev_step_up_since=_ts(-35))
    plan, _ = evaluate(snap, default_cfg, state)
    assert any(a.reason == "heater_yield_to_ev" for a in _actions_for(plan, "heater"))

    heater_first = OrchestratorConfig(
        mode=OperatingMode.AUTO_EV_MODULATED, priority_order=("house", "heater", "ev", "pool")
    )
    plan2, _ = evaluate(snap, heater_first, state)
    assert not _actions_for(plan2, "heater")


# --- Priority-aware allocation, shared headroom and shed/increase separation ------------


def _reasons_with(plan: object, prefix: str) -> list[str]:
    return [r for r in plan.reasons if r.startswith(prefix)]  # type: ignore[attr-defined]


def test_aux_vs_aux_default_order_heater_gets_contended_surplus() -> None:
    """Default order heater > pool: the heater claims 1300 W first; pool must not steal it."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR, heater_hold_seconds=0, pool_hold_seconds=0
    )
    common = {"grid_power_w": -1300.0, "heater_temp_c": 35.0}
    plan0, state = evaluate(_base_snapshot(0, **common), cfg, OrchestratorState())
    assert "heater_export_holding" in plan0.reasons
    assert "pool_waits_for_priority:heater" in plan0.reasons
    plan1, _ = evaluate(_base_snapshot(10, **common), cfg, state)
    assert [(a.target, a.action) for a in plan1.actions] == [("heater", "turn_on")]


def test_aux_vs_aux_custom_order_pool_first() -> None:
    """Custom order pool > heater: the pool starts first and the heater waits for it."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        heater_hold_seconds=0,
        pool_hold_seconds=0,
        priority_order=("house", "ev", "pool", "heater"),
    )
    common = {"grid_power_w": -1300.0, "heater_temp_c": 35.0}
    plan0, state = evaluate(_base_snapshot(0, **common), cfg, OrchestratorState())
    assert "pool_export_holding" in plan0.reasons
    assert "heater_waits_for_priority:pool" in plan0.reasons
    plan1, _ = evaluate(_base_snapshot(10, **common), cfg, state)
    # Pool takes 250 W -> 1050 W left < 1150 W heater threshold: heater stays off.
    assert [(a.target, a.action) for a in plan1.actions] == [("pool", "turn_on")]


def test_higher_priority_heater_preempts_running_pool_on_separate_tick() -> None:
    """Heater > pool: a solar-owned pool yields; heater starts only on a later tick."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR, heater_hold_seconds=0, pool_hold_seconds=0
    )
    state = OrchestratorState(pool_solar_owned=True, pool_last_changed_at=_ts(-3600))
    on = {"grid_power_w": -1000.0, "heater_temp_c": 35.0, "pool_on": True, "pool_power_w": 250.0}
    plan0, state = evaluate(_base_snapshot(0, **on), cfg, state)
    assert "heater_export_holding" in plan0.reasons
    assert not plan0.actions

    plan1, state = evaluate(_base_snapshot(10, **on), cfg, state)
    assert [(a.target, a.action, a.reason) for a in plan1.actions] == [
        ("pool", "turn_off", "pool_yield_to_heater")
    ]
    assert state.handoff_waiting_for_off == "pool"

    off = {"grid_power_w": -1250.0, "heater_temp_c": 35.0}
    evaluate_seq = [20, 30]  # post-off sample, fresh sample -> winner starts without 2nd hold
    plans = []
    for t in evaluate_seq:
        p, state = evaluate(_base_snapshot(t, **off), cfg, state)
        plans.append(p)
    assert not any(_actions_for(p, "pool") for p in plans)  # pool stays off (cooldown)
    assert any(a.action == "turn_on" for a in _actions_for(plans[-1], "heater"))


def test_ev_step_up_yields_to_higher_priority_heater() -> None:
    """Order heater > EV: the EV must not step up; it releases its floor for the heater."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_EV_MODULATED,
        heater_hold_seconds=0,
        priority_order=("house", "heater", "ev", "pool"),
    )
    ev = {
        "grid_power_w": -400.0,
        "heater_temp_c": 35.0,
        "ev_connected": True,
        "ev_charging": True,
        "ev_power_w": 2220.0,
        "ev_current_a": 10.0,
        "ev_min_amps_a": 10,
        "ev_mode": "FV + Minima Potencia",
    }
    plan0, state = evaluate(_base_snapshot(0, **ev), cfg, OrchestratorState())
    assert "heater_export_holding" in plan0.reasons
    assert "ev_waits_for_priority:heater" in plan0.reasons
    assert not plan0.actions

    plan1, state = evaluate(_base_snapshot(10, **ev), cfg, state)
    assert [(a.target, a.value, a.reason) for a in plan1.actions] == [
        ("ev_min_amps", 6, "ev_yield_to_heater")
    ]  # decrease only; no heater start in the same tick

    # Trydan now follows solar above a 6 A floor: 888 W reclaimable + 400 W export.
    ev_low = {**ev, "ev_min_amps_a": 6}
    plan2, _ = evaluate(_base_snapshot(20, **ev_low), cfg, state)
    assert [(a.target, a.action) for a in plan2.actions] == [("heater", "turn_on")]


def test_default_order_relays_wait_while_ev_contends() -> None:
    """Default order EV first: relays wait while EV holds, then only the remainder is used."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_EV_MODULATED, heater_hold_seconds=0, pool_hold_seconds=0
    )
    snap = {"grid_power_w": -1800.0, "heater_temp_c": 35.0, "ev_connected": True}
    plan0, state = evaluate(
        _base_snapshot(0, ev_mode="FV Exclusivo", **snap), cfg, OrchestratorState()
    )
    assert "ev_start_holding" in plan0.reasons
    assert "heater_waits_for_priority:ev" in plan0.reasons
    assert "pool_waits_for_priority:ev" in plan0.reasons
    plan1, _ = evaluate(_base_snapshot(35, ev_mode="FV Exclusivo", **snap), cfg, state)
    targets = {a.target for a in plan1.actions}
    assert targets == {"ev_mode", "ev_min_amps"}  # 1776 W granted to EV, 24 W left


def test_no_increase_in_same_tick_as_a_shed() -> None:
    """Heater reaching temperature (decrease) defers an otherwise-ready pool start."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, pool_hold_seconds=0)
    state = _solar_heater_state(pool_export_since=_ts(-600))
    snap = _base_snapshot(grid_power_w=-2000.0, heater_on=True, heater_temp_c=50.0)
    plan, _ = evaluate(snap, cfg, state)
    assert [(a.target, a.action) for a in plan.actions] == [("heater", "turn_off")]
    assert "pool_increase_deferred:decrease_this_tick" in plan.reasons


def test_force_ev_does_not_step_up_in_same_tick_as_aux_shed() -> None:
    cfg = OrchestratorConfig(mode=OperatingMode.FORCE_EV)
    snap = _base_snapshot(
        grid_power_w=0.0,
        heater_on=True,
        heater_temp_c=40.0,
        ev_connected=True,
        ev_mode="FV Exclusivo",
        ev_soc=50.0,
    )
    plan, _ = evaluate(snap, cfg, _solar_heater_state())
    assert [(a.target, a.action) for a in plan.actions] == [("heater", "turn_off")]
    assert "force_ev_deferred_after_shed" in plan.reasons


def test_common_headroom_limits_start_even_while_exporting() -> None:
    """Exporting never counts as ICP headroom: a start must fit under limit with margin."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        heater_hold_seconds=0,
        contracted_power_w=1500.0,
        icp_margin_w=500.0,
    )
    state = OrchestratorState(heater_export_since=_ts(-600))
    plan, _ = evaluate(_base_snapshot(grid_power_w=-2000.0, heater_temp_c=35.0), cfg, state)
    assert not _actions_for(plan, "heater")
    assert "heater_increase_deferred:icp_headroom" in plan.reasons


def test_common_headroom_is_shared_by_increases_in_one_tick() -> None:
    """Heater start consumes headroom; pool in the same tick sees only what is left."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        heater_hold_seconds=0,
        pool_hold_seconds=0,
        contracted_power_w=1800.0,
        icp_margin_w=500.0,
    )
    state = OrchestratorState(heater_export_since=_ts(-600), pool_export_since=_ts(-600))
    plan, _ = evaluate(_base_snapshot(grid_power_w=-3000.0, heater_temp_c=35.0), cfg, state)
    assert [(a.target, a.action) for a in plan.actions] == [("heater", "turn_on")]
    assert "pool_increase_deferred:icp_headroom" in plan.reasons


def test_force_ev_step_up_uses_common_headroom_while_importing() -> None:
    cfg = OrchestratorConfig(mode=OperatingMode.FORCE_EV)
    snap = _base_snapshot(
        grid_power_w=4000.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
        ev_soc=50.0,
    )
    plan, _ = evaluate(snap, cfg, OrchestratorState())
    # room 5250-4000 = 1250 W -> floor((1332 + 1250 - 150) / 222) = 10 A
    assert [a.value for a in _actions_for(plan, "ev_min_amps")] == [10]


def test_pool_shed_uses_configured_import_threshold() -> None:
    """Pool shedding follows import_shed_threshold_w (was hardcoded at 400 W)."""
    state = OrchestratorState(pool_solar_owned=True, pool_last_changed_at=_ts(-3600))
    snap = _base_snapshot(grid_power_w=350.0, pool_on=True, heater_temp_c=50.0)
    plan, _ = evaluate(snap, OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR), state)
    assert "pool_shed_holding" in plan.reasons
    lenient = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, import_shed_threshold_w=500.0)
    plan2, _ = evaluate(snap, lenient, state)
    assert "pool_shed_holding" not in plan2.reasons


def test_ev_shadow_commands_do_not_touch_real_ev_timers() -> None:
    """Simulated EV start in observing load mode leaves run/stop/settle timers untouched."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_EV_MODULATED,
        load_modes={"ev": "observing", "heater": "active", "pool": "active"},
    )
    snap = {"grid_power_w": -1800.0, "heater_temp_c": 50.0, "ev_connected": True}
    _, state = evaluate(_base_snapshot(0, ev_mode="FV Exclusivo", **snap), cfg, OrchestratorState())
    plan, state = evaluate(_base_snapshot(35, ev_mode="FV Exclusivo", **snap), cfg, state)
    assert any(a.reason == "ev_start_surplus" for a in plan.simulated_actions)
    assert not plan.actions
    assert state.ev_started_at is None
    assert state.last_ev_command_at is None


def test_passive_higher_priority_load_does_not_block_real_lower_load() -> None:
    """An observing heater simulates, but cannot reserve power against an active pool."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        heater_hold_seconds=0,
        pool_hold_seconds=0,
        load_modes={"heater": "observing", "pool": "active", "ev": "observing"},
    )
    common = {"grid_power_w": -1300.0, "heater_temp_c": 35.0}
    plan0, state = evaluate(_base_snapshot(0, **common), cfg, OrchestratorState())
    assert "pool_waits_for_priority:heater" not in plan0.reasons
    plan1, _ = evaluate(_base_snapshot(10, **common), cfg, state)
    assert [(a.target, a.action) for a in plan1.actions] == [("pool", "turn_on")]
    assert any(a.target == "heater" for a in plan1.simulated_actions)


def test_blocked_or_disabled_solar_owned_aux_turns_off_but_manual_untouched() -> None:
    """Solar-owned aux loads turn off when blocked or disabled; manual loads are untouched."""
    snap = _base_snapshot(grid_power_w=-2500.0, heater_on=True, pool_on=True, heater_temp_c=35.0)

    # Blocked solar-owned heater & pool -> both turn off and release ownership
    cfg_blocked = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        blocked_loads=("heater", "pool"),
    )
    solar_state = OrchestratorState(
        heater_solar_owned=True,
        pool_solar_owned=True,
        heater_last_changed_at=_ts(-10),  # even within min_on_seconds
        pool_last_changed_at=_ts(-10),
    )
    plan_b, next_b = evaluate(snap, cfg_blocked, solar_state)
    assert {(a.target, a.action) for a in plan_b.actions} == {
        ("heater", "turn_off"),
        ("pool", "turn_off"),
    }
    assert next_b.heater_solar_owned is False
    assert next_b.pool_solar_owned is False

    # Disabled solar-owned heater & pool -> both turn off and release ownership
    cfg_disabled = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        heater_enabled=False,
        pool_enabled=False,
    )
    plan_d, next_d = evaluate(snap, cfg_disabled, solar_state)
    assert {(a.target, a.action) for a in plan_d.actions} == {
        ("heater", "turn_off"),
        ("pool", "turn_off"),
    }
    assert next_d.heater_solar_owned is False
    assert next_d.pool_solar_owned is False

    # Manual (not solar-owned) heater & pool -> never touched even when blocked/disabled
    manual_state = OrchestratorState(heater_solar_owned=False, pool_solar_owned=False)
    plan_mb, _ = evaluate(snap, cfg_blocked, manual_state)
    plan_md, _ = evaluate(snap, cfg_disabled, manual_state)
    assert plan_mb.actions == ()
    assert plan_md.actions == ()


def test_blocked_or_disabled_aux_respects_observing_mode() -> None:
    """Shadow/observing mode never actuates or clears solar ownership."""
    snap = _base_snapshot(grid_power_w=-2500.0, heater_on=True, pool_on=True, heater_temp_c=35.0)
    cfg = OrchestratorConfig(
        mode=OperatingMode.OBSERVING,
        heater_enabled=False,
        blocked_loads=("pool",),
    )
    state = OrchestratorState(heater_solar_owned=True, pool_solar_owned=True)
    plan, next_state = evaluate(snap, cfg, state)
    assert plan.actions == ()
    assert {(a.target, a.action) for a in plan.simulated_actions} >= {
        ("heater", "turn_off"),
        ("pool", "turn_off"),
    }
    assert next_state.heater_solar_owned is True
    assert next_state.pool_solar_owned is True


def test_blocked_ev_in_force_ev_falls_back_to_safe_mode3() -> None:
    """Blocking EV in FORCE_EV forces Trydan back to Mode 3 and ev_min_amps."""
    cfg = OrchestratorConfig(mode=OperatingMode.FORCE_EV, blocked_loads=("ev",))
    snap = _base_snapshot(
        grid_power_w=-2000.0,
        ev_connected=True,
        ev_charging=True,
        ev_mode="FV + Minima Potencia",
        ev_min_amps_a=14,
    )
    plan, _ = evaluate(snap, cfg, OrchestratorState())
    assert (LoadAction_val := {(a.target, a.action, a.value) for a in plan.actions}) == {
        ("ev_mode", "set", TRYDAN_MODE_SOLAR_ONLY),
        ("ev_min_amps", "set", cfg.ev_min_amps),
    }
    assert LoadAction_val


def test_stale_ev_telemetry_blocks_start_stepup_and_reclaim() -> None:
    """Stale EV readings must not reclaim phantom EV power or drive starts/step-ups."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED, stale_timeout_seconds=30)

    # Stale EV sample while in Mode 2 charging: no step-up, no EV power reclaim
    stale_charging = _base_snapshot(
        45,
        grid_power_w=-2000.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=3500.0,
        ev_current_a=16.0,
        ev_min_amps_a=8,
        ev_mode="FV + Minima Potencia",
        ev_sample_time=_ts(0),
    )
    state = OrchestratorState(ev_step_up_since=_ts(0))
    plan, next_state = evaluate(stale_charging, cfg, state)
    assert plan.actions == ()
    assert "ev_telemetry_stale" in plan.reasons
    assert plan.ev_budget_w == plan.usable_surplus_w
    assert next_state.ev_step_up_since is None

    # Stale EV sample while in Mode 3: no start
    stale_idle = _base_snapshot(
        45,
        grid_power_w=-2500.0,
        ev_connected=True,
        ev_mode="FV Exclusivo",
        ev_sample_time=_ts(0),
    )
    plan_idle, _ = evaluate(stale_idle, cfg, OrchestratorState())
    assert plan_idle.actions == ()
    assert "ev_telemetry_stale" in plan_idle.reasons

    # FORCE_EV also freezes step-ups on stale EV telemetry
    force_cfg = OrchestratorConfig(mode=OperatingMode.FORCE_EV, stale_timeout_seconds=30)
    plan_force, _ = evaluate(stale_charging, force_cfg, OrchestratorState())
    assert plan_force.actions == ()
    assert "ev_telemetry_stale" in plan_force.reasons


def test_stale_ev_telemetry_safe_window_drops_to_min_amps_then_recovers() -> None:
    """Charging EV under persistent stale telemetry drops to ev_min_amps after safe window."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_EV_MODULATED,
        stale_timeout_seconds=30,
        ev_stale_hold_seconds=60,
        ev_min_amps=6,
    )
    common = {
        "grid_power_w": -1000.0,
        "ev_connected": True,
        "ev_charging": True,
        "ev_power_w": 3108.0,
        "ev_current_a": 14.0,
        "ev_min_amps_a": 14,
        "ev_mode": "FV + Minima Potencia",
        "ev_sample_time": _ts(0),
    }
    # t=45: stale (>30s), safe window starts -> holds without dropping yet
    plan0, state = evaluate(_base_snapshot(45, **common), cfg, OrchestratorState())
    assert plan0.actions == ()
    assert "ev_telemetry_stale" in plan0.reasons
    assert state.ev_stale_since == _ts(45)

    # t=105: 60s elapsed under stale telemetry -> drops to ev_min_amps (6 A)
    plan1, state = evaluate(_base_snapshot(105, **common), cfg, state)
    assert [(a.target, a.action, a.value) for a in plan1.actions] == [
        ("ev_min_amps", "set", 6),
    ]
    assert "ev_stale_drop_to_min" in plan1.reasons

    # Already at ev_min_amps (6 A) under stale telemetry -> holds safely, no redundant command
    common_min = {**common, "ev_min_amps_a": 6}
    plan2, state = evaluate(_base_snapshot(115, **common_min), cfg, state)
    assert plan2.actions == ()
    assert "ev_stale_safe_hold" in plan2.reasons

    # Fresh EV telemetry returns -> ev_stale_since resets
    fresh = {**common_min, "ev_sample_time": _ts(120)}
    plan3, state = evaluate(_base_snapshot(125, **fresh), cfg, state)
    assert state.ev_stale_since is None
    assert "ev_telemetry_stale" not in plan3.reasons


# --- Regression tests: priority EV preemption, FORCE_EV ICP shed, hold & shared budget ---


def test_ev_preempts_lower_priority_aux_via_priority_order() -> None:
    """EV preempts lowest-priority solar-owned aux first (pool before heater), then steps up."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED, ev_max_step_up_amps=1)
    state = OrchestratorState(
        pool_solar_owned=True,
        pool_last_changed_at=_ts(-3600),
        heater_solar_owned=True,
        heater_last_changed_at=_ts(-3600),
        ev_step_up_since=_ts(-35),
    )
    snap = _base_snapshot(
        0,
        grid_power_w=-200.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
        pool_on=True,
        pool_power_w=250.0,
        heater_on=True,
        heater_temp_c=40.0,
    )
    plan0, state = evaluate(snap, cfg, state)
    # 200 W export + 250 W pool = 450 W >= 372 W (+1 A): only pool yields, heater stays on
    assert [(a.target, a.action, a.reason) for a in plan0.actions] == [
        ("pool", "turn_off", "pool_yield_to_ev")
    ]
    assert not _actions_for(plan0, "heater")

    # After handoff clears, EV steps up to 7 A immediately without a second hold
    after = {
        "grid_power_w": -450.0,
        "ev_connected": True,
        "ev_charging": True,
        "ev_power_w": 1332.0,
        "ev_current_a": 6.0,
        "ev_min_amps_a": 6,
        "ev_mode": "FV + Minima Potencia",
        "pool_on": False,
        "heater_on": True,
        "heater_temp_c": 40.0,
    }
    _, state = evaluate(_base_snapshot(10, **after), cfg, state)
    plan2, _ = evaluate(_base_snapshot(20, **after), cfg, state)
    assert [(a.target, a.action, a.value) for a in plan2.actions] == [
        ("ev_min_amps", "set", 7),
    ]


def test_ev_does_not_preempt_pool_when_surplus_is_already_sufficient() -> None:
    """Removing the 2600 W heuristic: pool stays on if EV already has enough surplus."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED)
    state = OrchestratorState(pool_solar_owned=True, pool_last_changed_at=_ts(-3600))
    snap = _base_snapshot(
        0,
        grid_power_w=-1800.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
        pool_on=True,
        pool_power_w=250.0,
    )
    plan, _ = evaluate(snap, cfg, state)
    assert not _actions_for(plan, "pool")


def test_force_ev_sheds_only_needed_aux_in_reverse_priority_order() -> None:
    """FORCE_EV keeps aux on when ICP fits, and sheds reverse-priority only for the deficit."""
    base_state = OrchestratorState(
        pool_solar_owned=True,
        pool_last_changed_at=_ts(-3600),
        heater_solar_owned=True,
        heater_last_changed_at=_ts(-3600),
    )

    # 1) Plenty of ICP headroom: neither pool nor heater is shed
    plenty_cfg = OrchestratorConfig(
        mode=OperatingMode.FORCE_EV, contracted_power_w=10000.0, ev_max_amps=32
    )
    snap_plenty = _base_snapshot(
        grid_power_w=0.0,
        ev_connected=True,
        ev_mode="FV Exclusivo",
        ev_soc=50.0,
        pool_on=True,
        pool_power_w=250.0,
        heater_on=True,
        heater_temp_c=40.0,
    )
    plan_plenty, _ = evaluate(snap_plenty, plenty_cfg, base_state)
    assert not _actions_for(plan_plenty, "pool")
    assert not _actions_for(plan_plenty, "heater")
    assert any(
        a.target == "ev_mode" and a.value == TRYDAN_MODE_MODULATED for a in plan_plenty.actions
    )

    # 2) Small deficit (152 W): sheds only lowest-priority pool (250 W), keeps heater on
    tight_cfg = OrchestratorConfig(
        mode=OperatingMode.FORCE_EV, contracted_power_w=5750.0, icp_margin_w=500.0, ev_max_amps=16
    )
    snap_tight = _base_snapshot(
        grid_power_w=1700.0,
        ev_connected=True,
        ev_mode="FV Exclusivo",
        ev_soc=50.0,
        pool_on=True,
        pool_power_w=250.0,
        heater_on=True,
        heater_temp_c=40.0,
    )
    plan_tight, _ = evaluate(snap_tight, tight_cfg, base_state)
    assert [(a.target, a.action) for a in plan_tight.actions] == [("pool", "turn_off")]
    assert not _actions_for(plan_tight, "heater")


def test_preemption_winner_starts_without_second_full_hold_in_prod_timings() -> None:
    """With 300 s hold, a preemption winner starts as soon as handoff clears (no 2nd 300 s)."""
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_SOLAR, heater_hold_seconds=300)
    state = OrchestratorState(pool_solar_owned=True, pool_last_changed_at=_ts(-3600))
    on = {"grid_power_w": -1000.0, "heater_temp_c": 35.0, "pool_on": True, "pool_power_w": 250.0}

    _, state = evaluate(_base_snapshot(0, **on), cfg, state)
    plan_preempt, state = evaluate(_base_snapshot(300, **on), cfg, state)
    assert [(a.target, a.action) for a in plan_preempt.actions] == [("pool", "turn_off")]

    off = {"grid_power_w": -1250.0, "heater_temp_c": 35.0}
    _, state = evaluate(_base_snapshot(310, **off), cfg, state)
    plan_start, _ = evaluate(_base_snapshot(320, **off), cfg, state)
    assert [(a.target, a.action) for a in plan_start.actions] == [("heater", "turn_on")]


def test_config_fields_replace_magic_numbers() -> None:
    """EV start/step-up caps and advisory thresholds honour OrchestratorConfig fields."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_EV_MODULATED,
        ev_max_start_amps=10,
        ev_max_step_up_amps=4,
    )
    # Start cap at 10 A instead of 8 A
    state = OrchestratorState(ev_step_up_since=_ts(-35))
    snap_start = _base_snapshot(0, grid_power_w=-2500.0, ev_connected=True, ev_mode="FV Exclusivo")
    plan_start, _ = evaluate(snap_start, cfg, state)
    assert any(a.target == "ev_min_amps" and a.value == 10 for a in plan_start.actions)

    # Step-up cap at +4 A (6 -> 10 A) instead of +2 A
    snap_up = _base_snapshot(
        0,
        grid_power_w=-1200.0,
        ev_connected=True,
        ev_charging=True,
        ev_power_w=1332.0,
        ev_current_a=6.0,
        ev_min_amps_a=6,
        ev_mode="FV + Minima Potencia",
    )
    plan_up, _ = evaluate(snap_up, cfg, state)
    assert any(a.target == "ev_min_amps" and a.value == 10 for a in plan_up.actions)


def test_aux_and_ev_share_battery_charge_reclaim_budget() -> None:
    """Heater uses the same usable_surplus_w (including battery-charge reclaim) as EV."""
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        house_soc_floor=20.0,
        heater_hold_seconds=0,
    )
    # Battery charging at -1500 W with SoC 60% >= 20% floor, grid_power_w = 0 W
    charging_high_soc = _base_snapshot(
        grid_power_w=0.0,
        battery_power_w=-1500.0,
        battery_soc=60.0,
        heater_temp_c=35.0,
    )
    _, state = evaluate(charging_high_soc, cfg, OrchestratorState())
    plan, _ = evaluate(
        _base_snapshot(
            10, grid_power_w=0.0, battery_power_w=-1500.0, battery_soc=60.0, heater_temp_c=35.0
        ),
        cfg,
        state,
    )
    assert [(a.target, a.action) for a in plan.actions] == [("heater", "turn_on")]

    # Battery SoC 15% < 20% floor: reclaim is blocked, heater stays off
    low_soc = _base_snapshot(
        grid_power_w=0.0,
        battery_power_w=-1500.0,
        battery_soc=15.0,
        heater_temp_c=35.0,
    )
    _, state_low = evaluate(low_soc, cfg, OrchestratorState())
    plan_low, _ = evaluate(
        _base_snapshot(
            10, grid_power_w=0.0, battery_power_w=-1500.0, battery_soc=15.0, heater_temp_c=35.0
        ),
        cfg,
        state_low,
    )
    assert plan_low.actions == ()
