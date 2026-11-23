"""Pure Python data models for Solar Excess Orchestrator (zero Home Assistant dependencies)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .const import (
    DEFAULT_AUX_SHED_HOLD_SECONDS,
    DEFAULT_BATTERY_DISCHARGE_SHED_W,
    DEFAULT_BATTERY_SLACK_W,
    DEFAULT_BOOT_GRACE_SECONDS,
    DEFAULT_CONTRACTED_POWER_W,
    DEFAULT_EMERGENCY_IMPORT_W,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_EV_MIN_RUN_SECONDS,
    DEFAULT_EV_MIN_STOP_SECONDS,
    DEFAULT_EV_START_AMPS,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_EXPORT_COMP_EUR,
    DEFAULT_HEATER_HOLD_SECONDS,
    DEFAULT_HEATER_MIN_OFF_SECONDS,
    DEFAULT_HEATER_MIN_ON_SECONDS,
    DEFAULT_HEATER_NOMINAL_W,
    DEFAULT_HEATER_START_EXPORT_W,
    DEFAULT_HEATER_TARGET_TEMP,
    DEFAULT_HOUSE_SOC_FLOOR,
    DEFAULT_ICP_MARGIN_W,
    DEFAULT_IMPORT_SHED_THRESHOLD_W,
    DEFAULT_NOMINAL_VOLTAGE,
    DEFAULT_POOL_COOLDOWN_MINUTES,
    DEFAULT_POOL_DAILY_MAX_MINUTES,
    DEFAULT_POOL_HOLD_SECONDS,
    DEFAULT_POOL_MIN_OFF_SECONDS,
    DEFAULT_POOL_MIN_ON_SECONDS,
    DEFAULT_POOL_NOMINAL_W,
    DEFAULT_POOL_START_EXPORT_W,
    DEFAULT_PRIORITY_ORDER,
    DEFAULT_SETTLE_SECONDS,
    DEFAULT_SETTLING_LOCK_SECONDS,
    DEFAULT_STALE_TIMEOUT_SECONDS,
    DEFAULT_STEP_UP_HOLD_SECONDS,
    DEFAULT_STEP_UP_MARGIN_W,
    DEFAULT_TARIFF_P1_EUR,
    DEFAULT_TARIFF_P2_EUR,
    DEFAULT_TARIFF_P3_EUR,
    GlobalTrafficLight,
    OperatingMode,
    ProfileName,
    SafetyState,
    TelemetrySource,
)


@dataclass(frozen=True, slots=True)
class EnergySnapshot:
    """Point-in-time telemetry snapshot across inverter, EV charger, car, and loads."""

    now: datetime
    solar_power_w: float | None = None
    load_power_w: float | None = None
    grid_power_w: float | None = None  # + importing, - exporting
    battery_power_w: float | None = None  # - charging, + discharging
    battery_soc: float | None = None
    local_sample_time: datetime | None = None
    telemetry_source: TelemetrySource = TelemetrySource.MODBUS

    ev_connected: bool = False
    ev_charging: bool = False
    ev_power_w: float = 0.0
    ev_current_a: float = 0.0
    ev_min_amps_a: int = 6
    ev_mode: str = ""
    ev_soc: float | None = None

    heater_on: bool = False
    heater_temp_c: float | None = None
    heater_power_w: float | None = None

    pool_on: bool = False
    pool_power_w: float | None = None

    solar_sample_time: datetime | None = None
    battery_sample_time: datetime | None = None
    ev_sample_time: datetime | None = None
    solar_forecast_today_kwh: float | None = None
    solar_forecast_tomorrow_kwh: float | None = None
    tariff_price_eur: float | None = None
    tariff_period: str | None = None
    critical_sensor_invalid: bool = False


@dataclass(slots=True)
class OrchestratorConfig:
    """Configurable thresholds and safety invariants."""

    mode: OperatingMode = OperatingMode.OBSERVING
    profile: ProfileName = ProfileName.COMFORT
    stale_timeout_seconds: int = DEFAULT_STALE_TIMEOUT_SECONDS
    house_soc_floor: float = DEFAULT_HOUSE_SOC_FLOOR
    ev_target_soc: float = DEFAULT_EV_TARGET_SOC
    ev_min_amps: int = DEFAULT_EV_MIN_AMPS
    ev_max_amps: int = DEFAULT_EV_MAX_AMPS
    ev_start_amps: int = DEFAULT_EV_START_AMPS
    nominal_voltage_v: float = DEFAULT_NOMINAL_VOLTAGE
    step_up_margin_w: float = DEFAULT_STEP_UP_MARGIN_W
    step_up_hold_seconds: int = DEFAULT_STEP_UP_HOLD_SECONDS
    settle_seconds: int = DEFAULT_SETTLE_SECONDS
    ev_min_run_seconds: int = DEFAULT_EV_MIN_RUN_SECONDS
    ev_min_stop_seconds: int = DEFAULT_EV_MIN_STOP_SECONDS

    heater_enabled: bool = True
    heater_target_temp_c: float = DEFAULT_HEATER_TARGET_TEMP
    heater_nominal_w: float = DEFAULT_HEATER_NOMINAL_W
    heater_start_export_w: float = DEFAULT_HEATER_START_EXPORT_W
    heater_hold_seconds: int = DEFAULT_HEATER_HOLD_SECONDS
    heater_min_on_seconds: int = DEFAULT_HEATER_MIN_ON_SECONDS
    heater_min_off_seconds: int = DEFAULT_HEATER_MIN_OFF_SECONDS

    pool_enabled: bool = True
    pool_nominal_w: float = DEFAULT_POOL_NOMINAL_W
    pool_start_export_w: float = DEFAULT_POOL_START_EXPORT_W
    pool_hold_seconds: int = DEFAULT_POOL_HOLD_SECONDS
    pool_min_on_seconds: int = DEFAULT_POOL_MIN_ON_SECONDS
    pool_min_off_seconds: int = DEFAULT_POOL_MIN_OFF_SECONDS
    pool_daily_max_minutes: int = DEFAULT_POOL_DAILY_MAX_MINUTES
    pool_cooldown_minutes: int = DEFAULT_POOL_COOLDOWN_MINUTES

    import_shed_threshold_w: float = DEFAULT_IMPORT_SHED_THRESHOLD_W
    emergency_import_w: float = DEFAULT_EMERGENCY_IMPORT_W
    aux_shed_hold_seconds: int = DEFAULT_AUX_SHED_HOLD_SECONDS
    battery_slack_w: float = DEFAULT_BATTERY_SLACK_W
    battery_discharge_shed_w: float = DEFAULT_BATTERY_DISCHARGE_SHED_W

    contracted_power_w: float = DEFAULT_CONTRACTED_POWER_W
    icp_margin_w: float = DEFAULT_ICP_MARGIN_W
    boot_grace_seconds: int = DEFAULT_BOOT_GRACE_SECONDS
    settling_lock_seconds: int = DEFAULT_SETTLING_LOCK_SECONDS

    priority_order: tuple[str, ...] = DEFAULT_PRIORITY_ORDER
    protected_loads: tuple[str, ...] = ()
    blocked_loads: tuple[str, ...] = ()

    ev_target_time_hhmm: str = "07:30"
    ev_allow_grid_topup: bool = False
    ev_grid_deadline_hhmm: str = "01:00"
    ev_boost_until: datetime | None = None

    manual_total_until: datetime | None = None
    vacation_until: datetime | None = None

    heater_override: str | None = None  # "on" | "off" | None
    heater_override_until: datetime | None = None
    pool_override: str | None = None  # "on" | "off" | None
    pool_override_until: datetime | None = None

    tariff_mode: str = "manual_20td"  # "manual_20td" | "sensor" | "conservative"
    tariff_p1_eur: float = DEFAULT_TARIFF_P1_EUR
    tariff_p2_eur: float = DEFAULT_TARIFF_P2_EUR
    tariff_p3_eur: float = DEFAULT_TARIFF_P3_EUR
    export_comp_eur: float = DEFAULT_EXPORT_COMP_EUR

    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"


@dataclass(slots=True)
class OrchestratorState:
    """Persisted runtime state across evaluations and restarts."""

    degraded: bool = False
    heater_solar_owned: bool = False
    pool_solar_owned: bool = False
    pool_manual_blocked_today: bool = False
    pool_minutes_today: float = 0.0
    pool_last_tick: datetime | None = None
    pool_last_day: str | None = None
    pool_cooldown_until: datetime | None = None

    last_ev_command_at: datetime | None = None
    ev_started_at: datetime | None = None
    ev_stopped_at: datetime | None = None
    ev_step_up_since: datetime | None = None
    ev_step_down_confirm_count: int = 0

    heater_export_since: datetime | None = None
    pool_export_since: datetime | None = None
    aux_shed_since: datetime | None = None

    handoff_waiting_for_off: str | None = None
    handoff_off_confirmed_at: datetime | None = None

    started_at: datetime | None = None
    degraded_since: datetime | None = None
    settling_until: datetime | None = None
    virtual_reserved_w: float = 0.0

    heater_last_changed_at: datetime | None = None
    pool_last_changed_at: datetime | None = None
    heater_quarantined: bool = False
    pool_quarantined: bool = False
    ev_quarantined: bool = False
    heater_discrepancy_count: int = 0
    pool_discrepancy_count: int = 0

    ev_external_override_until: datetime | None = None
    heater_external_override_until: datetime | None = None
    pool_external_override_until: datetime | None = None

    savings_today_eur: float = 0.0
    savings_month_eur: float = 0.0
    savings_last_tick: datetime | None = None
    savings_last_day: str | None = None
    savings_last_month: str | None = None
    manual_total_expired: bool = False


@dataclass(frozen=True, slots=True)
class LoadAction:
    """Single deterministic actuator intention emitted by the engine."""

    target: str  # "ev_mode", "ev_min_amps", "heater", "pool"
    action: str  # "set", "turn_on", "turn_off"
    value: int | str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AlertItem:
    """Grouped operational or safety alert."""

    alert_id: str
    severity: str  # "info" | "warning" | "critical"
    title: str
    detail: str
    target: str = "system"
    bypass_quiet_hours: bool = False


@dataclass(frozen=True, slots=True)
class FlexLoadStatus:
    """Per-load status, timers, and override info for the radar UI."""

    load_id: str
    name: str
    state: str  # "on" | "off" | "waiting" | "blocked" | "protected" | "quarantined" | "override"
    reason: str
    power_w: float | None
    min_on_remaining_s: int = 0
    min_off_remaining_s: int = 0
    protected: bool = False
    blocked: bool = False
    override_until: datetime | None = None
    quarantined: bool = False


@dataclass(frozen=True, slots=True)
class EvCommitmentStatus:
    """EV departure commitment & boost state."""

    target_soc: float
    target_time_hhmm: str
    allow_grid_topup: bool
    grid_deadline_hhmm: str
    status: str  # "on_track" | "needs_grid" | "at_risk" | "unknown_soc" | "reached"
    boost_active: bool
    boost_until: datetime | None = None
    external_override_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class DispatchPlan:
    """Complete evaluation output for telemetry, UI explanation, and execution."""

    effective_mode: OperatingMode
    degraded: bool
    telemetry_source: TelemetrySource
    usable_surplus_w: float
    ev_budget_w: float
    recommended_ev_amps: int
    recommended_heater_on: bool
    recommended_pool_on: bool
    decision_summary: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    actions: tuple[LoadAction, ...] = field(default_factory=tuple)
    simulated_actions: tuple[LoadAction, ...] = field(default_factory=tuple)
    traffic_light: GlobalTrafficLight = GlobalTrafficLight.SHADOW
    safety_state: SafetyState = SafetyState.OBSERVING
    icp_headroom_w: float | None = None
    icp_load_ratio_pct: float | None = None
    settling_remaining_s: int = 0
    human_headline: str = ""
    why_by_device: dict[str, str] = field(default_factory=dict)
    flex_loads: tuple[FlexLoadStatus, ...] = field(default_factory=tuple)
    ev_commitment: EvCommitmentStatus | None = None
    washer_suggestion: str | None = None
    battery_advice: str | None = None
    alerts: tuple[AlertItem, ...] = field(default_factory=tuple)
    estimated_savings_today_eur: float | None = None
    estimated_savings_month_eur: float | None = None
    savings_formula: str = ""
    freshness_by_source: dict[str, int | None] = field(default_factory=dict)
