"""Home Assistant coordinator for Solar Excess Orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, valid_entity_id
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_SLACK_W,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BOOT_GRACE_SECONDS,
    CONF_CLIMATE_ENTITY,
    CONF_CLOUD_BATTERY_POWER_ENTITY,
    CONF_CLOUD_BATTERY_SOC_ENTITY,
    CONF_CLOUD_GRID_POWER_ENTITY,
    CONF_CLOUD_PV_POWER_ENTITY,
    CONF_CONTRACTED_POWER_W,
    CONF_EV_CHARGING_ENTITY,
    CONF_EV_CONNECTED_ENTITY,
    CONF_EV_CURRENT_ENTITY,
    CONF_EV_MAX_AMPS,
    CONF_EV_MIN_AMPS,
    CONF_EV_MIN_CURRENT_ENTITY,
    CONF_EV_MODE_ENTITY,
    CONF_EV_MODE_SERVICE,
    CONF_EV_POWER_ENTITY,
    CONF_EV_SOC_ENTITY,
    CONF_EV_START_AMPS,
    CONF_EV_TARGET_SOC,
    CONF_EV_TRACKER_ENTITY,
    CONF_EVAL_INTERVAL_SECONDS,
    CONF_GRID_POWER_ENTITY,
    CONF_HEATER_DAILY_WATER_ENTITY,
    CONF_HEATER_FLOW_ENTITY,
    CONF_HEATER_POWER_ENTITY,
    CONF_HEATER_START_EXPORT_W,
    CONF_HEATER_SWITCH_ENTITY,
    CONF_HEATER_TARGET_TEMP,
    CONF_HEATER_TEMP_ENTITY,
    CONF_HOUSE_SOC_FLOOR,
    CONF_ICP_MARGIN_W,
    CONF_IMPORT_SHED_THRESHOLD_W,
    CONF_LOAD_POWER_ENTITY,
    CONF_POOL_COOLDOWN_MINUTES,
    CONF_POOL_DAILY_MAX_MINUTES,
    CONF_POOL_POWER_ENTITY,
    CONF_POOL_START_EXPORT_W,
    CONF_POOL_SWITCH_ENTITY,
    CONF_SETTLING_LOCK_SECONDS,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_POWER_ENTITY,
    CONF_STALE_TIMEOUT_SECONDS,
    CONF_TARIFF_PRICE_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULT_BATTERY_SLACK_W,
    DEFAULT_BOOT_GRACE_SECONDS,
    DEFAULT_CONTRACTED_POWER_W,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_EV_START_AMPS,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_EVAL_INTERVAL_SECONDS,
    DEFAULT_EXTERNAL_OVERRIDE_MINUTES,
    DEFAULT_HEATER_START_EXPORT_W,
    DEFAULT_HEATER_TARGET_TEMP,
    DEFAULT_HOUSE_SOC_FLOOR,
    DEFAULT_ICP_MARGIN_W,
    DEFAULT_IMPORT_SHED_THRESHOLD_W,
    DEFAULT_MANUAL_TOTAL_MINUTES,
    DEFAULT_POOL_COOLDOWN_MINUTES,
    DEFAULT_POOL_DAILY_MAX_MINUTES,
    DEFAULT_POOL_START_EXPORT_W,
    DEFAULT_SETTLING_LOCK_SECONDS,
    DEFAULT_STALE_TIMEOUT_SECONDS,
    DOMAIN,
    MAX_BLACKBOX_ENTRIES,
    OperatingMode,
    ProfileName,
    TelemetrySource,
)
from .engine import (
    TRYDAN_MODE_SOLAR_ONLY,
    SecurityDenylistViolation,
    evaluate,
    evaluate_profile_preview,
    is_forbidden_entity,
)
from .models import (
    CONF_POOL_SEASON_ENABLED,
    CONF_POOL_SEASON_END,
    CONF_POOL_SEASON_START,
    DEFAULT_POOL_SEASON_ENABLED,
    DEFAULT_POOL_SEASON_END,
    DEFAULT_POOL_SEASON_START,
    DispatchPlan,
    EnergySnapshot,
    LoadAction,
    OrchestratorConfig,
    OrchestratorState,
    is_pool_in_season,
    is_valid_month_day,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
_CHECKPOINT_SECONDS = 300
_WEATHER_CACHE_TTL_SECONDS = 1800.0
_WEATHER_CACHE_MAX_AGE_SECONDS = 21600.0
_WEATHER_BACKOFF_BASE_SECONDS = 60.0
_WEATHER_BACKOFF_MAX_SECONDS = 1800.0
_LOADS = ("ev", "heater", "pool")

# Past-event timestamps in OrchestratorState: persisted across restarts and
# validated on restore (parse errors or future timestamps are dropped).
_PAST_TIMESTAMP_STATE_FIELDS: tuple[str, ...] = (
    "pool_last_tick",
    "last_ev_command_at",
    "ev_started_at",
    "ev_stopped_at",
    "ev_step_up_since",
    "heater_export_since",
    "pool_export_since",
    "aux_shed_since",
    "heater_shed_since",
    "handoff_off_confirmed_at",
    "degraded_since",
    "ev_stale_since",
    "heater_last_changed_at",
    "pool_last_changed_at",
    "savings_last_tick",
    "heater_low_power_since",
    "shower_flow_since",
    "shower_veto_started_at",
    "ev_at_home_since",
    "cloud_pocket_episode_started_at",
    "cloud_pocket_last_tick",
)

# Future-expiration (*_until) timestamps in OrchestratorState: persisted across
# restarts and validated on restore (parse errors are dropped).
_UNTIL_TIMESTAMP_STATE_FIELDS: tuple[str, ...] = (
    "pool_cooldown_until",
    "settling_until",
    "ev_external_override_until",
    "heater_external_override_until",
    "pool_external_override_until",
    "shower_veto_until",
)

# Boot-scoped timestamps intentionally reset on restart (e.g. BOOT_GRACE).
_TRANSIENT_TIMESTAMP_STATE_FIELDS: tuple[str, ...] = ("started_at",)
_CLOCK_SETTINGS = (
    "quiet_hours_start",
    "quiet_hours_end",
    "ev_target_time_hhmm",
    "ev_grid_deadline_hhmm",
)
_BOOL_SETTINGS = (
    "heater_enabled",
    "pool_enabled",
    "pool_season_enabled",
    "quiet_hours_enabled",
    "ev_allow_grid_topup",
)
_MONTH_DAY_SETTINGS = ("pool_season_start", "pool_season_end")
_NUMBER_SETTINGS = {
    "contracted_power_w": (1000, math.inf),
    "icp_margin_w": (0, math.inf),
    "house_soc_floor": (0, 100),
    "ev_target_soc": (20, 100),
    "ev_max_amps": (6, 32),
    "battery_slack_w": (0, 600),
    "heater_target_temp_c": (30, 75),
    "pool_daily_max_minutes": (0, 720),
    **dict.fromkeys(
        ("tariff_p1_eur", "tariff_p2_eur", "tariff_p3_eur", "export_comp_eur"),
        (-math.inf, math.inf),
    ),
}


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return dt_util.as_utc(datetime.fromisoformat(raw))
    except (TypeError, ValueError, OverflowError):
        return None


class SolarOrchestratorCoordinator(DataUpdateCoordinator[DispatchPlan]):
    """Coordinates snapshot acquisition, pure engine evaluation, and serialized execution."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        interval_s = int(
            entry.options.get(CONF_EVAL_INTERVAL_SECONDS, DEFAULT_EVAL_INTERVAL_SECONDS)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=interval_s),
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}"
        )
        self._exec_lock = asyncio.Lock()
        self._pending_actions: deque[LoadAction] = deque()
        self._queue_generation = 0
        self._next_checkpoint = 0.0
        self._checkpoint_pending = False
        self._urgent_save = False
        self._degraded_since: datetime | None = None
        self.config = self._build_config()
        self._mode_before_boost: OperatingMode | None = None
        self.runtime_state = OrchestratorState(started_at=dt_util.utcnow())
        self.blackbox: deque[dict[str, Any]] = deque(maxlen=MAX_BLACKBOX_ENTRIES)
        self._last_snapshot: EnergySnapshot | None = None
        self._expected_heater_on: bool | None = None
        self._expected_pool_on: bool | None = None
        self._weather_cache: list[dict[str, Any]] = []
        self._weather_cache_at: datetime | None = None
        self._weather_failures: int = 0
        self._weather_next_retry_at: datetime | None = None
        self._weather_last_error: str | None = None
        self._weather_last_error_at: datetime | None = None
        self._active_conflicts: list[dict[str, Any]] = []
        if self.config.load_modes is None:
            self.config.load_modes = {"pool": "observing", "heater": "observing", "ev": "observing"}

    def _build_config(self) -> OrchestratorConfig:
        opts = self.entry.options
        return OrchestratorConfig(
            mode=OperatingMode(opts.get("mode", OperatingMode.OBSERVING)),
            stale_timeout_seconds=int(
                opts.get(CONF_STALE_TIMEOUT_SECONDS, DEFAULT_STALE_TIMEOUT_SECONDS)
            ),
            house_soc_floor=float(opts.get(CONF_HOUSE_SOC_FLOOR, DEFAULT_HOUSE_SOC_FLOOR)),
            ev_target_soc=float(opts.get(CONF_EV_TARGET_SOC, DEFAULT_EV_TARGET_SOC)),
            ev_min_amps=int(opts.get(CONF_EV_MIN_AMPS, DEFAULT_EV_MIN_AMPS)),
            ev_max_amps=int(opts.get(CONF_EV_MAX_AMPS, DEFAULT_EV_MAX_AMPS)),
            ev_start_amps=int(opts.get(CONF_EV_START_AMPS, DEFAULT_EV_START_AMPS)),
            heater_enabled=bool(opts.get("heater_enabled", True)),
            heater_target_temp_c=float(
                opts.get(CONF_HEATER_TARGET_TEMP, DEFAULT_HEATER_TARGET_TEMP)
            ),
            heater_start_export_w=float(
                opts.get(CONF_HEATER_START_EXPORT_W, DEFAULT_HEATER_START_EXPORT_W)
            ),
            pool_enabled=bool(opts.get("pool_enabled", True)),
            pool_start_export_w=float(
                opts.get(CONF_POOL_START_EXPORT_W, DEFAULT_POOL_START_EXPORT_W)
            ),
            pool_daily_max_minutes=int(
                opts.get(CONF_POOL_DAILY_MAX_MINUTES, DEFAULT_POOL_DAILY_MAX_MINUTES)
            ),
            pool_cooldown_minutes=int(
                opts.get(CONF_POOL_COOLDOWN_MINUTES, DEFAULT_POOL_COOLDOWN_MINUTES)
            ),
            pool_season_enabled=bool(
                opts.get(CONF_POOL_SEASON_ENABLED, DEFAULT_POOL_SEASON_ENABLED)
            ),
            pool_season_start=(
                str(opts[CONF_POOL_SEASON_START]).strip()
                if is_valid_month_day(opts.get(CONF_POOL_SEASON_START))
                else DEFAULT_POOL_SEASON_START
            ),
            pool_season_end=(
                str(opts[CONF_POOL_SEASON_END]).strip()
                if is_valid_month_day(opts.get(CONF_POOL_SEASON_END))
                else DEFAULT_POOL_SEASON_END
            ),
            import_shed_threshold_w=float(
                opts.get(CONF_IMPORT_SHED_THRESHOLD_W, DEFAULT_IMPORT_SHED_THRESHOLD_W)
            ),
            battery_slack_w=float(opts.get(CONF_BATTERY_SLACK_W, DEFAULT_BATTERY_SLACK_W)),
            contracted_power_w=float(opts.get(CONF_CONTRACTED_POWER_W, DEFAULT_CONTRACTED_POWER_W)),
            icp_margin_w=float(opts.get(CONF_ICP_MARGIN_W, DEFAULT_ICP_MARGIN_W)),
            boot_grace_seconds=int(opts.get(CONF_BOOT_GRACE_SECONDS, DEFAULT_BOOT_GRACE_SECONDS)),
            settling_lock_seconds=int(
                opts.get(CONF_SETTLING_LOCK_SECONDS, DEFAULT_SETTLING_LOCK_SECONDS)
            ),
        )

    def _record_blackbox(
        self,
        event_type: str,
        message: str,
        *,
        target: str = "system",
        executed: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": dt_util.utcnow().isoformat(),
            "mode": self.config.mode.value,
            "type": event_type,
            "target": target,
            "message": message,
            "executed": executed,
        }
        if extra:
            entry["extra"] = extra
        self.blackbox.appendleft(entry)
        if executed or event_type in (
            "mode_change",
            "denylist_block",
            "external_override",
            "quarantine",
            "conflict",
            "override",
        ):
            self._urgent_save = True

    async def async_load_state(self) -> None:
        """Load persisted counters, ownership flags, Blackbox, and mode."""
        raw = await self._store.async_load()
        if not isinstance(raw, dict) or not raw:
            self.runtime_state.started_at = dt_util.utcnow()
            return
        if "mode" in raw:
            with contextlib.suppress(ValueError):
                self.config.mode = OperatingMode(raw["mode"])
        if "profile" in raw:
            with contextlib.suppress(ValueError):
                self.config.profile = ProfileName(raw["profile"])
        settings = {
            key: raw[key]
            for key in (
                *_NUMBER_SETTINGS,
                *_BOOL_SETTINGS,
                *_CLOCK_SETTINGS,
                *_MONTH_DAY_SETTINGS,
                "tariff_mode",
            )
            if key in raw
        }
        try:
            self._apply_settings(settings)
        except ServiceValidationError as err:
            self.config.mode = OperatingMode.OBSERVING
            _LOGGER.warning(
                "Stored settings invalid, using entry defaults in observing mode: %s", err
            )

        if "priority_order" in raw and isinstance(raw["priority_order"], list):
            ordered_loads = [load for load in raw["priority_order"] if load in _LOADS]
            self.config.priority_order = ("house", *dict.fromkeys((*ordered_loads, *_LOADS)))
        if "protected_loads" in raw and isinstance(raw["protected_loads"], list):
            self.config.protected_loads = tuple(
                dict.fromkeys(x for x in raw["protected_loads"] if x in _LOADS)
            )
        if "blocked_loads" in raw and isinstance(raw["blocked_loads"], list):
            self.config.blocked_loads = tuple(
                dict.fromkeys(x for x in raw["blocked_loads"] if x in _LOADS)
            )

        self.config.manual_total_until = _parse_dt(raw.get("manual_total_until"))
        self.config.ev_boost_until = _parse_dt(raw.get("ev_boost_until"))
        self.config.vacation_until = _parse_dt(raw.get("vacation_until"))
        self.config.extra_shower_until = _parse_dt(raw.get("extra_shower_until"))
        self.config.ev_nag_muted_until = _parse_dt(raw.get("ev_nag_muted_until"))
        if self.config.ev_boost_until and self.config.mode == OperatingMode.FORCE_EV:
            try:
                self._mode_before_boost = OperatingMode(
                    raw.get("mode_before_boost") or OperatingMode.AUTO_EV_MODULATED
                )
            except ValueError:
                self._mode_before_boost = OperatingMode.OBSERVING
        for load in ("heater", "pool"):
            override = raw.get(f"{load}_override")
            until = _parse_dt(raw.get(f"{load}_override_until"))
            if override in ("on", "off") and until:
                setattr(self.config, f"{load}_override", override)
                setattr(self.config, f"{load}_override_until", until)
        if isinstance(raw.get("load_modes"), dict):
            self.config.load_modes = {
                k: "active" if raw["load_modes"].get(k) == "active" else "observing" for k in _LOADS
            }

        now = dt_util.utcnow()
        restored_timestamps: dict[str, datetime | None] = {}
        for key in _PAST_TIMESTAMP_STATE_FIELDS:
            dt_val = _parse_dt(raw.get(key))
            if dt_val is not None and dt_val > now:
                _LOGGER.warning("Dropping future stored timestamp for %s: %s", key, dt_val)
                dt_val = None
            restored_timestamps[key] = dt_val
        for key in _UNTIL_TIMESTAMP_STATE_FIELDS:
            restored_timestamps[key] = _parse_dt(raw.get(key))

        self.runtime_state = OrchestratorState(
            started_at=now,
            heater_solar_owned=raw.get("heater_solar_owned") is True,
            pool_solar_owned=raw.get("pool_solar_owned") is True,
            pool_manual_blocked_today=bool(raw.get("pool_manual_blocked_today", False)),
            pool_minutes_today=_finite_float(raw.get("pool_minutes_today")) or 0.0,
            pool_last_day=raw.get("pool_last_day"),
            heater_quarantined=bool(raw.get("heater_quarantined", False)),
            pool_quarantined=bool(raw.get("pool_quarantined", False)),
            ev_quarantined=bool(raw.get("ev_quarantined", False)),
            savings_today_eur=_finite_float(raw.get("savings_today_eur")) or 0.0,
            savings_month_eur=_finite_float(raw.get("savings_month_eur")) or 0.0,
            grid_import_cost_today_eur=_finite_float(raw.get("grid_import_cost_today_eur")) or 0.0,
            grid_import_cost_month_eur=_finite_float(raw.get("grid_import_cost_month_eur")) or 0.0,
            grid_export_revenue_today_eur=_finite_float(raw.get("grid_export_revenue_today_eur"))
            or 0.0,
            grid_export_revenue_month_eur=_finite_float(raw.get("grid_export_revenue_month_eur"))
            or 0.0,
            savings_last_day=raw.get("savings_last_day"),
            savings_last_month=raw.get("savings_last_month"),
            heater_seen_active_draw=raw.get("heater_seen_active_draw") is True,
            handoff_waiting_for_off=raw.get("handoff_waiting_for_off")
            if raw.get("handoff_waiting_for_off") in ("pool", "heater")
            else None,
            **restored_timestamps,
        )
        self._expected_heater_on = (
            raw.get("expected_heater_on")
            if isinstance(raw.get("expected_heater_on"), bool)
            else None
        )
        self._expected_pool_on = (
            raw.get("expected_pool_on") if isinstance(raw.get("expected_pool_on"), bool) else None
        )
        history = raw.get("blackbox")
        for item in reversed(history[:MAX_BLACKBOX_ENTRIES] if isinstance(history, list) else []):
            if isinstance(item, dict):
                self.blackbox.appendleft(item)
        self._record_blackbox("startup", "Integración iniciada en periodo BOOT_GRACE")

    async def async_save_state(self) -> None:
        """Persist runtime counters, settings, and rotating local Blackbox."""
        payload = {
            "mode": self.config.mode.value,
            "profile": self.config.profile.value,
            "heater_enabled": self.config.heater_enabled,
            "pool_enabled": self.config.pool_enabled,
            "house_soc_floor": self.config.house_soc_floor,
            "ev_target_soc": self.config.ev_target_soc,
            "ev_max_amps": self.config.ev_max_amps,
            "heater_target_temp_c": self.config.heater_target_temp_c,
            "pool_daily_max_minutes": self.config.pool_daily_max_minutes,
            "pool_season_enabled": self.config.pool_season_enabled,
            "pool_season_start": self.config.pool_season_start,
            "pool_season_end": self.config.pool_season_end,
            "battery_slack_w": self.config.battery_slack_w,
            "contracted_power_w": self.config.contracted_power_w,
            "icp_margin_w": self.config.icp_margin_w,
            "priority_order": list(self.config.priority_order),
            "protected_loads": list(self.config.protected_loads),
            "blocked_loads": list(self.config.blocked_loads),
            "ev_target_time_hhmm": self.config.ev_target_time_hhmm,
            "ev_allow_grid_topup": self.config.ev_allow_grid_topup,
            "ev_grid_deadline_hhmm": self.config.ev_grid_deadline_hhmm,
            "tariff_mode": self.config.tariff_mode,
            "tariff_p1_eur": self.config.tariff_p1_eur,
            "tariff_p2_eur": self.config.tariff_p2_eur,
            "tariff_p3_eur": self.config.tariff_p3_eur,
            "export_comp_eur": self.config.export_comp_eur,
            "quiet_hours_enabled": self.config.quiet_hours_enabled,
            "quiet_hours_start": self.config.quiet_hours_start,
            "quiet_hours_end": self.config.quiet_hours_end,
            "manual_total_until": (
                self.config.manual_total_until.isoformat()
                if self.config.manual_total_until
                else None
            ),
            "ev_boost_until": (
                self.config.ev_boost_until.isoformat() if self.config.ev_boost_until else None
            ),
            "vacation_until": (
                self.config.vacation_until.isoformat() if self.config.vacation_until else None
            ),
            "extra_shower_until": (
                self.config.extra_shower_until.isoformat()
                if self.config.extra_shower_until
                else None
            ),
            "ev_nag_muted_until": (
                self.config.ev_nag_muted_until.isoformat()
                if self.config.ev_nag_muted_until
                else None
            ),
            "load_modes": dict(self.config.load_modes or {}),
            "heater_solar_owned": self.runtime_state.heater_solar_owned,
            "pool_solar_owned": self.runtime_state.pool_solar_owned,
            "pool_manual_blocked_today": self.runtime_state.pool_manual_blocked_today,
            "pool_minutes_today": round(self.runtime_state.pool_minutes_today, 2),
            "pool_last_day": self.runtime_state.pool_last_day,
            "heater_quarantined": self.runtime_state.heater_quarantined,
            "pool_quarantined": self.runtime_state.pool_quarantined,
            "ev_quarantined": self.runtime_state.ev_quarantined,
            "savings_today_eur": self.runtime_state.savings_today_eur,
            "savings_month_eur": self.runtime_state.savings_month_eur,
            "grid_import_cost_today_eur": self.runtime_state.grid_import_cost_today_eur,
            "grid_import_cost_month_eur": self.runtime_state.grid_import_cost_month_eur,
            "grid_export_revenue_today_eur": self.runtime_state.grid_export_revenue_today_eur,
            "grid_export_revenue_month_eur": self.runtime_state.grid_export_revenue_month_eur,
            "savings_last_day": self.runtime_state.savings_last_day,
            "savings_last_month": self.runtime_state.savings_last_month,
            "blackbox": list(self.blackbox)[:MAX_BLACKBOX_ENTRIES],
        }
        payload["heater_seen_active_draw"] = self.runtime_state.heater_seen_active_draw
        payload["handoff_waiting_for_off"] = self.runtime_state.handoff_waiting_for_off
        payload["mode_before_boost"] = self._mode_before_boost
        payload["expected_heater_on"] = self._expected_heater_on
        payload["expected_pool_on"] = self._expected_pool_on
        for load in ("heater", "pool"):
            payload[f"{load}_override"] = getattr(self.config, f"{load}_override")
            until = getattr(self.config, f"{load}_override_until")
            payload[f"{load}_override_until"] = until.isoformat() if until else None
        for key in (*_PAST_TIMESTAMP_STATE_FIELDS, *_UNTIL_TIMESTAMP_STATE_FIELDS):
            value = getattr(self.runtime_state, key)
            payload[key] = value.isoformat() if value else None
        self._urgent_save = False
        self._checkpoint_pending = False
        self._next_checkpoint = time.monotonic() + _CHECKPOINT_SECONDS
        try:
            await self._store.async_save(payload)
        except Exception:
            self._urgent_save = True
            self._checkpoint_pending = True
            raise

    def _read_float_w(self, entity_id: str | None) -> tuple[float | None, datetime | None]:
        """Read a power sensor safely (never float(x, 0) on unavailable/unknown)."""
        if not entity_id:
            return None, None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", "none", ""):
            return None, None
        val = _finite_float(st.state)
        unit = str(st.attributes.get("unit_of_measurement", "W")).strip()
        if val is None or unit not in ("W", "kW", "w", "kw"):
            return None, None
        if unit.lower() == "kw":
            val *= 1000.0
            if not math.isfinite(val):
                return None, None
        ts = getattr(st, "last_reported", None) or st.last_updated
        return val, ts

    def _read_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", "none", ""):
            return None
        return _finite_float(st.state)

    def _read_bool(self, entity_id: str | None) -> bool:
        if not entity_id:
            return False
        st = self.hass.states.get(entity_id)
        return st is not None and st.state == "on"

    def _read_str(self, entity_id: str | None) -> str:
        if not entity_id:
            return ""
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", "none"):
            return ""
        return st.state

    def _build_snapshot(self) -> EnergySnapshot:
        data = self.entry.data
        now = dt_util.utcnow()

        solar_w, solar_ts = self._read_float_w(data.get(CONF_SOLAR_POWER_ENTITY))
        load_w, _ = self._read_float_w(data.get(CONF_LOAD_POWER_ENTITY))
        grid_w, grid_ts = self._read_float_w(data.get(CONF_GRID_POWER_ENTITY))
        bat_w, bat_ts = self._read_float_w(data.get(CONF_BATTERY_POWER_ENTITY))
        bat_soc = self._read_float(data.get(CONF_BATTERY_SOC_ENTITY))

        def _is_fresh(ts: datetime | None) -> bool:
            return (
                ts is not None
                and 0 <= (now - ts).total_seconds() <= self.config.stale_timeout_seconds
            )

        critical_invalid = any(not _is_fresh(ts) for ts in (grid_ts, solar_ts))
        source = TelemetrySource.MODBUS
        local_ts = grid_ts or solar_ts

        if grid_w is None or solar_w is None:
            cloud_solar_w, cloud_solar_ts = self._read_float_w(data.get(CONF_CLOUD_PV_POWER_ENTITY))
            cloud_grid_w, cloud_grid_ts = self._read_float_w(data.get(CONF_CLOUD_GRID_POWER_ENTITY))
            cloud_bat_w, cloud_bat_ts = self._read_float_w(
                data.get(CONF_CLOUD_BATTERY_POWER_ENTITY)
            )
            cloud_soc = self._read_float(data.get(CONF_CLOUD_BATTERY_SOC_ENTITY))
            fresh_cloud_solar = cloud_solar_w if _is_fresh(cloud_solar_ts) else None
            fresh_cloud_grid = cloud_grid_w if _is_fresh(cloud_grid_ts) else None
            fresh_cloud_bat = cloud_bat_w if _is_fresh(cloud_bat_ts) else None
            if fresh_cloud_grid is not None or fresh_cloud_solar is not None:
                solar_w = solar_w if solar_w is not None else fresh_cloud_solar
                grid_w = grid_w if grid_w is not None else fresh_cloud_grid
                bat_w = bat_w if bat_w is not None else fresh_cloud_bat
                bat_soc = bat_soc if bat_soc is not None else cloud_soc
                source = TelemetrySource.CLOUD_FALLBACK
            else:
                source = TelemetrySource.UNAVAILABLE

        ev_min_raw = self._read_float(data.get(CONF_EV_MIN_CURRENT_ENTITY))
        ev_power_w, ev_ts = self._read_float_w(data.get(CONF_EV_POWER_ENTITY))
        tariff_price = self._read_float(data.get(CONF_TARIFF_PRICE_ENTITY))
        solar_forecast_tomorrow = self._read_float(
            data.get(CONF_SOLAR_FORECAST_ENTITY) or "sensor.sem_forecast_tomorrow_kwh"
        )
        heater_pwr, heater_pwr_ts = self._read_float_w(data.get(CONF_HEATER_POWER_ENTITY))
        heater_flow = self._read_float(
            data.get(CONF_HEATER_FLOW_ENTITY)
            or "sensor.galeria_galeria_sensor_termo_galeria_caudal_instantaneo"
        )
        heater_daily_water = self._read_float(
            data.get(CONF_HEATER_DAILY_WATER_ENTITY)
            or "sensor.galeria_galeria_sensor_termo_agua_consumo_diario"
        )
        tracker_id = data.get(CONF_EV_TRACKER_ENTITY) or "device_tracker.byd_atto_3_ubicacion"
        ev_at_home = self._read_str(tracker_id).lower() == "home"
        weather_id = data.get(CONF_WEATHER_ENTITY) or "weather.forecast_casa"
        w_st = self.hass.states.get(weather_id)
        outdoor_temp = (
            float(w_st.attributes["temperature"])
            if w_st and isinstance(w_st.attributes.get("temperature"), (int, float))
            else None
        )
        climate_id = data.get(CONF_CLIMATE_ENTITY) or "climate.aire_comedor"
        c_st = self.hass.states.get(climate_id)
        indoor_temp = (
            float(c_st.attributes["current_temperature"])
            if c_st and isinstance(c_st.attributes.get("current_temperature"), (int, float))
            else None
        )

        snap = EnergySnapshot(
            now=now,
            calendar_timezone=dt_util.as_local(now).tzinfo,
            solar_power_w=solar_w,
            load_power_w=load_w,
            grid_power_w=grid_w,
            battery_power_w=bat_w,
            battery_soc=bat_soc,
            local_sample_time=local_ts,
            solar_sample_time=solar_ts,
            battery_sample_time=bat_ts,
            ev_sample_time=ev_ts,
            ev_at_home=ev_at_home,
            telemetry_source=source,
            ev_connected=self._read_bool(data.get(CONF_EV_CONNECTED_ENTITY)),
            ev_charging=self._read_bool(data.get(CONF_EV_CHARGING_ENTITY)),
            ev_power_w=ev_power_w or 0.0,
            ev_current_a=self._read_float(data.get(CONF_EV_CURRENT_ENTITY)) or 0.0,
            ev_min_amps_a=int(ev_min_raw) if ev_min_raw is not None else self.config.ev_min_amps,
            ev_mode=self._read_str(data.get(CONF_EV_MODE_ENTITY)),
            ev_soc=self._read_float(data.get(CONF_EV_SOC_ENTITY)),
            heater_on=self._read_bool(data.get(CONF_HEATER_SWITCH_ENTITY)),
            heater_temp_c=self._read_float(data.get(CONF_HEATER_TEMP_ENTITY)),
            heater_power_w=heater_pwr,
            heater_power_sample_time=heater_pwr_ts,
            heater_flow_l_min=heater_flow,
            heater_daily_water_l=heater_daily_water,
            pool_on=self._read_bool(data.get(CONF_POOL_SWITCH_ENTITY)),
            pool_power_w=self._read_float_w(data.get(CONF_POOL_POWER_ENTITY))[0],
            tariff_price_eur=tariff_price,
            solar_forecast_tomorrow_kwh=solar_forecast_tomorrow,
            weather_daily_forecast=(
                tuple(self._weather_cache) if self._weather_cache_is_valid(now) else ()
            ),
            outdoor_temp_c=outdoor_temp,
            indoor_temp_c=indoor_temp,
            critical_sensor_invalid=critical_invalid,
        )
        self._last_snapshot = snap
        return snap

    def _check_w_return_and_external(self, snapshot: EnergySnapshot) -> None:
        """Verify W-return feedback & detect external manual changes (M18/M19)."""
        now = snapshot.now
        if self._expected_heater_on is not None and self._read_str(
            self.entry.data.get(CONF_HEATER_SWITCH_ENTITY)
        ) in ("on", "off"):
            if snapshot.heater_on != self._expected_heater_on:
                self.runtime_state.heater_external_override_until = now + timedelta(
                    minutes=DEFAULT_EXTERNAL_OVERRIDE_MINUTES
                )
                self._expected_heater_on = None
                self.runtime_state.heater_solar_owned = False
                self._record_blackbox(
                    "external_override",
                    "Cambio externo detectado en Termo: control pausado 30 min",
                    target="heater",
                )
            elif (
                not self.runtime_state.heater_thermostat_idle
                and not self.runtime_state.heater_seen_active_draw
                and not self.runtime_state.heater_quarantined
                and snapshot.heater_power_sample_time is not None
                and 0 <= (now - snapshot.heater_power_sample_time).total_seconds() <= 120
                and snapshot.heater_on
                and snapshot.heater_power_w is not None
                and snapshot.heater_power_w < 20.0
                and self.runtime_state.heater_last_changed_at is not None
                and (now - self.runtime_state.heater_last_changed_at).total_seconds() >= 30.0
            ):
                self.runtime_state.heater_discrepancy_count += 1
                if self.runtime_state.heater_discrepancy_count >= 3:
                    self.runtime_state.heater_quarantined = True
                    self._record_blackbox(
                        "quarantine",
                        "Termo en cuarentena: ON sin consumo real en W",
                        target="heater",
                    )
            else:
                self.runtime_state.heater_discrepancy_count = 0

        pool_in_season = is_pool_in_season(snapshot.calendar_time, self.config)
        if not pool_in_season:
            if self._expected_pool_on is not None and not snapshot.pool_on:
                self._expected_pool_on = None
        elif self._expected_pool_on is not None and self._read_str(
            self.entry.data.get(CONF_POOL_SWITCH_ENTITY)
        ) in ("on", "off"):
            if snapshot.pool_on != self._expected_pool_on:
                self.runtime_state.pool_external_override_until = now + timedelta(
                    minutes=DEFAULT_EXTERNAL_OVERRIDE_MINUTES
                )
                self._expected_pool_on = None
                self.runtime_state.pool_solar_owned = False
                self._record_blackbox(
                    "external_override",
                    "Cambio externo detectado en Depuradora: control pausado 30 min",
                    target="pool",
                )
            elif (
                snapshot.pool_on
                and not self.runtime_state.pool_quarantined
                and snapshot.pool_power_w is not None
                and snapshot.pool_power_w < 20.0
                and self.runtime_state.pool_last_changed_at is not None
                and (now - self.runtime_state.pool_last_changed_at).total_seconds() >= 30.0
            ):
                self.runtime_state.pool_discrepancy_count += 1
                if self.runtime_state.pool_discrepancy_count >= 3:
                    self.runtime_state.pool_quarantined = True
                    self._record_blackbox(
                        "quarantine",
                        "Depuradora en cuarentena: ON sin consumo real en W",
                        target="pool",
                    )
            else:
                self.runtime_state.pool_discrepancy_count = 0

    def _validate_target_entity(self, entity_id: str | None) -> None:
        """Enforce immutable anti-cut Denylist before any physical call."""
        if not entity_id or not valid_entity_id(entity_id):
            raise ServiceValidationError("Falta una entidad o servicio válido para la carga.")
        if is_forbidden_entity(entity_id):
            raise SecurityDenylistViolation(
                f"Acción bloqueada por Denylist inmutable sobre entidad prohibida: {entity_id}"
            )

    def purge_pending_actions(self) -> int:
        """Clear any queued actions immediately when entering Sombra or Manual total."""
        self._queue_generation += 1
        count = len(self._pending_actions)
        self._pending_actions.clear()
        return count

    async def _async_execute_actions(
        self,
        actions: tuple[LoadAction, ...],
        on_success: Callable[[LoadAction], None] | None = None,
    ) -> None:
        """Execute emitted actuator actions sequentially under a lock with Denylist guard."""
        if self.config.mode in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL):
            self.purge_pending_actions()
            return
        if not actions:
            return
        generation = self._queue_generation
        data = self.entry.data
        async with self._exec_lock:
            if generation != self._queue_generation:
                return
            self._pending_actions.extend(actions)
            while self._pending_actions:
                if self.config.mode in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL):
                    self.purge_pending_actions()
                    return
                act = self._pending_actions.popleft()
                grp = "ev" if act.target.startswith("ev") else act.target
                if (self.config.load_modes or {}).get(grp, "observing") != "active":
                    self._record_blackbox(
                        "simulated",
                        f"[Carga en Sombra] Omitida orden real {act.target}.{act.action}",
                        target=act.target,
                        executed=False,
                    )
                    continue
                if (
                    act.target == "heater"
                    and act.action == "turn_off"
                    and act.reason not in ("degraded_grid_import", "icp_overload")
                    and self.runtime_state.shower_veto_until is not None
                    and dt_util.utcnow() < self.runtime_state.shower_veto_until
                ):
                    self._record_blackbox(
                        "shower_pattern",
                        "Veto de ducha activo: turn_off retenido en el punto de envío",
                        target="heater",
                        executed=False,
                    )
                    continue
                try:
                    if act.target in ("heater", "pool") and act.action in ("turn_on", "turn_off"):
                        key = (
                            CONF_HEATER_SWITCH_ENTITY
                            if act.target == "heater"
                            else CONF_POOL_SWITCH_ENTITY
                        )
                        entity_id = data.get(key)
                        self._validate_target_entity(entity_id)
                        if not entity_id.startswith("switch."):
                            raise ServiceValidationError("Se requiere una entidad switch.")
                        current = self._read_str(entity_id)
                        if current not in ("on", "off"):
                            raise ServiceValidationError("No consta el estado del interruptor.")
                        if current == ("on" if act.action == "turn_on" else "off"):
                            self._record_blackbox(
                                "action_skipped",
                                "El interruptor ya está en el estado solicitado.",
                                target=act.target,
                            )
                            continue
                        await self.hass.services.async_call(
                            "switch",
                            act.action,
                            {"entity_id": entity_id},
                            blocking=True,
                        )
                        setattr(self, f"_expected_{act.target}_on", act.action == "turn_on")
                    elif act.target == "ev_min_amps" and act.action == "set":
                        entity_id = data.get(CONF_EV_MIN_CURRENT_ENTITY)
                        self._validate_target_entity(entity_id)
                        if (
                            not entity_id.startswith("number.")
                            or not isinstance(act.value, int)
                            or not self.config.ev_min_amps <= act.value <= self.config.ev_max_amps
                        ):
                            raise ServiceValidationError("Entidad o corriente de recarga inválida.")
                        await self.hass.services.async_call(
                            "number",
                            "set_value",
                            {"entity_id": entity_id, "value": act.value},
                            blocking=True,
                        )
                    elif act.target == "ev_mode" and act.action == "set" and act.value is not None:
                        svc = str(data.get(CONF_EV_MODE_SERVICE) or "").strip()
                        self._validate_target_entity(svc)
                        domain_name, service_name = svc.split(".", 1)
                        await self.hass.services.async_call(
                            domain_name,
                            service_name,
                            {"modo": int(act.value)},
                            blocking=True,
                        )
                    else:
                        raise ServiceValidationError("Orden de actuador no válida.")
                    if on_success is not None:
                        on_success(act)
                    self._record_blackbox(
                        "action",
                        f"{act.target}.{act.action}({act.value}) · {act.reason}",
                        target=act.target,
                        executed=True,
                    )
                except SecurityDenylistViolation as err:
                    _LOGGER.error("%s", err)
                    self._record_blackbox("denylist_block", str(err), target=act.target)
                    self.config.mode = OperatingMode.OBSERVING
                    self.purge_pending_actions()
                    return
                except Exception as err:
                    _LOGGER.warning(
                        "Failed orchestrator action %s.%s(%s): %s",
                        act.target,
                        act.action,
                        act.value,
                        err,
                    )
                    self._record_blackbox(
                        "action_error",
                        f"{act.target}.{act.action}: {err}",
                        target=act.target,
                    )

    def _weather_cache_is_valid(self, now: datetime) -> bool:
        """Return True if cached weather forecast is present and within validity."""
        if not self._weather_cache or self._weather_cache_at is None:
            return False
        age = (now - self._weather_cache_at).total_seconds()
        return 0.0 <= age <= _WEATHER_CACHE_MAX_AGE_SECONDS

    async def _async_refresh_weather_cache(self) -> None:
        """Fetch 3-5 day daily weather forecast with 30-min cache and backoff (V3-M05)."""
        now = dt_util.utcnow()
        if (
            self._weather_cache_at is not None
            and (now - self._weather_cache_at).total_seconds() > _WEATHER_CACHE_MAX_AGE_SECONDS
        ):
            self._weather_cache = []
        if (
            self._weather_cache_at is not None
            and 0.0 <= (now - self._weather_cache_at).total_seconds() < _WEATHER_CACHE_TTL_SECONDS
        ):
            return
        if self._weather_next_retry_at is not None and now < self._weather_next_retry_at:
            return
        weather_id = self.entry.data.get(CONF_WEATHER_ENTITY) or "weather.forecast_casa"
        if not self.hass.states.get(weather_id):
            return
        try:
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_id, "type": "daily"},
                blocking=True,
                return_response=True,
            )
            fc = (resp or {}).get(weather_id, {}).get("forecast")
            if not isinstance(fc, list):
                raise ValueError("Invalid weather forecast payload")
        except Exception as err:
            self._weather_failures += 1
            backoff_s = min(
                _WEATHER_BACKOFF_MAX_SECONDS,
                _WEATHER_BACKOFF_BASE_SECONDS * (2 ** (self._weather_failures - 1)),
            )
            self._weather_next_retry_at = now + timedelta(seconds=backoff_s)
            self._weather_last_error = str(err) or type(err).__name__
            self._weather_last_error_at = now
            _LOGGER.warning(
                "Weather forecast fetch failed (attempt %d, retry in %.0fs): %s",
                self._weather_failures,
                backoff_s,
                self._weather_last_error,
            )
            return
        self._weather_cache = fc[:5]
        self._weather_cache_at = now
        self._weather_failures = 0
        self._weather_next_retry_at = None
        self._weather_last_error = None
        self._weather_last_error_at = None

    async def _async_update_data(self) -> DispatchPlan:
        await self._async_refresh_weather_cache()
        self._active_conflicts = self.detect_all_conflicts()
        snapshot = self._build_snapshot()
        if self.config.ev_boost_until and snapshot.now >= self.config.ev_boost_until:
            if self.config.mode == OperatingMode.FORCE_EV and self._mode_before_boost:
                self.config.mode = self._mode_before_boost
            self.config.ev_boost_until = None
            self._mode_before_boost = None
            self._record_blackbox("override", "Carga inmediata del VE finalizada", target="ev")
        self._check_w_return_and_external(snapshot)
        previous_state = self.runtime_state
        plan, next_state = evaluate(snapshot, self.config, previous_state)
        # Publish observations immediately, but commit switch-related transitions only
        # when the corresponding HA service actually returns successfully.
        pending_changes: dict[str, dict[str, Any]] = {}
        for act in plan.actions:
            if act.target not in ("heater", "pool"):
                continue
            keys = [f"{act.target}_solar_owned", f"{act.target}_last_changed_at"]
            if (
                act.target == "pool"
                and next_state.pool_cooldown_until != previous_state.pool_cooldown_until
            ):
                keys.append("pool_cooldown_until")
            if act.action == "turn_off":
                # The engine clears the shed hold timer when it emits the shed; a failed
                # shed must retry on the next tick, not wait a whole new hold period.
                keys.append("heater_shed_since" if act.target == "heater" else "aux_shed_since")
                if next_state.handoff_waiting_for_off == act.target:
                    keys.extend(("handoff_waiting_for_off", "handoff_off_confirmed_at"))
            pending_changes[act.target] = {key: getattr(next_state, key) for key in keys}
            for key in keys:
                setattr(next_state, key, getattr(previous_state, key))
        self.runtime_state = next_state

        def commit_action(act: LoadAction) -> None:
            for key, value in pending_changes.get(act.target, {}).items():
                setattr(next_state, key, value)

        # D1: If Manual total expired, persist transition back to OBSERVING & notify
        if next_state.manual_total_expired:
            self.config.mode = OperatingMode.OBSERVING
            self.config.manual_total_until = None
            self.purge_pending_actions()
            self._record_blackbox(
                "mode_change",
                "Manual total expirado: retorno automático a Modo Sombra (observing)",
            )
            with contextlib.suppress(Exception):
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Solar Orchestrator: Manual total finalizado",
                        "message": (
                            "El temporizador de Manual total ha caducado y el sistema ha vuelto "
                            "por seguridad a Modo Sombra (solo observación)."
                        ),
                        "notification_id": f"{DOMAIN}_manual_expired",
                    },
                    blocking=False,
                )

        if self.config.mode == OperatingMode.OBSERVING and plan.simulated_actions:
            for sim in plan.simulated_actions:
                self._record_blackbox(
                    "simulated",
                    f"[Sombra] Propondría {sim.target}.{sim.action}({sim.value}) · {sim.reason}",
                    target=sim.target,
                    executed=False,
                )

        issue_id = f"degraded_telemetry_{self.entry.entry_id}"
        if plan.degraded:
            if self._degraded_since is None:
                self._degraded_since = snapshot.now
            elif (snapshot.now - self._degraded_since) >= timedelta(minutes=2):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="degraded_telemetry",
                )
        else:
            if self._degraded_since is not None:
                self._degraded_since = None
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

        if plan.actions:
            await self._async_execute_actions(plan.actions, commit_action)

        self._checkpoint_pending = True
        if self._urgent_save or time.monotonic() >= self._next_checkpoint:
            try:
                await self.async_save_state()
            except Exception:
                # Actions already ran; failing the refresh would hide the plan and mark
                # every entity unavailable. Flags stay set, so the next tick retries.
                _LOGGER.exception("Failed to persist orchestrator state; will retry")
        return plan

    async def async_set_mode(
        self, mode: OperatingMode, duration_minutes: int | None = None
    ) -> None:
        """Switch operating mode cleanly, purging queues on Sombra/Manual total."""
        now = dt_util.utcnow()
        self.config.ev_boost_until = None
        self._mode_before_boost = None
        self.config.mode = mode
        if mode == OperatingMode.MANUAL_TOTAL:
            mins = duration_minutes or DEFAULT_MANUAL_TOTAL_MINUTES
            self.config.manual_total_until = now + timedelta(minutes=max(1, min(1440, mins)))
            self.purge_pending_actions()
            self._record_blackbox("mode_change", f"Manual total activado ({mins} min)")
        elif mode == OperatingMode.OBSERVING:
            self.config.manual_total_until = None
            self.purge_pending_actions()
            self._record_blackbox("mode_change", "Modo Sombra activado (cola purgada)")
        else:
            self.config.manual_total_until = None
            self.purge_pending_actions()
            self._record_blackbox("mode_change", f"Modo cambiado a {mode.value}")
        await self.async_save_state()
        await self.async_request_refresh()

    async def async_apply_profile(
        self,
        profile: ProfileName,
        *,
        vacation_until: str | None = None,
        preview_only: bool = False,
    ) -> dict[str, str]:
        """Preview or apply a 1-touch profile (M10)."""
        preview = evaluate_profile_preview(profile, self.config)
        if preview_only:
            return preview

        vacation_end = _parse_dt(vacation_until)
        if profile == ProfileName.VACATION and (
            vacation_end is None or vacation_end <= dt_util.utcnow()
        ):
            raise ServiceValidationError("El perfil Vacaciones exige una fecha de fin.")

        self.config.profile = profile
        if profile == ProfileName.SAVING:
            self.config.house_soc_floor = 30.0
            self.config.ev_allow_grid_topup = False
        elif profile == ProfileName.COMFORT:
            self.config.house_soc_floor = 20.0
            self.config.ev_allow_grid_topup = True
        elif profile == ProfileName.FAST_CHARGE:
            self.config.ev_boost_until = None
            self._mode_before_boost = None
            self.purge_pending_actions()
            self.config.mode = OperatingMode.FORCE_EV
        elif profile == ProfileName.VACATION:
            self.config.heater_enabled = False
            self.config.pool_enabled = True
            self.config.vacation_until = vacation_end

        self._record_blackbox("profile", f"Perfil aplicado: {profile.value}")
        await self.async_save_state()
        await self.async_request_refresh()
        return preview

    async def async_set_priorities(
        self,
        priority_order: list[str],
        protected_loads: list[str] | None = None,
        blocked_loads: list[str] | None = None,
    ) -> None:
        """Update load priority matrix with Casa locked #1 (M11)."""
        if not priority_order or priority_order[0] != "house":
            raise ServiceValidationError("La carga 'house' (Casa) debe permanecer fija en #1.")
        if len(priority_order) != 4 or set(priority_order[1:]) != set(_LOADS):
            raise ServiceValidationError("Las prioridades deben incluir cada carga una sola vez.")
        for group in (protected_loads or [], blocked_loads or []):
            if not set(group) <= set(_LOADS) or len(group) != len(set(group)):
                raise ServiceValidationError("Lista de cargas inválida.")
        self.config.priority_order = tuple(priority_order)
        if protected_loads is not None:
            self.config.protected_loads = tuple(protected_loads)
        if blocked_loads is not None:
            self.config.blocked_loads = tuple(blocked_loads)
        self._record_blackbox("config", f"Prioridades actualizadas: {priority_order}")
        await self.async_save_state()
        await self.async_request_refresh()

    async def async_set_ev_commitment(
        self,
        target_soc: float,
        target_time_hhmm: str,
        allow_grid_topup: bool,
        grid_deadline_hhmm: str,
    ) -> None:
        """Update EV departure commitment atomically (M04)."""
        self._apply_settings(
            {
                "ev_target_soc": target_soc,
                "ev_target_time_hhmm": target_time_hhmm,
                "ev_allow_grid_topup": allow_grid_topup,
                "ev_grid_deadline_hhmm": grid_deadline_hhmm,
            }
        )
        self._record_blackbox(
            "ev_commitment",
            (
                f"Meta VE: {self.config.ev_target_soc:.0f}% a las {target_time_hhmm} "
                f"(red={allow_grid_topup})"
            ),
            target="ev",
        )
        await self.async_save_state()
        await self.async_request_refresh()

    async def async_override_load(
        self, load_id: str, action: str, duration_minutes: int = 60
    ) -> None:
        """Apply expiring 1-touch override or EV boost (M04/M06)."""
        allowed_actions = ("auto", "charge_now") if load_id == "ev" else ("auto", "on", "off")
        if load_id not in _LOADS or action not in allowed_actions:
            raise ServiceValidationError("Acción no válida para la carga indicada.")
        now = dt_util.utcnow()
        until = now + timedelta(minutes=max(5, min(1440, duration_minutes)))
        if load_id == "ev" and action == "charge_now":
            self.config.ev_boost_until = until
            if self.config.mode not in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL):
                if self._mode_before_boost is None:
                    self._mode_before_boost = self.config.mode
                self.config.mode = OperatingMode.FORCE_EV
            self._record_blackbox(
                "override", f"Cargar ahora VE ({duration_minutes} min)", target="ev"
            )
        elif load_id == "ev" and action == "auto":
            self.config.ev_boost_until = None
            if self.config.mode == OperatingMode.FORCE_EV:
                self.config.mode = self._mode_before_boost or OperatingMode.AUTO_EV_MODULATED
            self._mode_before_boost = None
            self._record_blackbox("override", "VE devuelto a automático", target="ev")
        elif load_id == "heater":
            self.config.heater_override = None if action == "auto" else action
            self.config.heater_override_until = None if action == "auto" else until
            self._record_blackbox("override", f"Termo override={action}", target="heater")
        elif load_id == "pool":
            self.config.pool_override = None if action == "auto" else action
            self.config.pool_override_until = None if action == "auto" else until
            self._record_blackbox("override", f"Depuradora override={action}", target="pool")
        await self.async_save_state()
        await self.async_request_refresh()

    def _apply_settings(self, settings: dict[str, Any]) -> None:
        """Validate the whole patch before changing live control parameters."""
        updates: dict[str, Any] = {}
        for key, value in settings.items():
            if key in _NUMBER_SETTINGS:
                low, high = _NUMBER_SETTINGS[key]
                number = _finite_float(value)
                if number is None or not low <= number <= high:
                    raise ServiceValidationError(f"Valor numérico fuera de rango para {key}.")
                if key in ("ev_max_amps", "pool_daily_max_minutes"):
                    if not number.is_integer():
                        raise ServiceValidationError(f"{key} debe ser un entero.")
                    updates[key] = int(number)
                else:
                    updates[key] = number
            elif (
                key in _BOOL_SETTINGS
                and isinstance(value, bool)
                or (
                    key in _CLOCK_SETTINGS
                    and isinstance(value, str)
                    and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
                )
                or (
                    key in _MONTH_DAY_SETTINGS
                    and isinstance(value, str)
                    and is_valid_month_day(value)
                )
                or key == "tariff_mode"
                and value in ("manual_20td", "sensor", "conservative")
            ):
                updates[key] = value.strip() if key in _MONTH_DAY_SETTINGS else value
            else:
                raise ServiceValidationError(f"Ajuste o valor no válido para {key}.")
        if updates.get("icp_margin_w", self.config.icp_margin_w) >= updates.get(
            "contracted_power_w", self.config.contracted_power_w
        ):
            raise ServiceValidationError(
                "El margen ICP debe ser inferior a la potencia contratada."
            )
        if updates.get("ev_max_amps", self.config.ev_max_amps) < self.config.ev_min_amps:
            raise ServiceValidationError("La corriente máxima debe alcanzar el mínimo del VE.")
        for key, value in updates.items():
            setattr(self.config, key, value)

    async def async_save_settings(self, data: dict[str, Any]) -> None:
        """Save tariff, contracted power, ICP margin, and advanced settings (M13/M14)."""
        self._apply_settings(data)
        self._record_blackbox("config", "Ajustes actualizados desde panel")
        await self.async_save_state()
        await self.async_request_refresh()

    async def async_restore_defaults(self, section: str = "all") -> None:
        """Restore recommended safe defaults (M14)."""
        if section in ("all", "safety"):
            self.config.contracted_power_w = DEFAULT_CONTRACTED_POWER_W
            self.config.icp_margin_w = DEFAULT_ICP_MARGIN_W
            self.config.house_soc_floor = DEFAULT_HOUSE_SOC_FLOOR
        if section in ("all", "loads"):
            self.config.ev_max_amps = DEFAULT_EV_MAX_AMPS
            self.config.heater_target_temp_c = DEFAULT_HEATER_TARGET_TEMP
            self.config.pool_daily_max_minutes = DEFAULT_POOL_DAILY_MAX_MINUTES
        self._record_blackbox("config", f"Valores recomendados restaurados ({section})")
        await self.async_save_state()
        await self.async_request_refresh()

    async def async_clear_quarantine(self, load_id: str) -> None:
        """Clear quarantine flag on a load after user inspection."""
        if load_id == "heater":
            self.runtime_state.heater_quarantined = False
            self.runtime_state.heater_discrepancy_count = 0
        elif load_id == "pool":
            self.runtime_state.pool_quarantined = False
            self.runtime_state.pool_discrepancy_count = 0
        elif load_id == "ev":
            self.runtime_state.ev_quarantined = False
        self._record_blackbox(
            "quarantine_cleared", f"Cuarentena limpiada para {load_id}", target=load_id
        )
        await self.async_save_state()
        await self.async_request_refresh()

    def detect_conflicts(self, load_id: str) -> list[dict[str, str]]:
        """Detect enabled HA automations competing for the same load (V3-M10)."""
        patterns = {
            "heater": ("termo", "energia_solar_termo", "termo_garantia_de_ducha"),
            "ev": ("coche_control_continuo", "trydan_alimentar_control"),
            "pool": ("terraza_depuradora", "terraza_control_de_enchufe"),
        }
        keys = patterns.get(load_id, ())
        conflicts: list[dict[str, str]] = []
        for st in self.hass.states.async_all("automation"):
            if st.state != "on":
                continue
            eid = st.entity_id.lower()
            if any(k in eid for k in keys):
                conflicts.append(
                    {
                        "load": load_id,
                        "entity_id": st.entity_id,
                        "alias": str(st.attributes.get("friendly_name", st.entity_id)),
                    }
                )
        return conflicts

    def detect_all_conflicts(self) -> list[dict[str, str]]:
        """Return conflicts across all granular loads and auto-degrade if active."""
        all_c: list[dict[str, str]] = []
        for lid in ("pool", "heater", "ev"):
            c = self.detect_conflicts(lid)
            all_c.extend(c)
            if c and (self.config.load_modes or {}).get(lid) == "active":
                if self.config.load_modes is None:
                    self.config.load_modes = {}
                self.config.load_modes[lid] = "observing"
                self._record_blackbox(
                    "conflict",
                    f"Conflicto detectado en {lid} ({c[0]['entity_id']}): vuelto a observing",
                    target=lid,
                )
        return all_c

    def scan_entities(self) -> dict[str, Any]:
        """Auto-discover candidate HA entities with live validation badges (V3-M09)."""
        role_patterns = {
            "solar_power": ("sungrow", "solar_power", "pv_power"),
            "grid_power": ("sungrow", "grid_power", "red"),
            "battery_soc": ("sungrow", "battery_soc", "bateria"),
            "ev_soc": ("byd_atto_3_nivel_de_bateria", "ev_soc"),
            "ev_connected": ("trydan_conectado", "ev_connected"),
            "ev_min_amps": ("trydan_intensidad_minima", "ev_min"),
            "heater_switch": ("switch.termo_electrico",),
            "heater_power": ("termo_electrico_energy_power", "galeria_termo_power"),
            "heater_temp": ("temperatura_del_agua_ajustada", "termo_temp"),
            "heater_flow": ("caudal_instantaneo",),
            "pool_switch": ("terraza_plug_router", "depuradora"),
            "weather": ("weather.forecast_casa", "weather."),
            "solar_forecast": ("sem_forecast_tomorrow_kwh",),
        }
        all_states = self.hass.states.async_all()
        roles: dict[str, Any] = {}
        for role, pats in role_patterns.items():
            matches = [st for st in all_states if any(p in st.entity_id.lower() for p in pats)]
            if not matches:
                roles[role] = {"status": "missing", "selected": None, "candidates": [], "live": "—"}
                continue
            best = matches[0]
            valid = best.state not in ("unknown", "unavailable", "none", "")
            status = "ok" if (valid and len(matches) == 1) else "warning"
            unit = str(best.attributes.get("unit_of_measurement", "")).strip()
            roles[role] = {
                "status": status,
                "selected": best.entity_id,
                "candidates": [m.entity_id for m in matches[:6]],
                "live": f"{best.state} {unit}".strip(),
            }
        return {"roles": roles, "conflicts": self.detect_all_conflicts()}

    async def async_set_load_mode(self, load_id: str, mode: str) -> None:
        """Set per-load observing/active mode with conflict interlock (V3-M10)."""
        if mode not in ("observing", "active") or load_id not in ("pool", "heater", "ev"):
            raise ServiceValidationError("Carga o modo inválido.")
        if mode == "active":
            conflicts = self.detect_conflicts(load_id)
            if conflicts:
                names = ", ".join(c["entity_id"] for c in conflicts)
                self._record_blackbox(
                    "conflict",
                    f"Bloqueado paso a active para {load_id}: {names}",
                    target=load_id,
                )
                raise ServiceValidationError(
                    f"Conflicto activo en HA para {load_id}: apaga primero {names}"
                )
        if self.config.load_modes is None:
            self.config.load_modes = {"pool": "observing", "heater": "observing", "ev": "observing"}
        self.config.load_modes[load_id] = mode
        self._record_blackbox("mode_change", f"Carga {load_id} -> {mode}", target=load_id)
        await self.async_save_state()
        await self.async_request_refresh()

    async def async_trigger_extra_shower(self) -> None:
        """Activate 1-touch Extra Shower demand until local midnight (V3-M03)."""
        local_now = dt_util.now()
        local_end = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
        self.config.extra_shower_until = dt_util.as_utc(local_end)
        self._record_blackbox(
            "override", "Ducha extra / Visitas activada hasta medianoche", target="heater"
        )
        await self.async_save_state()
        await self.async_request_refresh()

    async def async_mute_ev_nag_today(self) -> None:
        """Mute BYD unplugged alert until local midnight (V3-M04)."""
        local_now = dt_util.now()
        local_end = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
        self.config.ev_nag_muted_until = dt_util.as_utc(local_end)
        self.runtime_state.ev_nag_sent_day = local_now.date().isoformat()
        self._record_blackbox("override", "Aviso BYD desenchufado silenciado hoy", target="ev")
        await self.async_save_state()
        await self.async_request_refresh()

    def get_filtered_blackbox(
        self,
        *,
        equipment: str | None = None,
        event_type: str | None = None,
        collapse: bool = True,
    ) -> list[dict[str, Any]]:
        """Filter Blackbox and collapse consecutive identical entries (V3-M12)."""
        items = [
            e
            for e in self.blackbox
            if (not equipment or e.get("target") == equipment)
            and (not event_type or e.get("type") == event_type)
        ]
        if not collapse:
            return items
        grouped: list[dict[str, Any]] = []
        for entry in items:
            if (
                grouped
                and grouped[-1].get("target") == entry.get("target")
                and grouped[-1].get("type") == entry.get("type")
                and grouped[-1].get("message") == entry.get("message")
            ):
                grouped[-1]["count"] = grouped[-1].get("count", 1) + 1
                grouped[-1]["first_ts"] = entry.get("ts")
            else:
                copy_e = dict(entry)
                copy_e["count"] = 1
                copy_e["first_ts"] = entry.get("ts")
                grouped.append(copy_e)
        return grouped

    def get_panel_snapshot(self) -> dict[str, Any]:
        """Build JSON-serializable payload for the custom sidebar Web Component."""
        plan = getattr(self, "data", None)
        snap = self._last_snapshot
        telemetry_source = (
            snap.telemetry_source.value if snap else TelemetrySource.UNAVAILABLE.value
        )
        if snap and snap.telemetry_source == TelemetrySource.CLOUD_FALLBACK:
            telemetry_status = TelemetrySource.CLOUD_FALLBACK.value
        elif plan is None or plan.degraded:
            telemetry_status = "degraded"
        else:
            telemetry_status = "ok"
        pool_in_season = (
            plan.pool_in_season
            if plan is not None
            else is_pool_in_season(snap.calendar_time if snap else dt_util.now(), self.config)
        )
        return {
            "entry_id": self.entry.entry_id,
            "mode": self.config.mode.value,
            "profile": self.config.profile.value,
            "manual_total_until": (
                self.config.manual_total_until.isoformat()
                if self.config.manual_total_until
                else None
            ),
            "load_modes": dict(
                self.config.load_modes
                or {"pool": "observing", "heater": "observing", "ev": "observing"}
            ),
            "conflicts": list(self._active_conflicts),
            "snapshot": {
                "solar_w": snap.solar_power_w if snap else None,
                "house_w": snap.load_power_w if snap else None,
                "grid_w": snap.grid_power_w if snap else None,
                "battery_w": snap.battery_power_w if snap else None,
                "battery_soc": snap.battery_soc if snap else None,
                "ev_power_w": snap.ev_power_w if snap else None,
                "ev_soc": snap.ev_soc if snap else None,
                "ev_current_a": snap.ev_current_a if snap else None,
                "ev_connected": snap.ev_connected if snap else False,
                "ev_charging": snap.ev_charging if snap else False,
                "ev_at_home": snap.ev_at_home if snap else False,
                "heater_on": snap.heater_on if snap else False,
                "heater_temp_c": snap.heater_temp_c if snap else None,
                "heater_power_w": snap.heater_power_w if snap else None,
                "pool_on": snap.pool_on if snap else False,
                "pool_power_w": snap.pool_power_w if snap else None,
                "pool_minutes_today": round(self.runtime_state.pool_minutes_today, 1),
                "pool_daily_max_minutes": self.config.pool_daily_max_minutes,
                "pool_in_season": pool_in_season,
                "solar_forecast_tomorrow_kwh": snap.solar_forecast_tomorrow_kwh if snap else None,
                "weather_daily_forecast": list(snap.weather_daily_forecast) if snap else [],
                "weather_fetch_failed": self._weather_failures > 0,
                "weather_failures": self._weather_failures,
                "weather_last_error": self._weather_last_error,
                "weather_last_error_at": (
                    self._weather_last_error_at.isoformat() if self._weather_last_error_at else None
                ),
                "weather_next_retry_at": (
                    self._weather_next_retry_at.isoformat() if self._weather_next_retry_at else None
                ),
                "weather_cache_at": (
                    self._weather_cache_at.isoformat() if self._weather_cache_at else None
                ),
                "telemetry_source": telemetry_source,
                "telemetry_status": telemetry_status,
                "cloud_fallback": telemetry_status == TelemetrySource.CLOUD_FALLBACK.value,
                "tariff_price_eur": snap.tariff_price_eur if snap else None,
            },
            "plan": {
                "traffic_light": plan.traffic_light.value if plan else "shadow",
                "safety_state": plan.safety_state.value if plan else "observing",
                "degraded": plan.degraded if plan else True,
                "telemetry_source": telemetry_source,
                "telemetry_status": telemetry_status,
                "pool_in_season": pool_in_season,
                "usable_surplus_w": plan.usable_surplus_w if plan else 0.0,
                "icp_headroom_w": plan.icp_headroom_w if plan else None,
                "icp_load_ratio_pct": plan.icp_load_ratio_pct if plan else None,
                "settling_remaining_s": plan.settling_remaining_s if plan else 0,
                "human_headline": plan.human_headline if plan else "Inicializando...",
                "why_by_device": plan.why_by_device if plan else {},
                "flex_loads": [asdict(x) for x in plan.flex_loads] if plan else [],
                "ev_commitment": asdict(plan.ev_commitment)
                if (plan and plan.ev_commitment)
                else None,
                "washer_suggestion": plan.washer_suggestion if plan else None,
                "battery_advice": plan.battery_advice if plan else None,
                "heater_thermostat_idle": plan.heater_thermostat_idle if plan else False,
                "shower_veto_active": plan.shower_veto_active if plan else False,
                "shower_veto_until": (
                    plan.shower_veto_until.isoformat()
                    if (plan and plan.shower_veto_until)
                    else None
                ),
                "extra_shower_active": plan.extra_shower_active if plan else False,
                "byd_unplugged_surplus_alert": plan.byd_unplugged_surplus_alert if plan else False,
                "storm_alert": plan.storm_alert if plan else False,
                "climate_advice": plan.climate_advice if plan else None,
                "cloud_pocket_used_kwh_today": plan.cloud_pocket_used_kwh_today if plan else 0.0,
                "alerts": [asdict(a) for a in plan.alerts] if plan else [],
                "estimated_savings_today_eur": plan.estimated_savings_today_eur if plan else None,
                "estimated_savings_month_eur": plan.estimated_savings_month_eur if plan else None,
                "grid_import_cost_today_eur": plan.grid_import_cost_today_eur if plan else None,
                "grid_import_cost_month_eur": plan.grid_import_cost_month_eur if plan else None,
                "grid_export_revenue_today_eur": (
                    plan.grid_export_revenue_today_eur if plan else None
                ),
                "grid_export_revenue_month_eur": (
                    plan.grid_export_revenue_month_eur if plan else None
                ),
                "net_cost_today_eur": plan.net_cost_today_eur if plan else None,
                "net_cost_month_eur": plan.net_cost_month_eur if plan else None,
                "savings_formula": plan.savings_formula if plan else "",
                "freshness_by_source": plan.freshness_by_source if plan else {},
                "simulated_actions": [asdict(a) for a in plan.simulated_actions] if plan else [],
            },
            "config": {
                "contracted_power_w": self.config.contracted_power_w,
                "icp_margin_w": self.config.icp_margin_w,
                "house_soc_floor": self.config.house_soc_floor,
                "ev_target_soc": self.config.ev_target_soc,
                "ev_max_amps": self.config.ev_max_amps,
                "heater_enabled": self.config.heater_enabled,
                "heater_target_temp_c": self.config.heater_target_temp_c,
                "pool_enabled": self.config.pool_enabled,
                "pool_daily_max_minutes": self.config.pool_daily_max_minutes,
                "pool_season_enabled": self.config.pool_season_enabled,
                "pool_season_start": self.config.pool_season_start,
                "pool_season_end": self.config.pool_season_end,
                "priority_order": list(self.config.priority_order),
                "protected_loads": list(self.config.protected_loads),
                "blocked_loads": list(self.config.blocked_loads),
                "tariff_mode": self.config.tariff_mode,
                "tariff_p1_eur": self.config.tariff_p1_eur,
                "tariff_p2_eur": self.config.tariff_p2_eur,
                "tariff_p3_eur": self.config.tariff_p3_eur,
                "export_comp_eur": self.config.export_comp_eur,
                "quiet_hours_enabled": self.config.quiet_hours_enabled,
                "quiet_hours_start": self.config.quiet_hours_start,
                "quiet_hours_end": self.config.quiet_hours_end,
                "entities": dict(self.entry.data),
            },
            "blackbox": self.get_filtered_blackbox()[:50],
        }

    async def async_shutdown(self) -> None:
        """Stop coordinator refreshes and flush counters on config entry unload."""
        await super().async_shutdown()
        if self._checkpoint_pending or self._urgent_save:
            await self.async_save_state()

    async def async_handle_shutdown(self, _event: Event | None = None) -> None:
        """Attempt safe Trydan fallback (Mode 3 + 6 A) on orderly HA shutdown."""
        if self.config.mode in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL):
            self.purge_pending_actions()
            if self._checkpoint_pending or self._urgent_save:
                await self.async_save_state()
            return
        safe_actions = (
            LoadAction(
                target="ev_min_amps",
                action="set",
                value=self.config.ev_min_amps,
                reason="ha_shutdown",
            ),
            LoadAction(
                target="ev_mode",
                action="set",
                value=TRYDAN_MODE_SOLAR_ONLY,
                reason="ha_shutdown",
            ),
        )
        try:
            await self._async_execute_actions(safe_actions)
        finally:
            if self._checkpoint_pending or self._urgent_save:
                await self.async_save_state()
