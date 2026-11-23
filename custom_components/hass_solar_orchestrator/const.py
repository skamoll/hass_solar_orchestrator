"""Constants for Solar Excess Orchestrator."""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "hass_solar_orchestrator"
PANEL_URL_PATH = "solar-orchestrator"
PANEL_COMPONENT_NAME = "solar-orchestrator-panel"
PANEL_STATIC_URL = "/hass_solar_orchestrator_static"

CONF_SOLAR_POWER_ENTITY = "solar_power_entity"
CONF_LOAD_POWER_ENTITY = "load_power_entity"
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_BATTERY_POWER_ENTITY = "battery_power_entity"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_CLOUD_PV_POWER_ENTITY = "cloud_pv_power_entity"
CONF_CLOUD_GRID_POWER_ENTITY = "cloud_grid_power_entity"
CONF_CLOUD_BATTERY_POWER_ENTITY = "cloud_battery_power_entity"
CONF_CLOUD_BATTERY_SOC_ENTITY = "cloud_battery_soc_entity"

CONF_EV_CONNECTED_ENTITY = "ev_connected_entity"
CONF_EV_CHARGING_ENTITY = "ev_charging_entity"
CONF_EV_POWER_ENTITY = "ev_power_entity"
CONF_EV_CURRENT_ENTITY = "ev_current_entity"
CONF_EV_MIN_CURRENT_ENTITY = "ev_min_current_entity"
CONF_EV_MODE_ENTITY = "ev_mode_entity"
CONF_EV_SOC_ENTITY = "ev_soc_entity"
CONF_EV_MODE_SERVICE = "ev_mode_service"

CONF_HEATER_SWITCH_ENTITY = "heater_switch_entity"
CONF_HEATER_TEMP_ENTITY = "heater_temp_entity"
CONF_HEATER_POWER_ENTITY = "heater_power_entity"

CONF_POOL_SWITCH_ENTITY = "pool_switch_entity"
CONF_POOL_POWER_ENTITY = "pool_power_entity"

CONF_TARIFF_PRICE_ENTITY = "tariff_price_entity"
CONF_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"

CONF_EVAL_INTERVAL_SECONDS = "eval_interval_seconds"
CONF_STALE_TIMEOUT_SECONDS = "stale_timeout_seconds"
CONF_HOUSE_SOC_FLOOR = "house_soc_floor"
CONF_EV_TARGET_SOC = "ev_target_soc"
CONF_EV_MIN_AMPS = "ev_min_amps"
CONF_EV_MAX_AMPS = "ev_max_amps"
CONF_EV_START_AMPS = "ev_start_amps"
CONF_HEATER_TARGET_TEMP = "heater_target_temp"
CONF_HEATER_START_EXPORT_W = "heater_start_export_w"
CONF_POOL_START_EXPORT_W = "pool_start_export_w"
CONF_POOL_DAILY_MAX_MINUTES = "pool_daily_max_minutes"
CONF_POOL_COOLDOWN_MINUTES = "pool_cooldown_minutes"
CONF_IMPORT_SHED_THRESHOLD_W = "import_shed_threshold_w"
CONF_BATTERY_SLACK_W = "battery_slack_w"
CONF_CONTRACTED_POWER_W = "contracted_power_w"
CONF_ICP_MARGIN_W = "icp_margin_w"
CONF_BOOT_GRACE_SECONDS = "boot_grace_seconds"
CONF_SETTLING_LOCK_SECONDS = "settling_lock_seconds"

DEFAULT_EVAL_INTERVAL_SECONDS = 10
DEFAULT_STALE_TIMEOUT_SECONDS = 30
DEFAULT_HOUSE_SOC_FLOOR = 20
DEFAULT_EV_TARGET_SOC = 90
DEFAULT_EV_MIN_AMPS = 6
DEFAULT_EV_MAX_AMPS = 32
DEFAULT_EV_START_AMPS = 7
DEFAULT_NOMINAL_VOLTAGE = 222.0
DEFAULT_STEP_UP_MARGIN_W = 150.0
DEFAULT_STEP_UP_HOLD_SECONDS = 30
DEFAULT_SETTLE_SECONDS = 10
DEFAULT_EV_MIN_RUN_SECONDS = 180
DEFAULT_EV_MIN_STOP_SECONDS = 180
DEFAULT_HEATER_TARGET_TEMP = 45.0
DEFAULT_HEATER_NOMINAL_W = 2000.0
DEFAULT_HEATER_START_EXPORT_W = 2000.0
DEFAULT_HEATER_HOLD_SECONDS = 300
DEFAULT_HEATER_MIN_ON_SECONDS = 600
DEFAULT_HEATER_MIN_OFF_SECONDS = 300
DEFAULT_POOL_NOMINAL_W = 250.0
DEFAULT_POOL_START_EXPORT_W = 1050.0
DEFAULT_POOL_HOLD_SECONDS = 300
DEFAULT_POOL_MIN_ON_SECONDS = 600
DEFAULT_POOL_MIN_OFF_SECONDS = 600
DEFAULT_POOL_DAILY_MAX_MINUTES = 360
DEFAULT_POOL_COOLDOWN_MINUTES = 60
DEFAULT_IMPORT_SHED_THRESHOLD_W = 300.0
DEFAULT_EMERGENCY_IMPORT_W = 1000.0
DEFAULT_AUX_SHED_HOLD_SECONDS = 600
DEFAULT_BATTERY_SLACK_W = 0.0
DEFAULT_BATTERY_DISCHARGE_SHED_W = 100.0
DEFAULT_CONTRACTED_POWER_W = 5750.0
DEFAULT_ICP_MARGIN_W = 500.0
DEFAULT_BOOT_GRACE_SECONDS = 30
DEFAULT_SETTLING_LOCK_SECONDS = 15
DEFAULT_MANUAL_TOTAL_MINUTES = 60
DEFAULT_EXTERNAL_OVERRIDE_MINUTES = 30
DEFAULT_TARIFF_P1_EUR = 0.24
DEFAULT_TARIFF_P2_EUR = 0.16
DEFAULT_TARIFF_P3_EUR = 0.10
DEFAULT_EXPORT_COMP_EUR = 0.06
DEFAULT_PRIORITY_ORDER: tuple[str, ...] = ("house", "ev", "heater", "pool")
MAX_BLACKBOX_ENTRIES = 200

FORBIDDEN_ENTITY_PATTERNS: tuple[str, ...] = (
    r".*general.*",
    r".*(?:^|[._-])icp(?:$|[._-]).*",
    r".*diferencial.*",
    r".*inverter.*",
    r".*inversor.*",
    r".*(?:^|[._-])bms(?:$|[._-]).*",
    r".*(?:^|[._-])byd(?:$|[._-]).*",
    r".*frigorifico.*",
    r".*nevera.*",
    r".*router.*",
    r".*server.*",
)


class OperatingMode(StrEnum):
    """Top-level orchestrator operating modes."""

    OBSERVING = "observing"
    AUTO_SOLAR = "auto_solar"
    AUTO_EV_MODULATED = "auto_ev_modulated"
    FORCE_EV = "force_ev"
    HOUSE_PRIORITY = "house_priority"
    PAUSED = "paused"
    MANUAL_TOTAL = "manual_total"


class ProfileName(StrEnum):
    """One-touch user profiles."""

    SAVING = "saving"
    COMFORT = "comfort"
    FAST_CHARGE = "fast_charge"
    VACATION = "vacation"


class SafetyState(StrEnum):
    """Safety state machine states for the power arbiter."""

    BOOT_GRACE = "boot_grace"
    OBSERVING = "observing"
    NORMAL = "normal"
    DEGRADED = "degraded"
    SAFE_HOLD = "safe_hold"
    MANUAL_TOTAL = "manual_total"


class GlobalTrafficLight(StrEnum):
    """Global status indicator for the sidebar panel."""

    OPTIMAL = "optimal"
    WARNING = "warning"
    CRITICAL = "critical"
    SHADOW = "shadow"
    MANUAL = "manual"


class TelemetrySource(StrEnum):
    """Active telemetry source."""

    MODBUS = "modbus"
    CLOUD_FALLBACK = "cloud_fallback"
    UNAVAILABLE = "unavailable"
