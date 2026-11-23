"""Pure Python data models for Solar Excess Orchestrator (zero Home Assistant dependencies)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .const import (
    DEFAULT_AUX_SHED_HOLD_SECONDS,
    DEFAULT_BATTERY_DISCHARGE_SHED_W,
    DEFAULT_BATTERY_SLACK_W,
    DEFAULT_EMERGENCY_IMPORT_W,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_EV_MIN_RUN_SECONDS,
    DEFAULT_EV_MIN_STOP_SECONDS,
    DEFAULT_EV_START_AMPS,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_HEATER_HOLD_SECONDS,
    DEFAULT_HEATER_NOMINAL_W,
    DEFAULT_HEATER_START_EXPORT_W,
    DEFAULT_HEATER_TARGET_TEMP,
    DEFAULT_HOUSE_SOC_FLOOR,
    DEFAULT_IMPORT_SHED_THRESHOLD_W,
    DEFAULT_NOMINAL_VOLTAGE,
    DEFAULT_POOL_COOLDOWN_MINUTES,
    DEFAULT_POOL_DAILY_MAX_MINUTES,
    DEFAULT_POOL_HOLD_SECONDS,
    DEFAULT_POOL_NOMINAL_W,
    DEFAULT_POOL_START_EXPORT_W,
    DEFAULT_SETTLE_SECONDS,
    DEFAULT_STALE_TIMEOUT_SECONDS,
    DEFAULT_STEP_UP_HOLD_SECONDS,
    DEFAULT_STEP_UP_MARGIN_W,
    OperatingMode,
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


@dataclass(slots=True)
class OrchestratorConfig:
    """Configurable thresholds and safety invariants."""

    mode: OperatingMode = OperatingMode.OBSERVING
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

    pool_enabled: bool = True
    pool_nominal_w: float = DEFAULT_POOL_NOMINAL_W
    pool_start_export_w: float = DEFAULT_POOL_START_EXPORT_W
    pool_hold_seconds: int = DEFAULT_POOL_HOLD_SECONDS
    pool_daily_max_minutes: int = DEFAULT_POOL_DAILY_MAX_MINUTES
    pool_cooldown_minutes: int = DEFAULT_POOL_COOLDOWN_MINUTES

    import_shed_threshold_w: float = DEFAULT_IMPORT_SHED_THRESHOLD_W
    emergency_import_w: float = DEFAULT_EMERGENCY_IMPORT_W
    aux_shed_hold_seconds: int = DEFAULT_AUX_SHED_HOLD_SECONDS
    battery_slack_w: float = DEFAULT_BATTERY_SLACK_W
    battery_discharge_shed_w: float = DEFAULT_BATTERY_DISCHARGE_SHED_W


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


@dataclass(frozen=True, slots=True)
class LoadAction:
    """Single deterministic actuator intention emitted by the engine."""

    target: str  # "ev_mode", "ev_min_amps", "heater", "pool"
    action: str  # "set", "turn_on", "turn_off"
    value: int | str | None = None
    reason: str = ""


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
