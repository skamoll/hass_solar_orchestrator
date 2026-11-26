"""Pure deterministic decision engine for Solar Excess Orchestrator."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import datetime, timedelta

from .const import (
    CLOUD_POCKET_DAILY_MAX_KWH,
    CLOUD_POCKET_EPISODE_MAX_KWH,
    CLOUD_POCKET_EPISODE_MAX_SECONDS,
    FORBIDDEN_ENTITY_PATTERNS,
    HEATER_IDLE_ACTIVE_MIN_W,
    HEATER_IDLE_EXIT_W,
    HEATER_IDLE_HOLD_SECONDS,
    HEATER_IDLE_THRESHOLD_W,
    SHOWER_DELTA_L_60S,
    SHOWER_DELTA_T_C,
    SHOWER_FLOW_HOLD_SECONDS,
    SHOWER_FLOW_THRESHOLD_L_MIN,
    SHOWER_VETO_COOLDOWN_MINUTES,
    SHOWER_VETO_MAX_MINUTES,
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
    is_pool_in_season,
)

TRYDAN_MODE_MODULATED = 2  # FV + Minima Potencia
TRYDAN_MODE_SOLAR_ONLY = 3  # FV Exclusivo (autonomous safe fallback)
_FORBIDDEN_REGEXES = tuple(re.compile(p, re.IGNORECASE) for p in FORBIDDEN_ENTITY_PATTERNS)
_FLEX_LOADS: tuple[str, ...] = ("ev", "heater", "pool")
_BAD_WEATHER_CONDITIONS = frozenset({"rainy", "pouring", "lightning-rainy", "snowy"})


class SecurityDenylistViolation(Exception):
    """Raised if an action ever targets a forbidden critical entity."""


def is_forbidden_entity(entity_id: str) -> bool:
    """Return True if entity_id matches the immutable anti-cut Denylist."""
    return any(rx.match(entity_id) for rx in _FORBIDDEN_REGEXES)


def compute_voltage(snapshot: EnergySnapshot, config: OrchestratorConfig) -> float:
    """Estimate real AC voltage from Trydan readings with safe nominal fallback."""
    if (
        _is_ev_fresh(snapshot, config)
        and snapshot.ev_power_w > 1000.0
        and snapshot.ev_current_a >= 6.0
    ):
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

    # A frozen battery reading (e.g. stuck at -3 kW charging) must never be reclaimed
    # as surplus: it would start loads that are really fed by the grid.
    battery_fresh = True
    if snapshot.battery_sample_time is not None:
        bat_age_s = (snapshot.now - snapshot.battery_sample_time).total_seconds()
        battery_fresh = 0.0 <= bat_age_s <= config.stale_timeout_seconds

    battery_charge_reclaim_w = 0.0
    if bat_w < 0.0 and soc >= config.house_soc_floor and battery_fresh:
        battery_charge_reclaim_w = -bat_w

    slack_w = (
        config.battery_slack_w
        if soc >= (config.house_soc_floor + config.battery_slack_soc_margin)
        else 0.0
    )
    return export_w + battery_charge_reclaim_w + slack_w


def _effective_heater_draw_w(
    snapshot: EnergySnapshot,
    config: OrchestratorConfig,
    state: OrchestratorState,
) -> float:
    """Use measured heater power and release reserve when thermostat is idle."""
    if not snapshot.heater_on or state.heater_thermostat_idle:
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


def _is_ev_fresh(snapshot: EnergySnapshot, config: OrchestratorConfig) -> bool:
    """Return True if EV charger telemetry is fresh (or no EV timestamp is tracked)."""
    if snapshot.ev_sample_time is None:
        return True
    age = (snapshot.now - snapshot.ev_sample_time).total_seconds()
    return 0.0 <= age <= config.stale_timeout_seconds


def _sample_age_s(now: datetime, sample_time: datetime | None) -> int | None:
    if sample_time is None:
        return None
    return max(0, int((now - sample_time).total_seconds()))


def _rollover_pool_daily(
    snapshot: EnergySnapshot,
    state: OrchestratorState,
    *,
    pool_in_season: bool = True,
) -> None:
    """Accumulate real pool runtime (>150 W if measured) and reset at local midnight."""
    day_key = snapshot.calendar_time.date().isoformat()
    if state.pool_last_day is None:
        state.pool_last_day = day_key
    elif state.pool_last_day != day_key:
        state.pool_last_day = day_key
        state.pool_minutes_today = 0.0
        state.pool_manual_blocked_today = False
        state.pool_cooldown_until = None

    pool_actually_drawing = (
        pool_in_season
        and snapshot.pool_on
        and (snapshot.pool_power_w is None or snapshot.pool_power_w > 150.0)
    )
    if pool_actually_drawing and state.pool_last_tick is not None:
        dt_min = max(0.0, (snapshot.now - state.pool_last_tick).total_seconds() / 60.0)
        if dt_min <= 5.0:
            state.pool_minutes_today += dt_min
    state.pool_last_tick = snapshot.now


def _update_heater_thermostat_idle(
    snapshot: EnergySnapshot,
    state: OrchestratorState,
) -> None:
    """Detect mechanical thermostat cut (<100 W fresh after >800 W) without false 0W."""
    if not snapshot.heater_on:
        state.heater_seen_active_draw = False
        state.heater_low_power_since = None
        state.heater_thermostat_idle = False
        return

    # Never coerce unavailable/None or stale power (>120 s) to 0 W!
    if snapshot.heater_power_w is None:
        state.heater_low_power_since = None
        state.heater_thermostat_idle = False
        return
    if snapshot.heater_power_sample_time is not None:
        age_s = (snapshot.now - snapshot.heater_power_sample_time).total_seconds()
        if age_s > 120.0 or age_s < 0.0:
            state.heater_low_power_since = None
            state.heater_thermostat_idle = False
            return

    pwr = snapshot.heater_power_w
    if pwr >= HEATER_IDLE_ACTIVE_MIN_W:
        state.heater_seen_active_draw = True

    if state.heater_thermostat_idle and pwr > HEATER_IDLE_EXIT_W:
        state.heater_thermostat_idle = False
        state.heater_low_power_since = None
        return

    if state.heater_seen_active_draw and pwr < HEATER_IDLE_THRESHOLD_W:
        if state.heater_low_power_since is None:
            state.heater_low_power_since = snapshot.now
        elif (
            snapshot.now - state.heater_low_power_since
        ).total_seconds() >= HEATER_IDLE_HOLD_SECONDS:
            state.heater_thermostat_idle = True
    else:
        state.heater_low_power_since = None


def _update_shower_pattern(
    snapshot: EnergySnapshot,
    config: OrchestratorConfig,
    state: OrchestratorState,
    reasons: list[str],
) -> bool:
    """Detect active shower via flow, rapid water delta, or abrupt ΔT drop (V3-M02)."""
    now = snapshot.now
    if not config.shower_pattern_enabled:
        return False

    # Clear veto if max 90 min hard cap or cooldown elapsed
    if state.shower_veto_until is not None and now >= state.shower_veto_until:
        state.shower_veto_until = None
        state.shower_veto_started_at = None

    triggered = False
    if (
        snapshot.heater_flow_l_min is not None
        and snapshot.heater_flow_l_min >= SHOWER_FLOW_THRESHOLD_L_MIN
    ):
        if state.shower_flow_since is None:
            state.shower_flow_since = now
        elif (now - state.shower_flow_since).total_seconds() >= SHOWER_FLOW_HOLD_SECONDS:
            triggered = True
            reasons.append("shower_pattern_flow")
    else:
        state.shower_flow_since = None

    # Volume delta >= 2 L in 60 s (discarding counter resets)
    if snapshot.heater_daily_water_l is not None:
        cutoff_v = now - timedelta(seconds=60)
        state.shower_water_history = [
            (ts, val) for ts, val in state.shower_water_history if ts >= cutoff_v
        ]
        state.shower_water_history.append((now, snapshot.heater_daily_water_l))
        if len(state.shower_water_history) >= 2:
            oldest_v = state.shower_water_history[0][1]
            delta_v = snapshot.heater_daily_water_l - oldest_v
            if delta_v >= SHOWER_DELTA_L_60S:
                triggered = True
                reasons.append("shower_pattern_volume_spike")

    # Abrupt temperature drop <= -1.5 °C in 10 min
    if snapshot.heater_temp_c is not None:
        cutoff_t = now - timedelta(minutes=10)
        state.shower_temp_history = [
            (ts, val) for ts, val in state.shower_temp_history if ts >= cutoff_t
        ]
        state.shower_temp_history.append((now, snapshot.heater_temp_c))
        if len(state.shower_temp_history) >= 2:
            max_recent_t = max(val for _, val in state.shower_temp_history)
            if (snapshot.heater_temp_c - max_recent_t) <= SHOWER_DELTA_T_C:
                triggered = True
                reasons.append("shower_pattern_temp_drop")

    if triggered:
        if state.shower_veto_started_at is None:
            state.shower_veto_started_at = now
        hard_cap = state.shower_veto_started_at + timedelta(minutes=SHOWER_VETO_MAX_MINUTES)
        if now < hard_cap:
            candidate_until = now + timedelta(minutes=SHOWER_VETO_COOLDOWN_MINUTES)
            state.shower_veto_until = min(candidate_until, hard_cap)

    return state.shower_veto_until is not None and now < state.shower_veto_until


def _update_cloud_pocket_metrics(
    snapshot: EnergySnapshot,
    state: OrchestratorState,
) -> None:
    """Track passive cloud-bridging budget in metrics/observing only (V3-C3)."""
    day_key = snapshot.calendar_time.date().isoformat()
    if state.cloud_pocket_day != day_key:
        state.cloud_pocket_day = day_key
        state.cloud_pocket_used_kwh_today = 0.0
        state.cloud_pocket_episode_kwh = 0.0
        state.cloud_pocket_episode_started_at = None

    bat_discharge_w = max(0.0, snapshot.battery_power_w or 0.0)
    if bat_discharge_w > 50.0 and state.cloud_pocket_last_tick is not None:
        dt_s = max(0.0, (snapshot.now - state.cloud_pocket_last_tick).total_seconds())
        if dt_s <= 120.0:
            if state.cloud_pocket_episode_started_at is None:
                state.cloud_pocket_episode_started_at = snapshot.now
            ep_elapsed = (snapshot.now - state.cloud_pocket_episode_started_at).total_seconds()
            delta_kwh = (bat_discharge_w / 1000.0) * (dt_s / 3600.0)
            if (
                ep_elapsed <= CLOUD_POCKET_EPISODE_MAX_SECONDS
                and state.cloud_pocket_episode_kwh < CLOUD_POCKET_EPISODE_MAX_KWH
                and state.cloud_pocket_used_kwh_today < CLOUD_POCKET_DAILY_MAX_KWH
            ):
                state.cloud_pocket_episode_kwh = round(
                    state.cloud_pocket_episode_kwh + delta_kwh, 4
                )
                state.cloud_pocket_used_kwh_today = round(
                    state.cloud_pocket_used_kwh_today + delta_kwh, 4
                )
    else:
        state.cloud_pocket_episode_started_at = None
        state.cloud_pocket_episode_kwh = 0.0
    state.cloud_pocket_last_tick = snapshot.now


def _evaluate_storm_alert(snapshot: EnergySnapshot) -> bool:
    """Return True if >= 2 of the next 3 forecast days are rainy/stormy/heavily cloudy."""
    if not snapshot.weather_daily_forecast:
        return False
    bad_days = 0
    for day in snapshot.weather_daily_forecast[:3]:
        cond = str(day.get("condition", "")).lower()
        cloud = day.get("cloud_coverage")
        precip = day.get("precipitation_probability")
        if cond in _BAD_WEATHER_CONDITIONS or (
            cond == "cloudy"
            and (
                (isinstance(cloud, (int, float)) and cloud >= 80)
                or (isinstance(precip, (int, float)) and precip >= 60)
            )
        ):
            bad_days += 1
    return bad_days >= 2


def _effective_tariff_price(snapshot: EnergySnapshot, config: OrchestratorConfig) -> float | None:
    """Resolve current import tariff price in EUR/kWh, or None if unconfigured."""
    if config.tariff_mode == "conservative":
        return None
    if config.tariff_mode == "sensor":
        return snapshot.tariff_price_eur
    if config.tariff_mode == "manual_20td":
        hour = snapshot.calendar_time.hour
        weekday = snapshot.calendar_time.weekday()
        if weekday >= 5 or hour < 8:
            return config.tariff_p3_eur
        if (10 <= hour < 14) or (18 <= hour < 22):
            return config.tariff_p1_eur
        return config.tariff_p2_eur
    return config.tariff_p1_eur


def _update_savings(
    snapshot: EnergySnapshot,
    config: OrchestratorConfig,
    state: OrchestratorState,
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    str,
]:
    """Accumulate estimated savings, grid import cost, export revenue, and net cost."""
    price_eur = _effective_tariff_price(snapshot, config)
    formula = "Ahorro = (Autoconsumo_kWh * Tarifa_Red) + (Exportado_kWh * Compensacion)"
    day_key = snapshot.calendar_time.date().isoformat()
    month_key = snapshot.calendar_time.strftime("%Y-%m")
    if state.savings_last_day != day_key:
        state.savings_last_day = day_key
        state.savings_today_eur = 0.0
        state.grid_import_cost_today_eur = 0.0
        state.grid_export_revenue_today_eur = 0.0
    if state.savings_last_month != month_key:
        state.savings_last_month = month_key
        state.savings_month_eur = 0.0
        state.grid_import_cost_month_eur = 0.0
        state.grid_export_revenue_month_eur = 0.0

    if price_eur is None:
        state.savings_last_tick = snapshot.now
        return None, None, None, None, None, None, None, None, formula

    if state.savings_last_tick is not None and snapshot.grid_power_w is not None:
        dt_h = max(0.0, (snapshot.now - state.savings_last_tick).total_seconds() / 3600.0)
        if dt_h <= 0.25:
            import_kw = max(0.0, snapshot.grid_power_w) / 1000.0
            export_kw = max(0.0, -snapshot.grid_power_w) / 1000.0
            local_now = snapshot.calendar_time
            midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            # Limit each bucket to the part of the sample interval inside that period.
            day_h = min(dt_h, (snapshot.now - midnight).total_seconds() / 3600)
            month_h = min(dt_h, (snapshot.now - midnight.replace(day=1)).total_seconds() / 3600)
            import_rate = import_kw * price_eur
            export_rate = export_kw * config.export_comp_eur
            state.grid_import_cost_today_eur += import_rate * day_h
            state.grid_import_cost_month_eur += import_rate * month_h
            state.grid_export_revenue_today_eur += export_rate * day_h
            state.grid_export_revenue_month_eur += export_rate * month_h
            if snapshot.solar_power_w is not None:
                solar_kw = max(0.0, snapshot.solar_power_w) / 1000.0
                self_consumed_kw = max(0.0, solar_kw - export_kw)
                value_rate = self_consumed_kw * price_eur + export_rate
                state.savings_today_eur += value_rate * day_h
                state.savings_month_eur += value_rate * month_h
    state.savings_last_tick = snapshot.now
    net_today = round(state.grid_import_cost_today_eur - state.grid_export_revenue_today_eur, 2)
    net_month = round(state.grid_import_cost_month_eur - state.grid_export_revenue_month_eur, 2)
    return (
        round(state.savings_today_eur, 2),
        round(state.savings_month_eur, 2),
        round(state.grid_import_cost_today_eur, 2),
        round(state.grid_import_cost_month_eur, 2),
        round(state.grid_export_revenue_today_eur, 2),
        round(state.grid_export_revenue_month_eur, 2),
        net_today,
        net_month,
        formula,
    )


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
    pool_in_season = is_pool_in_season(snapshot.calendar_time, config)
    _rollover_pool_daily(snapshot, state, pool_in_season=pool_in_season)
    _update_heater_thermostat_idle(snapshot, state)
    _update_cloud_pocket_metrics(snapshot, state)
    reasons: list[str] = []
    actions: list[LoadAction] = []
    simulated_actions: list[LoadAction] = []
    alerts: list[AlertItem] = []

    shower_veto_active = _update_shower_pattern(snapshot, config, state, reasons)
    extra_shower_active = (
        config.extra_shower_until is not None and snapshot.now < config.extra_shower_until
    )

    if state.settling_until is not None and snapshot.now >= state.settling_until:
        state.settling_until = None
        state.virtual_reserved_w = 0.0
    settling_rem_s = _remaining_seconds(snapshot.now, state.settling_until)
    reserved0_w = max(0.0, state.virtual_reserved_w)  # pending reservations at tick start

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
        and (snapshot.now - state.degraded_since).total_seconds() >= config.safe_hold_seconds
    )

    ev_fresh = _is_ev_fresh(snapshot, config)
    if not ev_fresh:
        if state.ev_stale_since is None:
            state.ev_stale_since = snapshot.now
    else:
        state.ev_stale_since = None

    ev_stale_safe_fallback = (
        not ev_fresh
        and state.ev_stale_since is not None
        and (snapshot.now - state.ev_stale_since).total_seconds() >= config.ev_stale_hold_seconds
    )

    raw_surplus_w = compute_usable_surplus_w(snapshot, config)
    usable_surplus_w = raw_surplus_w - max(0.0, state.virtual_reserved_w)
    voltage_v = compute_voltage(snapshot, config)
    ev_budget_w = usable_surplus_w + (
        snapshot.ev_power_w if (snapshot.ev_charging and ev_fresh) else 0.0
    )
    icp_headroom_w, icp_ratio_pct = _compute_icp(snapshot, config, state.virtual_reserved_w)

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
            state.handoff_preempt_winner = None

    rec_ev_amps = int(snapshot.ev_min_amps_a)
    rec_heater_on = snapshot.heater_on
    rec_pool_on = snapshot.pool_on

    is_global_passive = (
        effective_mode in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL) or in_boot_grace
    )

    def _target_group(target: str) -> str:
        if target.startswith("ev"):
            return "ev"
        return target

    def _is_load_active(target: str) -> bool:
        if is_global_passive:
            return False
        if config.load_modes is None:
            return True
        return config.load_modes.get(_target_group(target), "observing") == "active"

    def _emit(act: LoadAction, nominal_delta_w: float = 0.0) -> None:
        simulated_actions.append(act)
        if _is_load_active(act.target):
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

    # Tick-local allocation ledger: no increase may follow a decrease in the same tick, and
    # every increase consumes from one shared budget / ICP headroom.
    tick: dict[str, float | bool] = {"decreased": False, "granted_w": 0.0}

    def _mirrors(load_id: str) -> bool:
        """True if this load's decisions reflect reality (executed, or a global shadow sim).

        A per-load observing load in an otherwise active system never really changes, so
        its simulated actions must neither consume budget nor block/free real loads.
        """
        return is_global_passive or _is_load_active(load_id)

    def _mark_decrease(load_id: str) -> None:
        if _mirrors(load_id):
            tick["decreased"] = True

    def _icp_room_w(*, include_reserved: bool = True) -> float | None:
        """Common ICP headroom: limit - real import - pending reservations - tick grants.

        Export is never credited as headroom: a passing cloud turns it into import at once.
        """
        if snapshot.grid_power_w is None or config.contracted_power_w <= 0:
            return None
        room_w = max(0.0, config.contracted_power_w - config.icp_margin_w) - max(
            0.0, snapshot.grid_power_w
        )
        if include_reserved:
            room_w -= reserved0_w + float(tick["granted_w"])
        return room_w

    def _headroom_ok(delta_w: float) -> bool:
        """One headroom rule for every step-up, whether net importing or exporting."""
        room_w = _icp_room_w()
        return room_w is None or room_w >= delta_w + config.step_up_margin_w

    def _ev_cmd_mark(*, started: bool = False, stopped: bool = False) -> None:
        """Record EV actuator timers only for commands that really execute (never shadow)."""
        if not _is_load_active("ev"):
            return
        state.last_ev_command_at = snapshot.now
        if started:
            state.ev_started_at = snapshot.now
        if stopped:
            state.ev_stopped_at = snapshot.now

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
            _mark_decrease("ev")
            _ev_cmd_mark()
        if snapshot.ev_min_amps_a != config.ev_min_amps:
            _emit(
                LoadAction(
                    target="ev_min_amps",
                    action="set",
                    value=config.ev_min_amps,
                    reason=reason,
                )
            )
            _mark_decrease("ev")

    def _priority_rank(load_id: str) -> int:
        order = config.priority_order
        return order.index(load_id) if load_id in order else len(order)

    def _outranks(load_a: str, load_b: str) -> bool:
        """True if load_a has strictly higher priority than load_b in priority_order."""
        return _priority_rank(load_a) < _priority_rank(load_b)

    def _aux_turn_on(load_id: str, reason: str, nominal_w: float) -> None:
        """Start an aux load; claim solar ownership only if the command really executes.

        In shadow/observing modes the action is only simulated, so claiming ownership
        would let a later active mode turn off a load the user switched on by hand.
        """
        nonlocal rec_heater_on, rec_pool_on
        if load_id == "heater":
            rec_heater_on = True
        else:
            rec_pool_on = True
        _emit(LoadAction(target=load_id, action="turn_on", reason=reason), nominal_w)
        if _mirrors(load_id):
            tick["granted_w"] = float(tick["granted_w"]) + nominal_w
        if not _is_load_active(load_id):
            return
        if load_id == "heater":
            state.heater_solar_owned = True
            state.heater_last_changed_at = snapshot.now
        else:
            state.pool_solar_owned = True
            state.pool_last_changed_at = snapshot.now

    def _aux_turn_off(load_id: str, reason: str, *, cooldown: bool, handoff: bool) -> None:
        """Stop an aux load; mutate ownership/handoff state only if it really executes."""
        nonlocal rec_heater_on, rec_pool_on
        if load_id == "heater":
            rec_heater_on = False
        else:
            rec_pool_on = False
        _emit(LoadAction(target=load_id, action="turn_off", reason=reason))
        _mark_decrease(load_id)
        if not _is_load_active(load_id):
            return
        if load_id == "heater":
            state.heater_solar_owned = False
            state.heater_last_changed_at = snapshot.now
            state.heater_shed_since = None
        else:
            state.pool_solar_owned = False
            state.pool_last_changed_at = snapshot.now
            state.aux_shed_since = None
            if cooldown:
                state.pool_cooldown_until = snapshot.now + timedelta(
                    minutes=config.pool_cooldown_minutes
                )
        if handoff:
            state.handoff_waiting_for_off = load_id
            state.handoff_off_confirmed_at = None

    def _shed_solar_aux(reason: str, *, force_safety: bool = False) -> None:
        if (
            pool_in_season
            and snapshot.pool_on
            and state.pool_solar_owned
            and not _is_protected("pool")
            and (force_safety or _cycle_lock_remaining("pool") == 0)
        ):
            _aux_turn_off("pool", reason, cooldown=True, handoff=True)
        # V3-M02: shower_veto blocks optimization turn_off, NEVER electrical safety cuts
        if (
            snapshot.heater_on
            and state.heater_solar_owned
            and not _is_protected("heater")
            and (force_safety or (not shower_veto_active and _cycle_lock_remaining("heater") == 0))
        ):
            _aux_turn_off("heater", reason, cooldown=False, handoff=True)

    def _shed_aux_for_icp_deficit(deficit_w: float, reason: str) -> None:
        """Shed solar-owned aux loads in reverse priority order until deficit_w <= 0."""
        if deficit_w <= 0.0:
            return
        for aux_id in sorted(("heater", "pool"), key=_priority_rank, reverse=True):
            if aux_id == "pool":
                can_shed = (
                    pool_in_season
                    and snapshot.pool_on
                    and state.pool_solar_owned
                    and not _is_protected("pool")
                    and _cycle_lock_remaining("pool") == 0
                )
                freed_w = (
                    snapshot.pool_power_w
                    if snapshot.pool_power_w is not None
                    else config.pool_nominal_w
                )
            else:
                can_shed = (
                    snapshot.heater_on
                    and state.heater_solar_owned
                    and not _is_protected("heater")
                    and not shower_veto_active
                    and _cycle_lock_remaining("heater") == 0
                )
                freed_w = _effective_heater_draw_w(snapshot, config, state)
            if not can_shed:
                continue
            _aux_turn_off(aux_id, reason, cooldown=(aux_id == "pool"), handoff=True)
            deficit_w -= max(0.0, freed_w)
            if deficit_w <= 0.0:
                break

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

    ev_ext_paused = (
        state.ev_external_override_until is not None
        and snapshot.now < state.ev_external_override_until
    )
    heater_ext_paused = (
        state.heater_external_override_until is not None
        and snapshot.now < state.heater_external_override_until
    )
    pool_ext_paused = (
        state.pool_external_override_until is not None
        and snapshot.now < state.pool_external_override_until
    )

    def _release_disallowed_solar_aux() -> None:
        """Turn off solar-owned aux loads that became blocked or disabled."""
        if (
            snapshot.heater_on
            and state.heater_solar_owned
            and rec_heater_on
            and not state.heater_quarantined
            and not heater_ext_paused
        ):
            if _is_blocked("heater"):
                _aux_turn_off("heater", "heater_blocked", cooldown=False, handoff=True)
                reasons.append("heater_blocked_off")
            elif not config.heater_enabled:
                _aux_turn_off("heater", "heater_disabled", cooldown=False, handoff=True)
                reasons.append("heater_disabled_off")
        if (
            snapshot.pool_on
            and state.pool_solar_owned
            and rec_pool_on
            and not state.pool_quarantined
            and not pool_ext_paused
        ):
            if _is_blocked("pool"):
                _aux_turn_off("pool", "pool_blocked", cooldown=False, handoff=True)
                reasons.append("pool_blocked_off")
            elif not config.pool_enabled:
                _aux_turn_off("pool", "pool_disabled", cooldown=False, handoff=True)
                reasons.append("pool_disabled_off")
            elif not pool_in_season:
                _aux_turn_off("pool", "pool_out_of_season", cooldown=False, handoff=True)
                reasons.append("pool_out_of_season_off")
            elif state.pool_manual_blocked_today:
                _aux_turn_off("pool", "pool_manual_blocked_today", cooldown=False, handoff=True)
                reasons.append("pool_manual_blocked_off")
        if (
            not pool_in_season
            and not snapshot.pool_on
            and state.pool_solar_owned
            and _is_load_active("pool")
        ):
            state.pool_solar_owned = False

    def _handle_stale_ev_fallback(in_mod: bool) -> None:
        nonlocal rec_ev_amps
        reasons.append("ev_telemetry_stale")
        if ev_stale_safe_fallback and in_mod and snapshot.ev_charging and not ev_ext_paused:
            if snapshot.ev_min_amps_a > config.ev_min_amps:
                rec_ev_amps = config.ev_min_amps
                _emit(
                    LoadAction(
                        target="ev_min_amps",
                        action="set",
                        value=config.ev_min_amps,
                        reason="ev_stale_safe_fallback",
                    )
                )
                _mark_decrease("ev")
                _ev_cmd_mark()
                reasons.append("ev_stale_drop_to_min")
            else:
                reasons.append("ev_stale_safe_hold")

    if effective_mode == OperatingMode.MANUAL_TOTAL:
        reasons.append("manual_total_active_zero_commands")
    elif effective_mode == OperatingMode.PAUSED:
        reasons.append("mode_paused")
        _release_disallowed_solar_aux()
        _shed_solar_aux("paused")
        _ensure_trydan_safe("paused_safe_fallback")
    elif effective_mode == OperatingMode.HOUSE_PRIORITY:
        reasons.append("mode_house_priority")
        _release_disallowed_solar_aux()
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
        _release_disallowed_solar_aux()
        _ensure_trydan_safe("degraded_safe_fallback")
        if (
            snapshot.grid_power_w is not None
            and snapshot.grid_power_w > config.import_shed_threshold_w
        ):
            _shed_solar_aux("degraded_grid_import", force_safety=True)
            reasons.append("degraded_shed_aux")
        elif safe_hold:
            # Surplus that justified solar-owned loads can no longer be verified: release
            # them (respecting cycle locks / shower veto) instead of running blind for hours.
            _shed_solar_aux("safe_hold_unverified_surplus")
            reasons.append("safe_hold_shed_aux")
    elif effective_mode == OperatingMode.FORCE_EV:
        reasons.append("mode_force_ev")
        _release_disallowed_solar_aux()
        ev_full = snapshot.ev_soc is not None and snapshot.ev_soc >= config.ev_target_soc
        if ev_full:
            reasons.append("ev_target_soc_reached")
            _ensure_trydan_safe("ev_target_soc_reached")
        elif _is_blocked("ev") and not ev_ext_paused and not state.ev_quarantined:
            reasons.append("ev_blocked")
            _ensure_trydan_safe("ev_blocked")
        elif snapshot.ev_connected and not _is_blocked("ev") and not state.ev_quarantined:
            # Setpoint follows the common ICP headroom (export is not credited): ev_max_amps
            # alone (32 A ≈ 7.1 kW) exceeds typical contracted power.
            ev_draw_w = snapshot.ev_power_w if (snapshot.ev_charging and ev_fresh) else 0.0
            room_real_w = _icp_room_w(include_reserved=False)
            room_w = _icp_room_w()
            in_modulated = bool(snapshot.ev_mode and "minima" in snapshot.ev_mode.lower())
            wants_mode = bool(snapshot.ev_mode) and not in_modulated
            if room_w is not None:
                ev_target_w = config.ev_max_amps * voltage_v
                ev_needed_delta_w = max(0.0, ev_target_w - ev_draw_w)
                if wants_mode or snapshot.ev_min_amps_a < config.ev_max_amps:
                    deficit_w = ev_needed_delta_w + config.step_up_margin_w - room_w
                else:
                    deficit_w = 0.0
                if room_real_w is not None and room_real_w < 0.0:
                    deficit_w = max(deficit_w, -room_real_w)
                _shed_aux_for_icp_deficit(deficit_w, "force_ev_priority")
            settle_ok = (
                state.last_ev_command_at is None
                or (snapshot.now - state.last_ev_command_at).total_seconds()
                >= config.settle_seconds
            )
            stop_elapsed = (
                state.ev_stopped_at is None
                or (snapshot.now - state.ev_stopped_at).total_seconds()
                >= config.ev_min_stop_seconds
            )
            real_icp_over = room_real_w is not None and room_real_w < 0.0
            down_cap_a = (
                math.floor((ev_draw_w + room_real_w) / voltage_v)
                if real_icp_over and room_real_w is not None
                else None
            )
            if down_cap_a is not None and down_cap_a < config.ev_min_amps:
                # Even min amps do not fit under the ICP: fall back to solar-only mode.
                rec_ev_amps = config.ev_min_amps
                _ensure_trydan_safe("force_ev_icp_overload")
                _ev_cmd_mark(stopped=in_modulated)
                reasons.append("force_ev_paused_icp_overload")
            elif down_cap_a is not None and down_cap_a < snapshot.ev_min_amps_a:
                # Step-downs are safety-relevant: immediate, no settle wait.
                rec_ev_amps = down_cap_a
                _emit(
                    LoadAction(
                        target="ev_min_amps",
                        action="set",
                        value=down_cap_a,
                        reason="force_ev_icp_cap",
                    )
                )
                _mark_decrease("ev")
                _ev_cmd_mark()
                reasons.append(f"force_ev_icp_step_down:{down_cap_a}A")
            elif not ev_fresh:
                _handle_stale_ev_fallback(in_modulated)
            elif not in_modulated and not stop_elapsed:
                rec_ev_amps = config.ev_min_amps
                reasons.append("force_ev_waiting_min_stop")
            else:
                up_cap_a = (
                    config.ev_max_amps
                    if room_w is None
                    else min(
                        config.ev_max_amps,
                        math.floor((ev_draw_w + room_w - config.step_up_margin_w) / voltage_v),
                    )
                )
                wants_mode = bool(snapshot.ev_mode) and not in_modulated
                wants_up = up_cap_a > snapshot.ev_min_amps_a
                if (wants_mode or wants_up) and tick["decreased"]:
                    reasons.append("force_ev_deferred_after_shed")
                else:
                    if wants_mode:
                        _emit(
                            LoadAction(
                                target="ev_mode",
                                action="set",
                                value=TRYDAN_MODE_MODULATED,
                                reason="force_ev",
                            )
                        )
                        _ev_cmd_mark()
                    if wants_up and (settling_rem_s > 0 or not settle_ok):
                        reasons.append("force_ev_step_up_settling")
                    elif wants_up:
                        rec_ev_amps = up_cap_a
                        _emit(
                            LoadAction(
                                target="ev_min_amps",
                                action="set",
                                value=up_cap_a,
                                reason="force_ev",
                            ),
                            nominal_delta_w=(up_cap_a - snapshot.ev_min_amps_a) * voltage_v,
                        )
                        _ev_cmd_mark()
    else:
        is_modulated_mode = effective_mode == OperatingMode.AUTO_EV_MODULATED
        in_modulated = bool(snapshot.ev_mode and "minima" in snapshot.ev_mode.lower())
        ev_wants_solar = (
            snapshot.ev_connected
            and not _is_blocked("ev")
            and not state.ev_quarantined
            and (snapshot.ev_soc is None or snapshot.ev_soc < config.ev_target_soc)
        )
        settle_ok = (
            state.last_ev_command_at is None
            or (snapshot.now - state.last_ev_command_at).total_seconds() >= config.settle_seconds
        )
        grid_import_w = max(0.0, snapshot.grid_power_w or 0.0)
        bat_discharge_w = max(0.0, snapshot.battery_power_w or 0.0)
        aux_over_budget = (
            grid_import_w > config.import_shed_threshold_w
            or bat_discharge_w > config.battery_discharge_shed_w
        )

        target_heater_temp = (
            config.heater_target_temp_c + config.extra_shower_temp_boost_c
            if extra_shower_active
            else config.heater_target_temp_c
        )
        heater_temp_ok = (
            snapshot.heater_temp_c is not None and snapshot.heater_temp_c >= target_heater_temp
        )
        heater_controllable = (
            not _is_blocked("heater") and not state.heater_quarantined and not heater_ext_paused
        )
        heater_can_turn_off_opt = (
            not shower_veto_active
            and not _is_protected("heater")
            and _cycle_lock_remaining("heater") == 0
        )
        pool_cooldown_active = (
            state.pool_cooldown_until is not None and snapshot.now < state.pool_cooldown_until
        )
        pool_quota_exhausted = state.pool_minutes_today >= config.pool_daily_max_minutes
        pool_controllable = (
            pool_in_season
            and not _is_blocked("pool")
            and not state.pool_quarantined
            and not pool_ext_paused
        )
        pool_can_turn_off_opt = not _is_protected("pool") and _cycle_lock_remaining("pool") == 0

        # ================= Phase 1: every decrease (safety first) =================
        _release_disallowed_solar_aux()
        # Real (measured, not virtual) ICP overload: cut solar-owned aux loads right away,
        # bypassing cycle locks and shower veto — the main breaker must never trip.
        if (
            snapshot.grid_power_w is not None
            and config.contracted_power_w > 0
            and snapshot.grid_power_w > config.contracted_power_w - config.icp_margin_w
        ):
            _shed_solar_aux("icp_overload", force_safety=True)
            reasons.append("icp_overload_shed_aux")

        ev_increase_kind: str | None = None
        if effective_mode in (OperatingMode.AUTO_SOLAR, OperatingMode.OBSERVING):
            _ensure_trydan_safe("auto_solar_single_loop_mode_3")
            reasons.append("trydan_local_fv_exclusivo")
        elif is_modulated_mode:
            emergency_import = grid_import_w >= config.emergency_import_w or icp_low
            if ev_ext_paused:
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
                            _mark_decrease("ev")
                            _ev_cmd_mark(stopped=True)
                            reasons.append("ev_safety_paused_to_mode_3")
                        else:
                            rec_ev_amps = config.ev_min_amps
                            reasons.append("ev_hold_min_run_6a")
                    else:
                        rec_ev_amps = next_a
                        _emit(
                            LoadAction(
                                target="ev_min_amps",
                                action="set",
                                value=next_a,
                                reason="safety_import_reduction",
                            )
                        )
                        _mark_decrease("ev")
                        _ev_cmd_mark()
                        reasons.append(f"ev_safety_step_down:{next_a}A")
                else:
                    reasons.append("ev_waiting_settle_before_drop")
            elif not ev_fresh:
                _handle_stale_ev_fallback(in_modulated)
            elif not in_modulated:
                stop_elapsed = (
                    state.ev_stopped_at is None
                    or (snapshot.now - state.ev_stopped_at).total_seconds()
                    >= config.ev_min_stop_seconds
                )
                if stop_elapsed:
                    ev_increase_kind = "start"
            else:
                ev_increase_kind = "step_up"

        # Heater decreases (1054 W real + shower veto M02 + extra shower M03)
        if not (snapshot.heater_on and state.heater_solar_owned):
            state.heater_shed_since = None
        if (
            heater_controllable
            and snapshot.heater_on
            and state.heater_solar_owned
            and rec_heater_on
        ):
            # Lower-priority solar-owned pool still on: shed that first.
            lower_aux_still_on = (
                pool_in_season
                and snapshot.pool_on
                and state.pool_solar_owned
                and rec_pool_on
                and _outranks("heater", "pool")
            )
            if heater_temp_ok and heater_can_turn_off_opt:
                _aux_turn_off("heater", "heater_target_temp", cooldown=False, handoff=True)
                reasons.append("heater_reached_target_temp")
            elif aux_over_budget and heater_can_turn_off_opt and not handoff_active:
                # Surplus vanished (clouds): a solar-started heater must not keep drawing
                # ~1 kW from grid or house battery until it reaches temperature.
                if lower_aux_still_on:
                    reasons.append("heater_shed_deferred_lower_priority_on")
                elif state.heater_shed_since is None:
                    state.heater_shed_since = snapshot.now
                    reasons.append("heater_shed_holding")
                elif (
                    snapshot.now - state.heater_shed_since
                ).total_seconds() >= config.aux_shed_hold_seconds:
                    _aux_turn_off("heater", "heater_grid_or_bat", cooldown=False, handoff=True)
                    reasons.append("heater_shed_grid_or_bat")
            elif not aux_over_budget:
                state.heater_shed_since = None

        # Pool decreases (250 W, daily quota M07)
        if not snapshot.pool_on:
            state.aux_shed_since = None
        elif pool_controllable and rec_pool_on:
            if pool_quota_exhausted and not _is_protected("pool"):
                _aux_turn_off("pool", "pool_daily_quota", cooldown=False, handoff=False)
                reasons.append("pool_daily_quota_reached")
            elif state.pool_solar_owned and pool_can_turn_off_opt:
                lower_aux_still_on = (
                    snapshot.heater_on
                    and state.heater_solar_owned
                    and rec_heater_on
                    and _outranks("pool", "heater")
                )
                if aux_over_budget:
                    if lower_aux_still_on:
                        reasons.append("pool_shed_deferred_lower_priority_on")
                    elif state.aux_shed_since is None:
                        state.aux_shed_since = snapshot.now
                        reasons.append("pool_shed_holding")
                    elif (
                        snapshot.now - state.aux_shed_since
                    ).total_seconds() >= config.aux_shed_hold_seconds:
                        _aux_turn_off("pool", "pool_grid_or_bat", cooldown=True, handoff=False)
                        reasons.append("pool_shed_grid_or_bat")
                else:
                    state.aux_shed_since = None

        # ============ Phase 2/3: priority-ordered allocation of increases ============
        def _ev_auto_reclaim_w() -> float:
            """EV draw the Trydan gives back by itself (solar-following above its floor)."""
            if not ev_fresh or not snapshot.ev_charging:
                return 0.0
            floor_w = snapshot.ev_min_amps_a * voltage_v if in_modulated else 0.0
            return max(0.0, snapshot.ev_power_w - floor_w)

        def _ev_cmd_reclaim_w() -> float:
            """EV floor above min amps that can be released with an explicit step-down."""
            if not (
                is_modulated_mode
                and in_modulated
                and snapshot.ev_charging
                and settle_ok
                and not ev_ext_paused
                and _mirrors("ev")
            ):
                return 0.0
            return max(0.0, (snapshot.ev_min_amps_a - config.ev_min_amps) * voltage_v)

        def _aux_preemptible_w(load_id: str) -> float:
            """Draw of a solar-owned aux load that may be switched off for a higher one."""
            if not _mirrors(load_id):
                return 0.0
            if load_id == "heater":
                if (
                    heater_controllable
                    and snapshot.heater_on
                    and rec_heater_on
                    and state.heater_solar_owned
                    and heater_can_turn_off_opt
                ):
                    return _effective_heater_draw_w(snapshot, config, state)
                return 0.0
            if (
                pool_controllable
                and snapshot.pool_on
                and rec_pool_on
                and state.pool_solar_owned
                and pool_can_turn_off_opt
            ):
                return (
                    snapshot.pool_power_w
                    if snapshot.pool_power_w is not None
                    else config.pool_nominal_w
                )
            return 0.0

        def _reclaim_from_lower(load_id: str) -> tuple[float, list[tuple[str, float]]]:
            """(auto-reclaim W, [(victim, W)] lowest priority first) below load_id."""
            auto_w = 0.0
            victims: list[tuple[str, float]] = []
            for other in sorted(_FLEX_LOADS, key=_priority_rank, reverse=True):
                if other == load_id or not _outranks(load_id, other):
                    continue
                if other == "ev":
                    auto_w += _ev_auto_reclaim_w()
                    freed_w = _ev_cmd_reclaim_w()
                else:
                    freed_w = _aux_preemptible_w(other)
                if freed_w > 0.0:
                    victims.append((other, freed_w))
            return auto_w, victims

        def _available_surplus_w() -> float:
            """Shared surplus budget (export + eligible battery reclaim - reservations - grants)."""
            return usable_surplus_w - float(tick["granted_w"])

        def _candidate(load_id: str) -> dict[str, object] | None:
            """Eligible increase for load_id with its budget, requirement and power delta."""
            spare_w = _available_surplus_w()
            if load_id == "heater":
                if not (
                    heater_controllable
                    and config.heater_enabled
                    and not snapshot.heater_on
                    and not heater_temp_ok
                ):
                    return None
                auto_w, victims = _reclaim_from_lower("heater")
                return {
                    "own": spare_w,
                    "auto": auto_w,
                    "victims": victims,
                    "req": config.heater_start_export_w,
                    "delta": config.heater_nominal_w,
                }
            if load_id == "pool":
                if not (
                    pool_controllable
                    and config.pool_enabled
                    and not snapshot.pool_on
                    and not state.pool_manual_blocked_today
                    and not pool_cooldown_active
                    and not pool_quota_exhausted
                    and bat_discharge_w < config.battery_discharge_shed_w
                ):
                    return None
                auto_w, victims = _reclaim_from_lower("pool")
                return {
                    "own": spare_w,
                    "auto": auto_w,
                    "victims": victims,
                    "req": config.pool_start_export_w,
                    "delta": config.pool_nominal_w,
                }
            if ev_increase_kind is None:
                return None
            auto_w, victims = _reclaim_from_lower("ev")
            ev_spare_w = ev_budget_w - float(tick["granted_w"])
            if ev_increase_kind == "start":
                start_a = min(
                    config.ev_max_amps,
                    max(config.ev_min_amps, math.floor(ev_spare_w / voltage_v)),
                    config.ev_max_start_amps,
                )
                return {
                    "own": ev_spare_w,
                    "auto": auto_w,
                    "victims": victims,
                    "req": config.ev_start_amps * voltage_v + config.step_up_margin_w,
                    "delta": start_a * voltage_v,
                    "amps": start_a,
                }
            own_target_a = min(
                config.ev_max_amps,
                math.floor((ev_spare_w - config.step_up_margin_w) / voltage_v),
            )
            step_a = min(config.ev_max_step_up_amps, own_target_a - snapshot.ev_min_amps_a)
            if step_a <= 0:
                pot_target_a = min(
                    config.ev_max_amps,
                    math.floor(
                        (ev_spare_w + sum(w for _, w in victims) - config.step_up_margin_w)
                        / voltage_v
                    ),
                )
                step_a = min(config.ev_max_step_up_amps, pot_target_a - snapshot.ev_min_amps_a)
                if step_a <= 0:
                    return None
            return {
                "own": spare_w,
                "auto": auto_w,
                "victims": victims,
                "req": step_a * voltage_v + config.step_up_margin_w,
                "delta": step_a * voltage_v,
                "amps": snapshot.ev_min_amps_a + step_a,
            }

        def _fire_increase(load_id: str, cand: dict[str, object]) -> None:
            nonlocal rec_ev_amps
            delta_w = float(cand["delta"])  # type: ignore[arg-type]
            if load_id == "heater":
                _aux_turn_on("heater", "heater_surplus", delta_w)
                reasons.append("heater_started")
                return
            if load_id == "pool":
                _aux_turn_on("pool", "pool_surplus", delta_w)
                reasons.append("pool_started")
                return
            amps = int(cand["amps"])  # type: ignore[call-overload]
            rec_ev_amps = amps
            if ev_increase_kind == "start":
                _emit(
                    LoadAction(
                        target="ev_min_amps", action="set", value=amps, reason="ev_start_surplus"
                    ),
                    nominal_delta_w=delta_w,
                )
                _emit(
                    LoadAction(
                        target="ev_mode",
                        action="set",
                        value=TRYDAN_MODE_MODULATED,
                        reason="ev_start_surplus",
                    )
                )
                _ev_cmd_mark(started=True)
                reasons.append(f"ev_started:{amps}A")
            else:
                _emit(
                    LoadAction(
                        target="ev_min_amps",
                        action="set",
                        value=amps,
                        reason="ev_surplus_step_up",
                    ),
                    nominal_delta_w=delta_w,
                )
                _ev_cmd_mark()
                reasons.append(f"ev_step_up:{amps}A")
            if _mirrors("ev"):
                tick["granted_w"] = float(tick["granted_w"]) + delta_w

        def _preempt(victim: str, for_load: str) -> None:
            nonlocal rec_ev_amps
            reason = f"{victim}_yield_to_{for_load}"
            if victim == "pool":
                _aux_turn_off("pool", reason, cooldown=True, handoff=True)
            elif victim == "heater":
                _aux_turn_off("heater", reason, cooldown=False, handoff=True)
            else:
                rec_ev_amps = config.ev_min_amps
                _emit(
                    LoadAction(
                        target="ev_min_amps", action="set", value=config.ev_min_amps, reason=reason
                    )
                )
                _mark_decrease("ev")
                _ev_cmd_mark()
            reasons.append(reason)

        hold_attr = {
            "ev": "ev_step_up_since",
            "heater": "heater_export_since",
            "pool": "pool_export_since",
        }
        hold_s = {
            "ev": config.step_up_hold_seconds,
            "heater": config.heater_hold_seconds,
            "pool": config.pool_hold_seconds,
        }
        holding_reason = {
            "ev": "ev_start_holding" if ev_increase_kind == "start" else "ev_step_up_holding",
            "heater": "heater_export_holding",
            "pool": "pool_export_holding",
        }

        if handoff_active or settling_rem_s > 0:
            # Measurements still include a pending change: no hold may accumulate on them,
            # except the winner of an active preemption handoff so it starts on the next
            # eligible tick without a second full hold.
            for lid, attr in hold_attr.items():
                if not (handoff_active and lid == state.handoff_preempt_winner):
                    setattr(state, attr, None)
        else:
            blocked_by: str | None = None
            for load_id in sorted(_FLEX_LOADS, key=_priority_rank):
                attr = hold_attr[load_id]
                cand = _candidate(load_id)
                if cand is None:
                    setattr(state, attr, None)
                    continue
                victims: list[tuple[str, float]] = cand["victims"]  # type: ignore[assignment]
                own_w = float(cand["own"]) + float(cand["auto"])  # type: ignore[arg-type]
                req_w = float(cand["req"])  # type: ignore[arg-type]
                if own_w + sum(w for _, w in victims) < req_w:
                    setattr(state, attr, None)
                    continue
                if blocked_by is not None:
                    # A higher-priority load is competing for this power: it goes first.
                    setattr(state, attr, None)
                    reasons.append(f"{load_id}_waits_for_priority:{blocked_by}")
                    continue
                real_contender = _mirrors(load_id)
                since = getattr(state, attr)
                if since is None or (snapshot.now - since).total_seconds() < hold_s[load_id]:
                    if since is None:
                        setattr(state, attr, snapshot.now)
                    reasons.append(holding_reason[load_id])
                    if real_contender:
                        blocked_by = load_id
                    continue
                if own_w >= req_w:
                    gate = (
                        "decrease_this_tick"
                        if tick["decreased"]
                        else "icp_low"
                        if icp_low
                        else "ev_settle"
                        if load_id == "ev" and not settle_ok
                        else "icp_headroom"
                        if not _headroom_ok(float(cand["delta"]))  # type: ignore[arg-type]
                        else None
                    )
                    if gate is not None:
                        reasons.append(f"{load_id}_increase_deferred:{gate}")
                        if real_contender:
                            blocked_by = load_id
                        continue
                    setattr(state, attr, None)
                    _fire_increase(load_id, cand)
                    continue
                # Power exists only if a lower-priority load yields: shed now, start on a
                # later tick (never increase and decrease in the same tick).
                if not real_contender:
                    continue
                blocked_by = load_id
                if tick["decreased"] or float(tick["granted_w"]) > 0.0:
                    reasons.append(f"{load_id}_preempt_deferred")
                    continue
                needed_w = req_w - own_w
                preempted_aux = False
                for victim, freed_w in victims:
                    if needed_w <= 0.0:
                        break
                    _preempt(victim, load_id)
                    if victim in ("pool", "heater") and _is_load_active(victim):
                        preempted_aux = True
                    needed_w -= freed_w
                if preempted_aux:
                    state.handoff_preempt_winner = load_id

    # BYD in home with sun & unplugged alert (V3-M04)
    if snapshot.ev_at_home:
        if state.ev_at_home_since is None:
            state.ev_at_home_since = snapshot.now
    else:
        state.ev_at_home_since = None

    day_key = snapshot.calendar_time.date().isoformat()
    nag_muted = (
        config.ev_nag_muted_until is not None and snapshot.now < config.ev_nag_muted_until
    ) or (state.ev_nag_sent_day == day_key)
    byd_unplugged_alert = (
        snapshot.ev_at_home
        and state.ev_at_home_since is not None
        and (snapshot.now - state.ev_at_home_since).total_seconds()
        >= config.byd_unplugged_hold_seconds
        and not snapshot.ev_connected
        and not degraded
        and usable_surplus_w >= config.byd_unplugged_min_surplus_w
        and snapshot.ev_soc is not None
        and snapshot.ev_soc < config.byd_unplugged_max_soc
        and not nag_muted
    )
    if byd_unplugged_alert:
        alerts.append(
            AlertItem(
                alert_id="byd_unplugged_surplus",
                severity="info",
                title="Enchufa el BYD para aprovechar el excedente",
                detail=(
                    f"Coche en casa con {snapshot.ev_soc:.0f}% SoC y "
                    f"{round(usable_surplus_w)} W de sol sin usar."
                ),
                target="ev",
                bypass_quiet_hours=False,
            )
        )

    # Storm alert (V3-M05) & climate advice (V3-M08)
    storm_alert = _evaluate_storm_alert(snapshot)
    if usable_surplus_w >= config.climate_advice_on_surplus_w and not degraded:
        state.climate_advice_active = True
    elif usable_surplus_w < config.climate_advice_off_surplus_w or degraded:
        state.climate_advice_active = False

    climate_advice: str | None = None
    if (
        state.climate_advice_active
        and snapshot.outdoor_temp_c is not None
        and (
            snapshot.outdoor_temp_c > config.climate_advice_hot_temp_c
            or snapshot.outdoor_temp_c < config.climate_advice_cold_temp_c
        )
    ):
        climate_advice = (
            f"Buen momento para climatizar comedor ({round(usable_surplus_w)} W solares, "
            f"ext {snapshot.outdoor_temp_c:.1f}°C)"
        )

    if effective_mode == OperatingMode.MANUAL_TOTAL:
        traffic_light = GlobalTrafficLight.MANUAL
    elif effective_mode == OperatingMode.OBSERVING:
        traffic_light = GlobalTrafficLight.SHADOW
    elif icp_low or safe_hold:
        traffic_light = GlobalTrafficLight.CRITICAL
    elif degraded or (icp_ratio_pct is not None and icp_ratio_pct >= config.icp_warning_ratio_pct):
        traffic_light = GlobalTrafficLight.WARNING
    else:
        traffic_light = GlobalTrafficLight.OPTIMAL

    (
        savings_today,
        savings_month,
        import_cost_today,
        import_cost_month,
        export_rev_today,
        export_rev_month,
        net_cost_today,
        net_cost_month,
        savings_formula,
    ) = _update_savings(snapshot, config, state)
    battery_advice = _build_battery_advice(snapshot, config)
    ev_commitment = _compute_ev_commitment(snapshot, config)

    washer_suggestion: str | None = None
    if (
        not degraded
        and not icp_low
        and usable_surplus_w >= config.washer_min_surplus_w
        and (icp_headroom_w is None or icp_headroom_w >= config.washer_min_headroom_w)
    ):
        washer_suggestion = (
            f"Buen momento para lavadora o lavavajillas ({round(usable_surplus_w)} W de excedente)"
        )

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
                else "🚿 Ducha detectada / Veto activo"
                if shower_veto_active
                else "Reposo termostato (<100 W)"
                if state.heater_thermostat_idle
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
                "out_of_season"
                if not pool_in_season
                else "quarantined"
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
                f"Fuera de temporada ({config.pool_season_start} a {config.pool_season_end})"
                if not pool_in_season
                else "En cuarentena por discrepancia de retorno"
                if state.pool_quarantined
                else (
                    f"Cuota diaria {int(state.pool_minutes_today)}/"
                    f"{config.pool_daily_max_minutes} min"
                )
            ),
            power_w=snapshot.pool_power_w,
            min_on_remaining_s=pool_min_on_rem if pool_in_season else 0,
            protected=_is_protected("pool") if pool_in_season else False,
            blocked=_is_blocked("pool") if pool_in_season else False,
            quarantined=state.pool_quarantined if pool_in_season else False,
        ),
    )

    why_by_device = {
        "house": "Prioridad #1 inamovible: el consumo doméstico nunca se recorta.",
        "ev": (
            "Modo Sombra: proponiendo sin actuar."
            if not _is_load_active("ev")
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

    heater_draw = _effective_heater_draw_w(snapshot, config, state)
    mode_prefix = (
        "Observing (dry-run)" if effective_mode == OperatingMode.OBSERVING else effective_mode.value
    )
    pool_summary = (
        f"Pool {'on' if rec_pool_on else 'off'} "
        f"({int(state.pool_minutes_today)}/{config.pool_daily_max_minutes} min)"
        if pool_in_season
        else "Pool out_of_season"
    )
    summary = (
        f"{mode_prefix}: surplus {round(usable_surplus_w)} W | "
        f"EV {rec_ev_amps} A | "
        f"Heater {'on' if rec_heater_on else 'off'} ({round(heater_draw)} W) | "
        f"{pool_summary}"
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
        pool_headline = (
            f"Depuradora {'ON' if rec_pool_on else 'OFF'}"
            if pool_in_season
            else "Depuradora fuera de temporada"
        )
        human_headline = (
            f"Excedente {round(usable_surplus_w)} W · VE {rec_ev_amps} A · "
            f"Termo {'ON' if rec_heater_on else 'OFF'} · "
            f"{pool_headline}"
        )

    effective_load_modes = (
        dict(config.load_modes)
        if config.load_modes is not None
        else {"pool": "active", "heater": "active", "ev": "active"}
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
            grid_import_cost_today_eur=import_cost_today,
            grid_import_cost_month_eur=import_cost_month,
            grid_export_revenue_today_eur=export_rev_today,
            grid_export_revenue_month_eur=export_rev_month,
            net_cost_today_eur=net_cost_today,
            net_cost_month_eur=net_cost_month,
            savings_formula=savings_formula,
            freshness_by_source=freshness_by_source,
            heater_thermostat_idle=state.heater_thermostat_idle,
            shower_veto_active=shower_veto_active,
            shower_veto_until=state.shower_veto_until,
            extra_shower_active=extra_shower_active,
            byd_unplugged_surplus_alert=byd_unplugged_alert,
            storm_alert=storm_alert,
            climate_advice=climate_advice,
            cloud_pocket_used_kwh_today=state.cloud_pocket_used_kwh_today,
            cloud_pocket_episode_kwh=state.cloud_pocket_episode_kwh,
            load_modes=effective_load_modes,
            pool_in_season=pool_in_season,
        ),
        state,
    )
