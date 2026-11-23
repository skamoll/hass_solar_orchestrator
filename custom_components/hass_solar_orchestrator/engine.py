"""Pure deterministic decision engine for Solar Excess Orchestrator."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

from .const import OperatingMode, TelemetrySource
from .models import (
    DispatchPlan,
    EnergySnapshot,
    LoadAction,
    OrchestratorConfig,
    OrchestratorState,
)

TRYDAN_MODE_MODULATED = 2  # FV + Minima Potencia
TRYDAN_MODE_SOLAR_ONLY = 3  # FV Exclusivo (autonomous safe fallback)


def compute_voltage(snapshot: EnergySnapshot, config: OrchestratorConfig) -> float:
    """Estimate real AC voltage from Trydan readings with safe nominal fallback."""
    if snapshot.ev_power_w > 1000.0 and snapshot.ev_current_a >= 6.0:
        est = snapshot.ev_power_w / snapshot.ev_current_a
        if 180.0 < est < 255.0:
            return est
    return config.nominal_voltage_v


def compute_usable_surplus_w(snapshot: EnergySnapshot, config: OrchestratorConfig) -> float:
    """Compute usable solar surplus (W) respecting house battery SoC floor.

    Positive grid_power_w means importing; negative means exporting.
    Negative battery_power_w means charging; positive means discharging.
    """
    if snapshot.grid_power_w is None:
        return 0.0

    export_w = -snapshot.grid_power_w
    bat_w = snapshot.battery_power_w if snapshot.battery_power_w is not None else 0.0
    soc = snapshot.battery_soc if snapshot.battery_soc is not None else 0.0

    battery_charge_reclaim_w = 0.0
    if bat_w < 0.0 and soc >= config.house_soc_floor:
        battery_charge_reclaim_w = -bat_w

    slack_w = config.battery_slack_w if soc >= (config.house_soc_floor + 5.0) else 0.0
    return export_w + battery_charge_reclaim_w + slack_w


def _effective_heater_draw_w(snapshot: EnergySnapshot, config: OrchestratorConfig) -> float:
    """Use measured heater power when available so thermostat-off (0W) is not double-counted."""
    if not snapshot.heater_on:
        return 0.0
    if snapshot.heater_power_w is not None:
        return max(0.0, snapshot.heater_power_w)
    return config.heater_nominal_w


def _is_local_fresh(snapshot: EnergySnapshot, config: OrchestratorConfig) -> bool:
    """Return True if local Modbus telemetry is present and fresh."""
    if (
        snapshot.telemetry_source != TelemetrySource.MODBUS
        or snapshot.grid_power_w is None
        or snapshot.solar_power_w is None
        or snapshot.local_sample_time is None
    ):
        return False
    age = (snapshot.now - snapshot.local_sample_time).total_seconds()
    return age <= config.stale_timeout_seconds


def _rollover_pool_daily(snapshot: EnergySnapshot, state: OrchestratorState) -> None:
    """Accumulate daily pool runtime and reset counters at local midnight."""
    day_key = snapshot.now.date().isoformat()
    if state.pool_last_day is None:
        state.pool_last_day = day_key
    elif state.pool_last_day != day_key:
        state.pool_last_day = day_key
        state.pool_minutes_today = 0.0
        state.pool_manual_blocked_today = False
        state.pool_cooldown_until = None

    if snapshot.pool_on and state.pool_last_tick is not None:
        dt_min = max(0.0, (snapshot.now - state.pool_last_tick).total_seconds() / 60.0)
        if dt_min <= 5.0:
            state.pool_minutes_today += dt_min
    state.pool_last_tick = snapshot.now


def evaluate(
    snapshot: EnergySnapshot,
    config: OrchestratorConfig,
    prev_state: OrchestratorState,
) -> tuple[DispatchPlan, OrchestratorState]:
    """Evaluate one telemetry tick and return (DispatchPlan, next OrchestratorState)."""
    state = replace(prev_state)
    _rollover_pool_daily(snapshot, state)
    reasons: list[str] = []
    actions: list[LoadAction] = []

    local_fresh = _is_local_fresh(snapshot, config)
    degraded = not local_fresh
    state.degraded = degraded

    usable_surplus_w = compute_usable_surplus_w(snapshot, config)
    voltage_v = compute_voltage(snapshot, config)
    ev_budget_w = usable_surplus_w + (snapshot.ev_power_w if snapshot.ev_charging else 0.0)

    # Track confirmed handoff state: if we ordered an aux load off, wait until it is off
    # AND at least one fresh local Modbus sample arrived afterwards before reallocating power.
    handoff_active = False
    if state.handoff_waiting_for_off is not None:
        target_is_off = (state.handoff_waiting_for_off == "pool" and not snapshot.pool_on) or (
            state.handoff_waiting_for_off == "heater" and not snapshot.heater_on
        )
        if not target_is_off:
            handoff_active = True
            reasons.append(f"handoff_waiting_off:{state.handoff_waiting_for_off}")
        elif state.handoff_off_confirmed_at is None:
            state.handoff_off_confirmed_at = snapshot.now
            handoff_active = True
            reasons.append(f"handoff_waiting_post_off_sample:{state.handoff_waiting_for_off}")
        elif (
            snapshot.local_sample_time is not None
            and snapshot.local_sample_time <= state.handoff_off_confirmed_at
        ):
            handoff_active = True
            reasons.append(f"handoff_waiting_fresh_sample:{state.handoff_waiting_for_off}")
        else:
            state.handoff_waiting_for_off = None
            state.handoff_off_confirmed_at = None

    # Default recommendations preserve current states unless changed below.
    rec_ev_amps = int(snapshot.ev_min_amps_a)
    rec_heater_on = snapshot.heater_on
    rec_pool_on = snapshot.pool_on

    def _emit(act: LoadAction) -> None:
        if config.mode != OperatingMode.OBSERVING:
            actions.append(act)

    def _ensure_trydan_safe(reason: str) -> None:
        if snapshot.ev_mode and "exclusivo" not in snapshot.ev_mode.lower():
            _emit(
                LoadAction(
                    target="ev_mode",
                    action="set",
                    value=TRYDAN_MODE_SOLAR_ONLY,
                    reason=reason,
                )
            )
            state.last_ev_command_at = snapshot.now
        if snapshot.ev_min_amps_a != config.ev_min_amps:
            _emit(
                LoadAction(
                    target="ev_min_amps",
                    action="set",
                    value=config.ev_min_amps,
                    reason=reason,
                )
            )

    def _shed_solar_aux(reason: str) -> None:
        nonlocal rec_heater_on, rec_pool_on
        if snapshot.pool_on and state.pool_solar_owned:
            rec_pool_on = False
            state.pool_solar_owned = False
            state.pool_cooldown_until = snapshot.now + timedelta(
                minutes=config.pool_cooldown_minutes
            )
            state.handoff_waiting_for_off = "pool"
            state.handoff_off_confirmed_at = None
            _emit(LoadAction(target="pool", action="turn_off", reason=reason))
        if snapshot.heater_on and state.heater_solar_owned:
            rec_heater_on = False
            state.heater_solar_owned = False
            state.handoff_waiting_for_off = "heater"
            state.handoff_off_confirmed_at = None
            _emit(LoadAction(target="heater", action="turn_off", reason=reason))

    # 1. PAUSED / HOUSE_PRIORITY / DEGRADED handling
    if config.mode == OperatingMode.PAUSED:
        reasons.append("mode_paused")
        _shed_solar_aux("paused")
        _ensure_trydan_safe("paused_safe_fallback")
        return (
            DispatchPlan(
                effective_mode=OperatingMode.PAUSED,
                degraded=degraded,
                telemetry_source=snapshot.telemetry_source,
                usable_surplus_w=round(usable_surplus_w, 1),
                ev_budget_w=round(ev_budget_w, 1),
                recommended_ev_amps=config.ev_min_amps,
                recommended_heater_on=rec_heater_on,
                recommended_pool_on=rec_pool_on,
                decision_summary="Paused: solar auxiliaries released, Trydan in FV Exclusivo.",
                reasons=tuple(reasons),
                actions=tuple(actions),
            ),
            state,
        )

    if config.mode == OperatingMode.HOUSE_PRIORITY:
        reasons.append("mode_house_priority")
        _shed_solar_aux("house_priority")
        _ensure_trydan_safe("house_priority_safe_fallback")
        return (
            DispatchPlan(
                effective_mode=OperatingMode.HOUSE_PRIORITY,
                degraded=degraded,
                telemetry_source=snapshot.telemetry_source,
                usable_surplus_w=round(usable_surplus_w, 1),
                ev_budget_w=round(ev_budget_w, 1),
                recommended_ev_amps=config.ev_min_amps,
                recommended_heater_on=rec_heater_on,
                recommended_pool_on=rec_pool_on,
                decision_summary=(
                    "House battery priority: solar auxiliaries off, Trydan FV Exclusivo."
                ),
                reasons=tuple(reasons),
                actions=tuple(actions),
            ),
            state,
        )

    if degraded:
        reasons.append("degraded_stale_or_missing_local_modbus")
        _ensure_trydan_safe("degraded_safe_fallback")
        if (
            snapshot.grid_power_w is not None
            and snapshot.grid_power_w > config.import_shed_threshold_w
        ):
            _shed_solar_aux("degraded_grid_import")
            reasons.append("degraded_shed_aux")
        return (
            DispatchPlan(
                effective_mode=config.mode,
                degraded=True,
                telemetry_source=snapshot.telemetry_source,
                usable_surplus_w=round(usable_surplus_w, 1),
                ev_budget_w=round(ev_budget_w, 1),
                recommended_ev_amps=config.ev_min_amps,
                recommended_heater_on=rec_heater_on,
                recommended_pool_on=rec_pool_on,
                decision_summary=(
                    f"Degraded ({snapshot.telemetry_source}): step-ups blocked, Trydan safe Mode 3."
                ),
                reasons=tuple(reasons),
                actions=tuple(actions),
            ),
            state,
        )

    # 2. FORCE_EV mode
    if config.mode == OperatingMode.FORCE_EV:
        reasons.append("mode_force_ev")
        ev_full = snapshot.ev_soc is not None and snapshot.ev_soc >= config.ev_target_soc
        if ev_full:
            reasons.append("ev_target_soc_reached")
            _ensure_trydan_safe("ev_target_soc_reached")
        elif snapshot.ev_connected:
            _shed_solar_aux("force_ev_priority")
            rec_ev_amps = config.ev_max_amps
            if snapshot.ev_mode and "minima" not in snapshot.ev_mode.lower():
                _emit(
                    LoadAction(
                        target="ev_mode",
                        action="set",
                        value=TRYDAN_MODE_MODULATED,
                        reason="force_ev",
                    )
                )
            if snapshot.ev_min_amps_a != config.ev_max_amps:
                _emit(
                    LoadAction(
                        target="ev_min_amps",
                        action="set",
                        value=config.ev_max_amps,
                        reason="force_ev",
                    )
                )
        return (
            DispatchPlan(
                effective_mode=OperatingMode.FORCE_EV,
                degraded=False,
                telemetry_source=snapshot.telemetry_source,
                usable_surplus_w=round(usable_surplus_w, 1),
                ev_budget_w=round(ev_budget_w, 1),
                recommended_ev_amps=rec_ev_amps,
                recommended_heater_on=rec_heater_on,
                recommended_pool_on=rec_pool_on,
                decision_summary=f"Force EV: target {rec_ev_amps} A (BYD {snapshot.ev_soc}%).",
                reasons=tuple(reasons),
                actions=tuple(actions),
            ),
            state,
        )

    # 3. EV control in AUTO_SOLAR (Trydan Mode 3 local loop) vs AUTO_EV_MODULATED (HA Mode 2)
    ev_wants_solar = snapshot.ev_connected and (
        snapshot.ev_soc is None or snapshot.ev_soc < config.ev_target_soc
    )
    ev_absorbing_all = (
        ev_wants_solar
        and snapshot.ev_charging
        and snapshot.ev_power_w < (config.ev_max_amps * voltage_v * 0.92)
    )

    if config.mode in (OperatingMode.AUTO_SOLAR, OperatingMode.OBSERVING):
        _ensure_trydan_safe("auto_solar_single_loop_mode_3")
        reasons.append("trydan_local_fv_exclusivo")
    elif config.mode == OperatingMode.AUTO_EV_MODULATED:
        in_modulated = bool(snapshot.ev_mode and "minima" in snapshot.ev_mode.lower())
        settle_ok = (
            state.last_ev_command_at is None
            or (snapshot.now - state.last_ev_command_at).total_seconds() >= config.settle_seconds
        )
        grid_import_w = max(0.0, snapshot.grid_power_w or 0.0)
        emergency_import = grid_import_w >= config.emergency_import_w

        if not ev_wants_solar:
            reasons.append("ev_not_connected_or_full")
            _ensure_trydan_safe("ev_idle_or_full")
        elif grid_import_w > config.import_shed_threshold_w and in_modulated:
            # Asymmetric safety reduction
            if settle_ok or emergency_import:
                drop_a = max(1, math.ceil(grid_import_w / voltage_v))
                next_a = snapshot.ev_min_amps_a - drop_a
                min_run_elapsed = (
                    state.ev_started_at is None
                    or (snapshot.now - state.ev_started_at).total_seconds()
                    >= config.ev_min_run_seconds
                )
                if next_a < config.ev_min_amps:
                    if min_run_elapsed or emergency_import:
                        rec_ev_amps = config.ev_min_amps
                        state.ev_stopped_at = snapshot.now
                        state.last_ev_command_at = snapshot.now
                        _emit(
                            LoadAction(
                                target="ev_min_amps",
                                action="set",
                                value=config.ev_min_amps,
                                reason="safety_drop_below_min",
                            )
                        )
                        _emit(
                            LoadAction(
                                target="ev_mode",
                                action="set",
                                value=TRYDAN_MODE_SOLAR_ONLY,
                                reason="safety_drop_below_min",
                            )
                        )
                        reasons.append("ev_safety_paused_to_mode_3")
                    else:
                        rec_ev_amps = config.ev_min_amps
                        reasons.append("ev_hold_min_run_6a")
                else:
                    rec_ev_amps = next_a
                    state.last_ev_command_at = snapshot.now
                    _emit(
                        LoadAction(
                            target="ev_min_amps",
                            action="set",
                            value=next_a,
                            reason="safety_import_reduction",
                        )
                    )
                    reasons.append(f"ev_safety_step_down:{next_a}A")
            else:
                reasons.append("ev_waiting_settle_before_drop")
        elif not in_modulated:
            # Check start condition (>= ev_start_amps * V + margin, plus min_stop_seconds)
            stop_elapsed = (
                state.ev_stopped_at is None
                or (snapshot.now - state.ev_stopped_at).total_seconds()
                >= config.ev_min_stop_seconds
            )
            start_threshold_w = config.ev_start_amps * voltage_v + config.step_up_margin_w
            if not handoff_active and stop_elapsed and ev_budget_w >= start_threshold_w:
                if state.ev_step_up_since is None:
                    state.ev_step_up_since = snapshot.now
                    reasons.append("ev_start_holding")
                elif (
                    snapshot.now - state.ev_step_up_since
                ).total_seconds() >= config.step_up_hold_seconds:
                    start_a = min(
                        config.ev_max_amps,
                        max(config.ev_min_amps, math.floor(ev_budget_w / voltage_v)),
                        8,
                    )
                    rec_ev_amps = start_a
                    state.ev_started_at = snapshot.now
                    state.last_ev_command_at = snapshot.now
                    state.ev_step_up_since = None
                    _emit(
                        LoadAction(
                            target="ev_min_amps",
                            action="set",
                            value=start_a,
                            reason="ev_start_surplus",
                        )
                    )
                    _emit(
                        LoadAction(
                            target="ev_mode",
                            action="set",
                            value=TRYDAN_MODE_MODULATED,
                            reason="ev_start_surplus",
                        )
                    )
                    reasons.append(f"ev_started:{start_a}A")
            else:
                state.ev_step_up_since = None
        else:
            # Already in modulated mode and not over-importing: evaluate step-up with deadband
            target_a = min(config.ev_max_amps, math.floor(ev_budget_w / voltage_v))
            step_a = min(2, target_a - snapshot.ev_min_amps_a)
            required_extra_w = step_a * voltage_v + config.step_up_margin_w if step_a > 0 else 0.0
            if (
                not handoff_active
                and settle_ok
                and step_a > 0
                and usable_surplus_w >= required_extra_w
            ):
                if state.ev_step_up_since is None:
                    state.ev_step_up_since = snapshot.now
                    reasons.append("ev_step_up_holding")
                elif (
                    snapshot.now - state.ev_step_up_since
                ).total_seconds() >= config.step_up_hold_seconds:
                    next_a = snapshot.ev_min_amps_a + step_a
                    rec_ev_amps = next_a
                    state.last_ev_command_at = snapshot.now
                    state.ev_step_up_since = None
                    _emit(
                        LoadAction(
                            target="ev_min_amps",
                            action="set",
                            value=next_a,
                            reason="ev_surplus_step_up",
                        )
                    )
                    reasons.append(f"ev_step_up:{next_a}A")
            else:
                state.ev_step_up_since = None

    # 4. Auxiliary Load 1: Water Heater (Termo)
    heater_temp_ok = (
        snapshot.heater_temp_c is not None and snapshot.heater_temp_c >= config.heater_target_temp_c
    )
    if snapshot.heater_on and state.heater_solar_owned:
        if heater_temp_ok:
            rec_heater_on = False
            state.heater_solar_owned = False
            state.handoff_waiting_for_off = "heater"
            state.handoff_off_confirmed_at = None
            _emit(LoadAction(target="heater", action="turn_off", reason="heater_target_temp"))
            reasons.append("heater_reached_target_temp")
        elif ev_absorbing_all:
            rec_heater_on = False
            state.heater_solar_owned = False
            state.handoff_waiting_for_off = "heater"
            state.handoff_off_confirmed_at = None
            _emit(LoadAction(target="heater", action="turn_off", reason="heater_yield_to_ev"))
            reasons.append("heater_yielded_to_ev")
    elif (
        config.heater_enabled
        and not snapshot.heater_on
        and not heater_temp_ok
        and not ev_absorbing_all
        and not handoff_active
    ):
        export_w = -(snapshot.grid_power_w or 0.0)
        if export_w >= config.heater_start_export_w:
            if state.heater_export_since is None:
                state.heater_export_since = snapshot.now
                reasons.append("heater_export_holding")
            elif (
                snapshot.now - state.heater_export_since
            ).total_seconds() >= config.heater_hold_seconds:
                rec_heater_on = True
                state.heater_solar_owned = True
                state.heater_export_since = None
                _emit(LoadAction(target="heater", action="turn_on", reason="heater_surplus"))
                reasons.append("heater_started")
        else:
            state.heater_export_since = None
    else:
        state.heater_export_since = None

    # 5. Auxiliary Load 2: Pool Pump (Depuradora)
    pool_cooldown_active = (
        state.pool_cooldown_until is not None and snapshot.now < state.pool_cooldown_until
    )
    pool_quota_exhausted = state.pool_minutes_today >= config.pool_daily_max_minutes
    grid_import_w = max(0.0, snapshot.grid_power_w or 0.0)
    bat_discharge_w = max(0.0, snapshot.battery_power_w or 0.0)

    if snapshot.pool_on:
        if pool_quota_exhausted:
            rec_pool_on = False
            state.pool_solar_owned = False
            _emit(LoadAction(target="pool", action="turn_off", reason="pool_daily_quota"))
            reasons.append("pool_daily_quota_reached")
        elif state.pool_solar_owned:
            if ev_wants_solar and snapshot.ev_charging:
                rec_pool_on = False
                state.pool_solar_owned = False
                state.pool_cooldown_until = snapshot.now + timedelta(
                    minutes=config.pool_cooldown_minutes
                )
                state.handoff_waiting_for_off = "pool"
                state.handoff_off_confirmed_at = None
                _emit(LoadAction(target="pool", action="turn_off", reason="pool_yield_to_ev"))
                reasons.append("pool_yielded_to_ev")
            elif rec_heater_on:
                rec_pool_on = False
                state.pool_solar_owned = False
                state.pool_cooldown_until = snapshot.now + timedelta(
                    minutes=config.pool_cooldown_minutes
                )
                state.handoff_waiting_for_off = "pool"
                state.handoff_off_confirmed_at = None
                _emit(LoadAction(target="pool", action="turn_off", reason="pool_yield_to_heater"))
                reasons.append("pool_yielded_to_heater")
            elif grid_import_w > 400.0 or bat_discharge_w > config.battery_discharge_shed_w:
                if state.aux_shed_since is None:
                    state.aux_shed_since = snapshot.now
                    reasons.append("pool_shed_holding")
                elif (
                    snapshot.now - state.aux_shed_since
                ).total_seconds() >= config.aux_shed_hold_seconds:
                    rec_pool_on = False
                    state.pool_solar_owned = False
                    state.pool_cooldown_until = snapshot.now + timedelta(
                        minutes=config.pool_cooldown_minutes
                    )
                    state.aux_shed_since = None
                    _emit(LoadAction(target="pool", action="turn_off", reason="pool_grid_or_bat"))
                    reasons.append("pool_shed_grid_or_bat")
            else:
                state.aux_shed_since = None
    else:
        state.aux_shed_since = None
        export_w = -(snapshot.grid_power_w or 0.0)
        can_start_pool = (
            config.pool_enabled
            and not state.pool_manual_blocked_today
            and not pool_cooldown_active
            and not pool_quota_exhausted
            and not snapshot.ev_charging
            and not rec_heater_on
            and (not config.heater_enabled or heater_temp_ok)
            and bat_discharge_w < config.battery_discharge_shed_w
            and not handoff_active
            and export_w >= config.pool_start_export_w
        )
        if can_start_pool:
            if state.pool_export_since is None:
                state.pool_export_since = snapshot.now
                reasons.append("pool_export_holding")
            elif (
                snapshot.now - state.pool_export_since
            ).total_seconds() >= config.pool_hold_seconds:
                rec_pool_on = True
                state.pool_solar_owned = True
                state.pool_export_since = None
                _emit(LoadAction(target="pool", action="turn_on", reason="pool_surplus"))
                reasons.append("pool_started")
        else:
            state.pool_export_since = None

    # Include effective heater draw in explanation if heater is on
    heater_draw = _effective_heater_draw_w(snapshot, config)
    mode_prefix = "Observing (dry-run)" if config.mode == OperatingMode.OBSERVING else config.mode
    summary = (
        f"{mode_prefix}: surplus {round(usable_surplus_w)} W | "
        f"EV {rec_ev_amps} A | "
        f"Heater {'on' if rec_heater_on else 'off'} ({round(heater_draw)} W) | "
        f"Pool {'on' if rec_pool_on else 'off'} "
        f"({int(state.pool_minutes_today)}/{config.pool_daily_max_minutes} min)"
    )

    return (
        DispatchPlan(
            effective_mode=config.mode,
            degraded=False,
            telemetry_source=snapshot.telemetry_source,
            usable_surplus_w=round(usable_surplus_w, 1),
            ev_budget_w=round(ev_budget_w, 1),
            recommended_ev_amps=rec_ev_amps,
            recommended_heater_on=rec_heater_on,
            recommended_pool_on=rec_pool_on,
            decision_summary=summary,
            reasons=tuple(reasons),
            actions=tuple(actions),
        ),
        state,
    )
