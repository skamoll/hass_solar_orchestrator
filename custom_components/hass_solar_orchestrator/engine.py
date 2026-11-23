"""Pure deterministic decision engine for Solar Excess Orchestrator."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import datetime, timedelta

from .const import (
    DEFAULT_BATTERY_DISCHARGE_SHED_W,
    FORBIDDEN_ENTITY_PATTERNS,
    GlobalTrafficLight,
    OperatingMode,
    ProfileName,
    SafetyState,
    TelemetrySource,
)
from .models import (
    AlertItem,
    DispatchPlan,
    EnergySnapshot,
    EvCommitmentStatus,
    FlexLoadStatus,
    LoadAction,
    OrchestratorConfig,
    OrchestratorState,
)

TRYDAN_MODE_MODULATED = 2  # FV + Minima Potencia
TRYDAN_MODE_SOLAR_ONLY = 3  # FV Exclusivo (autonomous safe fallback)
_FORBIDDEN_REGEXES = tuple(re.compile(p, re.IGNORECASE) for p in FORBIDDEN_ENTITY_PATTERNS)


class SecurityDenylistViolation(Exception):
    """Raised if an action ever targets a forbidden critical entity."""


def is_forbidden_entity(entity_id: str) -> bool:
    """Return True if entity_id matches the immutable anti-cut Denylist."""
    return any(rx.match(entity_id) for rx in _FORBIDDEN_REGEXES)


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
    """Return True if local Modbus telemetry is present, valid, and fresh."""
    if (
        snapshot.critical_sensor_invalid
        or snapshot.telemetry_source != TelemetrySource.MODBUS
        or snapshot.grid_power_w is None
        or snapshot.solar_power_w is None
        or snapshot.local_sample_time is None
    ):
        return False
    age = (snapshot.now - snapshot.local_sample_time).total_seconds()
    return 0.0 <= age <= config.stale_timeout_seconds


def _sample_age_s(now: datetime, sample_time: datetime | None) -> int | None:
    if sample_time is None:
        return None
    return max(0, int((now - sample_time).total_seconds()))


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


def _effective_tariff_price(snapshot: EnergySnapshot, config: OrchestratorConfig) -> float | None:
    """Resolve current import tariff price in EUR/kWh, or None if unconfigured."""
    if config.tariff_mode == "conservative":
        return None
    if config.tariff_mode == "sensor" and snapshot.tariff_price_eur is not None:
        return max(0.0, snapshot.tariff_price_eur)
    if config.tariff_mode == "manual_20td":
        hour = snapshot.now.hour
        weekday = snapshot.now.weekday()
        if weekday >= 5 or hour < 8:
            return config.tariff_p3_eur
        if (10 <= hour < 14) or (18 <= hour < 22):
            return config.tariff_p1_eur
        return config.tariff_p2_eur
    # Conservative fallback when price sensor is missing
    return config.tariff_p1_eur


def _update_savings(
    snapshot: EnergySnapshot,
    config: OrchestratorConfig,
    state: OrchestratorState,
) -> tuple[float | None, float | None, str]:
    """Accumulate honest estimated savings when tariff is configured."""
    price_eur = _effective_tariff_price(snapshot, config)
    formula = (
        "Ahorro = (Autoconsumo_kWh * Tarifa_Red) + "
        "(Exportado_kWh * Compensacion) - Coste_Importacion"
    )
    day_key = snapshot.now.date().isoformat()
    month_key = snapshot.now.strftime("%Y-%m")
    if state.savings_last_day != day_key:
        state.savings_last_day = day_key
        state.savings_today_eur = 0.0
    if state.savings_last_month != month_key:
        state.savings_last_month = month_key
        state.savings_month_eur = 0.0

    if price_eur is None:
        state.savings_last_tick = snapshot.now
        return None, None, formula

    if (
        state.savings_last_tick is not None
        and snapshot.solar_power_w is not None
        and snapshot.grid_power_w is not None
    ):
        dt_h = max(0.0, (snapshot.now - state.savings_last_tick).total_seconds() / 3600.0)
        if dt_h <= 0.25:
            export_kw = max(0.0, -snapshot.grid_power_w) / 1000.0
            solar_kw = max(0.0, snapshot.solar_power_w) / 1000.0
            self_consumed_kw = max(0.0, solar_kw - export_kw)
            delta_eur = (self_consumed_kw * price_eur + export_kw * config.export_comp_eur) * dt_h
            state.savings_today_eur = round(state.savings_today_eur + delta_eur, 4)
            state.savings_month_eur = round(state.savings_month_eur + delta_eur, 4)
    state.savings_last_tick = snapshot.now
    return round(state.savings_today_eur, 2), round(state.savings_month_eur, 2), formula


def _remaining_seconds(now: datetime, until: datetime | None) -> int:
    if until is None or now >= until:
        return 0
    return int(math.ceil((until - now).total_seconds()))


def _compute_icp(
    snapshot: EnergySnapshot,
    config: OrchestratorConfig,
    virtual_reserved_w: float,
) -> tuple[float | None, float | None]:
    """Compute live ICP headroom (W) and load percentage (%)."""
    if snapshot.grid_power_w is None or config.contracted_power_w <= 0:
        return None, None
    effective_limit_w = max(0.0, config.contracted_power_w - config.icp_margin_w)
    import_w = max(0.0, snapshot.grid_power_w) + max(0.0, virtual_reserved_w)
    headroom_w = round(effective_limit_w - import_w, 1)
    ratio_pct = round(min(200.0, (import_w / config.contracted_power_w) * 100.0), 1)
    return headroom_w, ratio_pct


def _build_battery_advice(snapshot: EnergySnapshot, config: OrchestratorConfig) -> str | None:
    """Read-only seasonal battery & solar headroom advice (M16, D2: zero BMS writes)."""
    if snapshot.battery_soc is None or snapshot.solar_forecast_tomorrow_kwh is None:
        return None
    tomorrow_kwh = snapshot.solar_forecast_tomorrow_kwh
    if tomorrow_kwh >= 18.0:
        return (
            f"Mañana previsión alta ({tomorrow_kwh:.1f} kWh): "
            f"deja hueco solar (reserva sugerida {int(config.house_soc_floor)}%)"
        )
    if tomorrow_kwh <= 8.0:
        return (
            f"Mañana previsión baja ({tomorrow_kwh:.1f} kWh): "
            "conviene conservar reserva de batería (>= 40%)"
        )
    return (
        f"Previsión mañana {tomorrow_kwh:.1f} kWh: "
        f"reserva actual {int(config.house_soc_floor)}% equilibrada"
    )


def _compute_ev_commitment(
    snapshot: EnergySnapshot,
    config: OrchestratorConfig,
) -> EvCommitmentStatus:
    """Compute EV departure commitment status (M04)."""
    boost_active = config.ev_boost_until is not None and snapshot.now < config.ev_boost_until
    if not snapshot.ev_connected:
        status = "on_track"
    elif snapshot.ev_soc is None:
        status = "unknown_soc"
    elif snapshot.ev_soc >= config.ev_target_soc:
        status = "reached"
    elif config.ev_allow_grid_topup:
        status = "needs_grid"
    else:
        status = "on_track"

    return EvCommitmentStatus(
        target_soc=config.ev_target_soc,
        target_time_hhmm=config.ev_target_time_hhmm,
        allow_grid_topup=config.ev_allow_grid_topup,
        grid_deadline_hhmm=config.ev_grid_deadline_hhmm,
        status=status,
        boost_active=boost_active,
        boost_until=config.ev_boost_until if boost_active else None,
    )


def evaluate_profile_preview(
    profile: ProfileName,
    config: OrchestratorConfig,
) -> dict[str, str]:
    """Return human-readable impact diff before applying a 1-touch profile (M10)."""
    if profile == ProfileName.SAVING:
        return {
            "profile": profile.value,
            "house_soc_floor": f"{config.house_soc_floor:.0f}% -> 30%",
            "ev_allow_grid_topup": f"{config.ev_allow_grid_topup} -> False",
            "summary": (
                "Prioriza autoconsumo y protege la batería hasta el 30% sin apoyo de red al VE."
            ),
        }
    if profile == ProfileName.COMFORT:
        return {
            "profile": profile.value,
            "house_soc_floor": f"{config.house_soc_floor:.0f}% -> 20%",
            "ev_allow_grid_topup": f"{config.ev_allow_grid_topup} -> True",
            "summary": (
                "Equilibrio diario: reserva 20% y apoyo de red en valle si el VE lo necesita."
            ),
        }
    if profile == ProfileName.FAST_CHARGE:
        return {
            "profile": profile.value,
            "mode": f"{config.mode.value} -> force_ev",
            "summary": (
                "Carga rápida del VE hasta el SoC objetivo respetando siempre el margen ICP."
            ),
        }
    return {
        "profile": profile.value,
        "heater_enabled": f"{config.heater_enabled} -> False",
        "pool_enabled": f"{config.pool_enabled} -> True",
        "summary": (
            "Modo Vacaciones: desactiva termo eléctrico y mantiene depuradora solo con excedente."
        ),
    }


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
    simulated_actions: list[LoadAction] = []
    alerts: list[AlertItem] = []

    # 0. Expire settling reservation when timer elapses
    if state.settling_until is not None and snapshot.now >= state.settling_until:
        state.settling_until = None
        state.virtual_reserved_w = 0.0
    settling_rem_s = _remaining_seconds(snapshot.now, state.settling_until)

    # 0b. Expire Manual total -> ALWAYS return to OBSERVING (D1)
    effective_mode = config.mode
    state.manual_total_expired = False
    if config.mode == OperatingMode.MANUAL_TOTAL:
        if config.manual_total_until is not None and snapshot.now >= config.manual_total_until:
            effective_mode = OperatingMode.OBSERVING
            state.manual_total_expired = True
            reasons.append("manual_total_expired_returned_to_observing")
            alerts.append(
                AlertItem(
                    alert_id="manual_total_expired",
                    severity="info",
                    title="Manual total finalizado",
                    detail=(
                        "Por seguridad ha vuelto a Modo Sombra. Pulsa Activar si deseas reanudar."
                    ),
                    bypass_quiet_hours=False,
                )
            )
        else:
            reasons.append("mode_manual_total")

    # 0c. BOOT_GRACE (only when started_at is tracked by HA coordinator)
    in_boot_grace = False
    if state.started_at is not None and config.boot_grace_seconds > 0:
        elapsed_boot = (snapshot.now - state.started_at).total_seconds()
        if elapsed_boot < config.boot_grace_seconds:
            in_boot_grace = True
            reasons.append("boot_grace_active")

    local_fresh = _is_local_fresh(snapshot, config)
    degraded = not local_fresh
    state.degraded = degraded
    if degraded:
        if state.degraded_since is None:
            state.degraded_since = snapshot.now
    else:
        state.degraded_since = None

    safe_hold = (
        degraded
        and state.degraded_since is not None
        and (snapshot.now - state.degraded_since).total_seconds() >= 60.0
    )

    # 0d. Virtual power reservation subtracted from usable surplus & ICP headroom (R10)
    raw_surplus_w = compute_usable_surplus_w(snapshot, config)
    usable_surplus_w = raw_surplus_w - max(0.0, state.virtual_reserved_w)
    voltage_v = compute_voltage(snapshot, config)
    ev_budget_w = usable_surplus_w + (snapshot.ev_power_w if snapshot.ev_charging else 0.0)
    icp_headroom_w, icp_ratio_pct = _compute_icp(snapshot, config, state.virtual_reserved_w)

    # Track confirmed handoff state
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

    rec_ev_amps = int(snapshot.ev_min_amps_a)
    rec_heater_on = snapshot.heater_on
    rec_pool_on = snapshot.pool_on

    is_passive_mode = (
        effective_mode in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL) or in_boot_grace
    )

    def _emit(act: LoadAction, nominal_delta_w: float = 0.0) -> None:
        simulated_actions.append(act)
        if not is_passive_mode:
            actions.append(act)
            if nominal_delta_w > 0.0 and config.settling_lock_seconds > 0:
                state.settling_until = snapshot.now + timedelta(
                    seconds=config.settling_lock_seconds
                )
                state.virtual_reserved_w += nominal_delta_w

    def _is_protected(load_id: str) -> bool:
        return load_id in config.protected_loads

    def _is_blocked(load_id: str) -> bool:
        return load_id in config.blocked_loads

    def _cycle_lock_remaining(load_id: str) -> int:
        if load_id == "heater" and snapshot.heater_on and state.heater_last_changed_at is not None:
            rem = config.heater_min_on_seconds - int(
                (snapshot.now - state.heater_last_changed_at).total_seconds()
            )
            return max(0, rem)
        if load_id == "pool" and snapshot.pool_on and state.pool_last_changed_at is not None:
            rem = config.pool_min_on_seconds - int(
                (snapshot.now - state.pool_last_changed_at).total_seconds()
            )
            return max(0, rem)
        return 0

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
        if (
            snapshot.pool_on
            and state.pool_solar_owned
            and not _is_protected("pool")
            and _cycle_lock_remaining("pool") == 0
        ):
            rec_pool_on = False
            state.pool_solar_owned = False
            state.pool_last_changed_at = snapshot.now
            state.pool_cooldown_until = snapshot.now + timedelta(
                minutes=config.pool_cooldown_minutes
            )
            state.handoff_waiting_for_off = "pool"
            state.handoff_off_confirmed_at = None
            _emit(LoadAction(target="pool", action="turn_off", reason=reason))
        if (
            snapshot.heater_on
            and state.heater_solar_owned
            and not _is_protected("heater")
            and _cycle_lock_remaining("heater") == 0
        ):
            rec_heater_on = False
            state.heater_solar_owned = False
            state.heater_last_changed_at = snapshot.now
            state.handoff_waiting_for_off = "heater"
            state.handoff_off_confirmed_at = None
            _emit(LoadAction(target="heater", action="turn_off", reason=reason))

    # Determine safety state
    if effective_mode == OperatingMode.MANUAL_TOTAL:
        safety_state = SafetyState.MANUAL_TOTAL
    elif in_boot_grace:
        safety_state = SafetyState.BOOT_GRACE
    elif safe_hold:
        safety_state = SafetyState.SAFE_HOLD
    elif degraded:
        safety_state = SafetyState.DEGRADED
    elif effective_mode == OperatingMode.OBSERVING:
        safety_state = SafetyState.OBSERVING
    else:
        safety_state = SafetyState.NORMAL

    # ICP critical check (M01 + M05: modulate EV down if headroom < 0)
    icp_low = icp_headroom_w is not None and icp_headroom_w < 0.0
    if icp_low:
        alerts.append(
            AlertItem(
                alert_id="icp_headroom_low",
                severity="critical",
                title="Margen ICP reducido",
                detail=(
                    f"Margen restante {icp_headroom_w:.0f} W. "
                    "Se bloquean arranques y se prioriza reducción modulada."
                ),
                target="icp",
                bypass_quiet_hours=True,
            )
        )

    # Mode-specific evaluation (skipped into read-only if MANUAL_TOTAL)
    if effective_mode == OperatingMode.MANUAL_TOTAL:
        reasons.append("manual_total_active_zero_commands")
    elif effective_mode == OperatingMode.PAUSED:
        reasons.append("mode_paused")
        _shed_solar_aux("paused")
        _ensure_trydan_safe("paused_safe_fallback")
    elif effective_mode == OperatingMode.HOUSE_PRIORITY:
        reasons.append("mode_house_priority")
        _shed_solar_aux("house_priority")
        _ensure_trydan_safe("house_priority_safe_fallback")
    elif degraded:
        reasons.append("degraded_stale_or_missing_local_modbus")
        alerts.append(
            AlertItem(
                alert_id="degraded_telemetry",
                severity="warning",
                title="Telemetría degradada",
                detail="Falta dato local fresco o válido. Arranques y subidas congelados.",
                target="telemetry",
                bypass_quiet_hours=True,
            )
        )
        _ensure_trydan_safe("degraded_safe_fallback")
        if (
            snapshot.grid_power_w is not None
            and snapshot.grid_power_w > config.import_shed_threshold_w
        ):
            _shed_solar_aux("degraded_grid_import")
            reasons.append("degraded_shed_aux")
    elif effective_mode == OperatingMode.FORCE_EV:
        reasons.append("mode_force_ev")
        ev_full = snapshot.ev_soc is not None and snapshot.ev_soc >= config.ev_target_soc
        if ev_full:
            reasons.append("ev_target_soc_reached")
            _ensure_trydan_safe("ev_target_soc_reached")
        elif snapshot.ev_connected and not _is_blocked("ev") and not state.ev_quarantined:
            _shed_solar_aux("force_ev_priority")
            max_allowed = config.ev_max_amps
            if icp_low and icp_headroom_w is not None:
                drop_a = max(1, math.ceil(abs(icp_headroom_w) / voltage_v))
                max_allowed = max(config.ev_min_amps, snapshot.ev_min_amps_a - drop_a)
            rec_ev_amps = max_allowed
            if snapshot.ev_mode and "minima" not in snapshot.ev_mode.lower():
                _emit(
                    LoadAction(
                        target="ev_mode",
                        action="set",
                        value=TRYDAN_MODE_MODULATED,
                        reason="force_ev",
                    )
                )
            if snapshot.ev_min_amps_a != max_allowed:
                _emit(
                    LoadAction(
                        target="ev_min_amps",
                        action="set",
                        value=max_allowed,
                        reason="force_ev",
                    ),
                    nominal_delta_w=max(0.0, (max_allowed - snapshot.ev_min_amps_a) * voltage_v),
                )
    else:
        # Normal solar / modulated / observing evaluation
        ev_wants_solar = (
            snapshot.ev_connected
            and not _is_blocked("ev")
            and not state.ev_quarantined
            and (snapshot.ev_soc is None or snapshot.ev_soc < config.ev_target_soc)
        )
        ev_absorbing_all = (
            ev_wants_solar
            and snapshot.ev_charging
            and snapshot.ev_power_w < (config.ev_max_amps * voltage_v * 0.92)
        )

        if effective_mode in (OperatingMode.AUTO_SOLAR, OperatingMode.OBSERVING):
            _ensure_trydan_safe("auto_solar_single_loop_mode_3")
            reasons.append("trydan_local_fv_exclusivo")
        elif effective_mode == OperatingMode.AUTO_EV_MODULATED:
            in_modulated = bool(snapshot.ev_mode and "minima" in snapshot.ev_mode.lower())
            settle_ok = (
                state.last_ev_command_at is None
                or (snapshot.now - state.last_ev_command_at).total_seconds()
                >= config.settle_seconds
            )
            grid_import_w = max(0.0, snapshot.grid_power_w or 0.0)
            emergency_import = grid_import_w >= config.emergency_import_w or icp_low

            if (
                state.ev_external_override_until is not None
                and snapshot.now < state.ev_external_override_until
            ):
                reasons.append("ev_external_override_paused")
            elif not ev_wants_solar:
                reasons.append("ev_not_connected_or_full")
                _ensure_trydan_safe("ev_idle_or_full")
            elif (grid_import_w > config.import_shed_threshold_w or icp_low) and in_modulated:
                if settle_ok or emergency_import:
                    excess_w = max(grid_import_w, abs(icp_headroom_w or 0.0) if icp_low else 0.0)
                    drop_a = max(1, math.ceil(excess_w / voltage_v))
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
                stop_elapsed = (
                    state.ev_stopped_at is None
                    or (snapshot.now - state.ev_stopped_at).total_seconds()
                    >= config.ev_min_stop_seconds
                )
                start_threshold_w = config.ev_start_amps * voltage_v + config.step_up_margin_w
                has_icp_room = icp_headroom_w is None or icp_headroom_w >= start_threshold_w
                if (
                    not handoff_active
                    and settling_rem_s == 0
                    and stop_elapsed
                    and has_icp_room
                    and ev_budget_w >= start_threshold_w
                ):
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
                            ),
                            nominal_delta_w=start_a * voltage_v,
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
                target_a = min(config.ev_max_amps, math.floor(ev_budget_w / voltage_v))
                step_a = min(2, target_a - snapshot.ev_min_amps_a)
                required_extra_w = (
                    step_a * voltage_v + config.step_up_margin_w if step_a > 0 else 0.0
                )
                has_icp_room = icp_headroom_w is None or icp_headroom_w >= required_extra_w
                if (
                    not handoff_active
                    and settling_rem_s == 0
                    and settle_ok
                    and step_a > 0
                    and has_icp_room
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
                            ),
                            nominal_delta_w=step_a * voltage_v,
                        )
                        reasons.append(f"ev_step_up:{next_a}A")
                else:
                    state.ev_step_up_since = None

        # Auxiliary Load 1: Water Heater (Termo)
        heater_temp_ok = (
            snapshot.heater_temp_c is not None
            and snapshot.heater_temp_c >= config.heater_target_temp_c
        )
        heater_ext_paused = (
            state.heater_external_override_until is not None
            and snapshot.now < state.heater_external_override_until
        )
        if not _is_blocked("heater") and not state.heater_quarantined and not heater_ext_paused:
            if snapshot.heater_on and state.heater_solar_owned:
                if (
                    heater_temp_ok
                    and _cycle_lock_remaining("heater") == 0
                    and not _is_protected("heater")
                ):
                    rec_heater_on = False
                    state.heater_solar_owned = False
                    state.heater_last_changed_at = snapshot.now
                    state.handoff_waiting_for_off = "heater"
                    state.handoff_off_confirmed_at = None
                    _emit(
                        LoadAction(target="heater", action="turn_off", reason="heater_target_temp")
                    )
                    reasons.append("heater_reached_target_temp")
                elif (
                    ev_absorbing_all
                    and _cycle_lock_remaining("heater") == 0
                    and not _is_protected("heater")
                ):
                    rec_heater_on = False
                    state.heater_solar_owned = False
                    state.heater_last_changed_at = snapshot.now
                    state.handoff_waiting_for_off = "heater"
                    state.handoff_off_confirmed_at = None
                    _emit(
                        LoadAction(target="heater", action="turn_off", reason="heater_yield_to_ev")
                    )
                    reasons.append("heater_yielded_to_ev")
            elif (
                config.heater_enabled
                and not snapshot.heater_on
                and not heater_temp_ok
                and not ev_absorbing_all
                and not handoff_active
                and settling_rem_s == 0
                and not icp_low
            ):
                export_w = -(snapshot.grid_power_w or 0.0) - max(0.0, state.virtual_reserved_w)
                if export_w >= config.heater_start_export_w:
                    if state.heater_export_since is None:
                        state.heater_export_since = snapshot.now
                        reasons.append("heater_export_holding")
                    elif (
                        snapshot.now - state.heater_export_since
                    ).total_seconds() >= config.heater_hold_seconds:
                        rec_heater_on = True
                        state.heater_solar_owned = True
                        state.heater_last_changed_at = snapshot.now
                        state.heater_export_since = None
                        _emit(
                            LoadAction(target="heater", action="turn_on", reason="heater_surplus"),
                            nominal_delta_w=config.heater_nominal_w,
                        )
                        reasons.append("heater_started")
                else:
                    state.heater_export_since = None
            else:
                state.heater_export_since = None

        # Auxiliary Load 2: Pool Pump (Depuradora)
        pool_cooldown_active = (
            state.pool_cooldown_until is not None and snapshot.now < state.pool_cooldown_until
        )
        pool_quota_exhausted = state.pool_minutes_today >= config.pool_daily_max_minutes
        grid_import_w = max(0.0, snapshot.grid_power_w or 0.0)
        bat_discharge_w = max(0.0, snapshot.battery_power_w or 0.0)
        pool_ext_paused = (
            state.pool_external_override_until is not None
            and snapshot.now < state.pool_external_override_until
        )

        if not _is_blocked("pool") and not state.pool_quarantined and not pool_ext_paused:
            if snapshot.pool_on:
                if pool_quota_exhausted and not _is_protected("pool"):
                    rec_pool_on = False
                    state.pool_solar_owned = False
                    state.pool_last_changed_at = snapshot.now
                    _emit(LoadAction(target="pool", action="turn_off", reason="pool_daily_quota"))
                    reasons.append("pool_daily_quota_reached")
                elif (
                    state.pool_solar_owned
                    and not _is_protected("pool")
                    and _cycle_lock_remaining("pool") == 0
                ):
                    if ev_wants_solar and snapshot.ev_charging:
                        rec_pool_on = False
                        state.pool_solar_owned = False
                        state.pool_last_changed_at = snapshot.now
                        state.pool_cooldown_until = snapshot.now + timedelta(
                            minutes=config.pool_cooldown_minutes
                        )
                        state.handoff_waiting_for_off = "pool"
                        state.handoff_off_confirmed_at = None
                        _emit(
                            LoadAction(target="pool", action="turn_off", reason="pool_yield_to_ev")
                        )
                        reasons.append("pool_yielded_to_ev")
                    elif rec_heater_on:
                        rec_pool_on = False
                        state.pool_solar_owned = False
                        state.pool_last_changed_at = snapshot.now
                        state.pool_cooldown_until = snapshot.now + timedelta(
                            minutes=config.pool_cooldown_minutes
                        )
                        state.handoff_waiting_for_off = "pool"
                        state.handoff_off_confirmed_at = None
                        _emit(
                            LoadAction(
                                target="pool", action="turn_off", reason="pool_yield_to_heater"
                            )
                        )
                        reasons.append("pool_yielded_to_heater")
                    elif (
                        grid_import_w > 400.0 or bat_discharge_w > DEFAULT_BATTERY_DISCHARGE_SHED_W
                    ):
                        if state.aux_shed_since is None:
                            state.aux_shed_since = snapshot.now
                            reasons.append("pool_shed_holding")
                        elif (
                            snapshot.now - state.aux_shed_since
                        ).total_seconds() >= config.aux_shed_hold_seconds:
                            rec_pool_on = False
                            state.pool_solar_owned = False
                            state.pool_last_changed_at = snapshot.now
                            state.pool_cooldown_until = snapshot.now + timedelta(
                                minutes=config.pool_cooldown_minutes
                            )
                            state.aux_shed_since = None
                            _emit(
                                LoadAction(
                                    target="pool", action="turn_off", reason="pool_grid_or_bat"
                                )
                            )
                            reasons.append("pool_shed_grid_or_bat")
                    else:
                        state.aux_shed_since = None
            else:
                state.aux_shed_since = None
                export_w = -(snapshot.grid_power_w or 0.0) - max(0.0, state.virtual_reserved_w)
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
                    and settling_rem_s == 0
                    and not icp_low
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
                        state.pool_last_changed_at = snapshot.now
                        state.pool_export_since = None
                        _emit(
                            LoadAction(target="pool", action="turn_on", reason="pool_surplus"),
                            nominal_delta_w=config.pool_nominal_w,
                        )
                        reasons.append("pool_started")
                else:
                    state.pool_export_since = None

    # Traffic light
    if effective_mode == OperatingMode.MANUAL_TOTAL:
        traffic_light = GlobalTrafficLight.MANUAL
    elif effective_mode == OperatingMode.OBSERVING:
        traffic_light = GlobalTrafficLight.SHADOW
    elif icp_low or safe_hold:
        traffic_light = GlobalTrafficLight.CRITICAL
    elif degraded or (icp_ratio_pct is not None and icp_ratio_pct >= 80.0):
        traffic_light = GlobalTrafficLight.WARNING
    else:
        traffic_light = GlobalTrafficLight.OPTIMAL

    # Honest savings & advisory outputs
    savings_today, savings_month, savings_formula = _update_savings(snapshot, config, state)
    battery_advice = _build_battery_advice(snapshot, config)
    ev_commitment = _compute_ev_commitment(snapshot, config)

    washer_suggestion: str | None = None
    if (
        not degraded
        and not icp_low
        and usable_surplus_w >= 1500.0
        and (icp_headroom_w is None or icp_headroom_w >= 1800.0)
    ):
        washer_suggestion = (
            f"Buen momento para lavadora o lavavajillas ({round(usable_surplus_w)} W de excedente)"
        )

    # Flex loads radar
    heater_min_on_rem = _cycle_lock_remaining("heater")
    pool_min_on_rem = _cycle_lock_remaining("pool")
    flex_loads = (
        FlexLoadStatus(
            load_id="heater",
            name="Termo eléctrico",
            state=(
                "quarantined"
                if state.heater_quarantined
                else "protected"
                if _is_protected("heater")
                else "blocked"
                if _is_blocked("heater")
                else "on"
                if snapshot.heater_on
                else "off"
            ),
            reason=(
                "En cuarentena por discrepancia de retorno"
                if state.heater_quarantined
                else "Ciclo protegido activo"
                if heater_min_on_rem > 0
                else ("Calentando con excedente" if rec_heater_on else "En espera de excedente")
            ),
            power_w=snapshot.heater_power_w,
            min_on_remaining_s=heater_min_on_rem,
            protected=_is_protected("heater"),
            blocked=_is_blocked("heater"),
            quarantined=state.heater_quarantined,
        ),
        FlexLoadStatus(
            load_id="pool",
            name="Depuradora piscina",
            state=(
                "quarantined"
                if state.pool_quarantined
                else "protected"
                if _is_protected("pool")
                else "blocked"
                if _is_blocked("pool")
                else "on"
                if snapshot.pool_on
                else "off"
            ),
            reason=(
                "En cuarentena por discrepancia de retorno"
                if state.pool_quarantined
                else (
                    f"Cuota diaria {int(state.pool_minutes_today)}/"
                    f"{config.pool_daily_max_minutes} min"
                )
            ),
            power_w=snapshot.pool_power_w,
            min_on_remaining_s=pool_min_on_rem,
            protected=_is_protected("pool"),
            blocked=_is_blocked("pool"),
            quarantined=state.pool_quarantined,
        ),
    )

    why_by_device = {
        "house": "Prioridad #1 inamovible: el consumo doméstico nunca se recorta.",
        "ev": (
            "Modo Sombra: proponiendo sin actuar."
            if effective_mode == OperatingMode.OBSERVING
            else f"VE {rec_ev_amps} A (suelo batería {int(config.house_soc_floor)}%)."
        ),
        "heater": flex_loads[0].reason,
        "pool": flex_loads[1].reason,
    }

    freshness_by_source = {
        "modbus_grid": _sample_age_s(snapshot.now, snapshot.local_sample_time),
        "modbus_solar": _sample_age_s(
            snapshot.now, snapshot.solar_sample_time or snapshot.local_sample_time
        ),
        "battery": _sample_age_s(
            snapshot.now, snapshot.battery_sample_time or snapshot.local_sample_time
        ),
        "ev": _sample_age_s(snapshot.now, snapshot.ev_sample_time),
    }

    heater_draw = _effective_heater_draw_w(snapshot, config)
    mode_prefix = (
        "Observing (dry-run)" if effective_mode == OperatingMode.OBSERVING else effective_mode.value
    )
    summary = (
        f"{mode_prefix}: surplus {round(usable_surplus_w)} W | "
        f"EV {rec_ev_amps} A | "
        f"Heater {'on' if rec_heater_on else 'off'} ({round(heater_draw)} W) | "
        f"Pool {'on' if rec_pool_on else 'off'} "
        f"({int(state.pool_minutes_today)}/{config.pool_daily_max_minutes} min)"
    )

    if effective_mode == OperatingMode.OBSERVING:
        human_headline = (
            f"Modo Sombra (sin actuar): excedente {round(usable_surplus_w)} W · "
            f"propondría VE {rec_ev_amps} A"
        )
    elif effective_mode == OperatingMode.MANUAL_TOTAL:
        human_headline = "Manual total activo: orquestador en pausa completa."
    elif degraded:
        human_headline = "Telemetría degradada: arranques congelados por seguridad."
    else:
        human_headline = (
            f"Excedente {round(usable_surplus_w)} W · VE {rec_ev_amps} A · "
            f"Termo {'ON' if rec_heater_on else 'OFF'} · "
            f"Depuradora {'ON' if rec_pool_on else 'OFF'}"
        )

    return (
        DispatchPlan(
            effective_mode=effective_mode,
            degraded=degraded,
            telemetry_source=snapshot.telemetry_source,
            usable_surplus_w=round(usable_surplus_w, 1),
            ev_budget_w=round(ev_budget_w, 1),
            recommended_ev_amps=rec_ev_amps,
            recommended_heater_on=rec_heater_on,
            recommended_pool_on=rec_pool_on,
            decision_summary=summary,
            reasons=tuple(reasons),
            actions=tuple(actions),
            simulated_actions=tuple(simulated_actions),
            traffic_light=traffic_light,
            safety_state=safety_state,
            icp_headroom_w=icp_headroom_w,
            icp_load_ratio_pct=icp_ratio_pct,
            settling_remaining_s=settling_rem_s,
            human_headline=human_headline,
            why_by_device=why_by_device,
            flex_loads=flex_loads,
            ev_commitment=ev_commitment,
            washer_suggestion=washer_suggestion,
            battery_advice=battery_advice,
            alerts=tuple(alerts),
            estimated_savings_today_eur=savings_today,
            estimated_savings_month_eur=savings_month,
            savings_formula=savings_formula,
            freshness_by_source=freshness_by_source,
        ),
        state,
    )
