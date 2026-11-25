"""Home Assistant coordinator for Solar Excess Orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
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
    DispatchPlan,
    EnergySnapshot,
    LoadAction,
    OrchestratorConfig,
    OrchestratorState,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
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
        self._degraded_since: datetime | None = None
        self.config = self._build_config()
        self.runtime_state = OrchestratorState(started_at=dt_util.utcnow())
        self.blackbox: deque[dict[str, Any]] = deque(maxlen=MAX_BLACKBOX_ENTRIES)
        self._last_snapshot: EnergySnapshot | None = None
        self._expected_heater_on: bool | None = None
        self._expected_pool_on: bool | None = None
        self._weather_cache: list[dict[str, Any]] = []
        self._weather_cache_at: datetime | None = None
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

    async def async_load_state(self) -> None:
        """Load persisted counters, ownership flags, Blackbox, and mode."""
        raw = await self._store.async_load()
        if not raw:
            self.runtime_state.started_at = dt_util.utcnow()
            return
        if "mode" in raw:
            with contextlib.suppress(ValueError):
                self.config.mode = OperatingMode(raw["mode"])
        if "profile" in raw:
            with contextlib.suppress(ValueError):
                self.config.profile = ProfileName(raw["profile"])
        for field_name in (
            "heater_enabled",
            "pool_enabled",
            "house_soc_floor",
            "ev_target_soc",
            "ev_max_amps",
            "heater_target_temp_c",
            "pool_daily_max_minutes",
            "battery_slack_w",
            "contracted_power_w",
            "icp_margin_w",
            "ev_target_time_hhmm",
            "ev_allow_grid_topup",
            "ev_grid_deadline_hhmm",
            "tariff_mode",
            "tariff_p1_eur",
            "tariff_p2_eur",
            "tariff_p3_eur",
            "export_comp_eur",
            "quiet_hours_enabled",
            "quiet_hours_start",
            "quiet_hours_end",
        ):
            if field_name in raw:
                setattr(self.config, field_name, raw[field_name])

        if "priority_order" in raw and isinstance(raw["priority_order"], list):
            order = [x for x in raw["priority_order"] if x != "house"]
            self.config.priority_order = ("house", *order)
        if "protected_loads" in raw and isinstance(raw["protected_loads"], list):
            self.config.protected_loads = tuple(raw["protected_loads"])
        if "blocked_loads" in raw and isinstance(raw["blocked_loads"], list):
            self.config.blocked_loads = tuple(raw["blocked_loads"])

        self.config.manual_total_until = _parse_dt(raw.get("manual_total_until"))
        self.config.ev_boost_until = _parse_dt(raw.get("ev_boost_until"))
        self.config.vacation_until = _parse_dt(raw.get("vacation_until"))
        self.config.extra_shower_until = _parse_dt(raw.get("extra_shower_until"))
        self.config.ev_nag_muted_until = _parse_dt(raw.get("ev_nag_muted_until"))
        if isinstance(raw.get("load_modes"), dict):
            self.config.load_modes = {
                k: v for k, v in raw["load_modes"].items() if v in ("observing", "active")
            }

        self.runtime_state = OrchestratorState(
            started_at=dt_util.utcnow(),
            heater_solar_owned=bool(raw.get("heater_solar_owned", False)),
            pool_solar_owned=bool(raw.get("pool_solar_owned", False)),
            pool_manual_blocked_today=bool(raw.get("pool_manual_blocked_today", False)),
            pool_minutes_today=float(raw.get("pool_minutes_today", 0.0)),
            pool_last_day=raw.get("pool_last_day"),
            pool_last_tick=_parse_dt(raw.get("pool_last_tick")),
            pool_cooldown_until=_parse_dt(raw.get("pool_cooldown_until")),
            heater_quarantined=bool(raw.get("heater_quarantined", False)),
            pool_quarantined=bool(raw.get("pool_quarantined", False)),
            ev_quarantined=bool(raw.get("ev_quarantined", False)),
            savings_today_eur=float(raw.get("savings_today_eur", 0.0)),
            savings_month_eur=float(raw.get("savings_month_eur", 0.0)),
            grid_import_cost_today_eur=float(raw.get("grid_import_cost_today_eur", 0.0)),
            grid_import_cost_month_eur=float(raw.get("grid_import_cost_month_eur", 0.0)),
            grid_export_revenue_today_eur=float(raw.get("grid_export_revenue_today_eur", 0.0)),
            grid_export_revenue_month_eur=float(raw.get("grid_export_revenue_month_eur", 0.0)),
            savings_last_day=raw.get("savings_last_day"),
            savings_last_month=raw.get("savings_last_month"),
            shower_veto_started_at=_parse_dt(raw.get("shower_veto_started_at")),
            shower_veto_until=_parse_dt(raw.get("shower_veto_until")),
        )
        for item in reversed(raw.get("blackbox", [])[:MAX_BLACKBOX_ENTRIES]):
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
            "shower_veto_started_at": (
                self.runtime_state.shower_veto_started_at.isoformat()
                if self.runtime_state.shower_veto_started_at
                else None
            ),
            "shower_veto_until": (
                self.runtime_state.shower_veto_until.isoformat()
                if self.runtime_state.shower_veto_until
                else None
            ),
            "heater_solar_owned": self.runtime_state.heater_solar_owned,
            "pool_solar_owned": self.runtime_state.pool_solar_owned,
            "pool_manual_blocked_today": self.runtime_state.pool_manual_blocked_today,
            "pool_minutes_today": round(self.runtime_state.pool_minutes_today, 2),
            "pool_last_day": self.runtime_state.pool_last_day,
            "pool_last_tick": (
                self.runtime_state.pool_last_tick.isoformat()
                if self.runtime_state.pool_last_tick
                else None
            ),
            "pool_cooldown_until": (
                self.runtime_state.pool_cooldown_until.isoformat()
                if self.runtime_state.pool_cooldown_until
                else None
            ),
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
        await self._store.async_save(payload)

    def _read_float_w(self, entity_id: str | None) -> tuple[float | None, datetime | None]:
        """Read a power sensor safely (never float(x, 0) on unavailable/unknown)."""
        if not entity_id:
            return None, None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", "none", ""):
            return None, None
        try:
            val = float(st.state)
        except ValueError:
            return None, None
        unit = str(st.attributes.get("unit_of_measurement", "W")).strip().lower()
        if unit == "kw":
            val *= 1000.0
        ts = getattr(st, "last_reported", None) or st.last_updated
        return val, ts

    def _read_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", "none", ""):
            return None
        try:
            return float(st.state)
        except ValueError:
            return None

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

        critical_invalid = grid_w is None or solar_w is None
        source = TelemetrySource.MODBUS
        local_ts = grid_ts or solar_ts

        if grid_w is None or solar_w is None:
            cloud_solar_w, _ = self._read_float_w(data.get(CONF_CLOUD_PV_POWER_ENTITY))
            cloud_grid_w, _ = self._read_float_w(data.get(CONF_CLOUD_GRID_POWER_ENTITY))
            cloud_bat_w, _ = self._read_float_w(data.get(CONF_CLOUD_BATTERY_POWER_ENTITY))
            cloud_soc = self._read_float(data.get(CONF_CLOUD_BATTERY_SOC_ENTITY))
            if cloud_grid_w is not None or cloud_solar_w is not None:
                solar_w = solar_w if solar_w is not None else cloud_solar_w
                grid_w = grid_w if grid_w is not None else cloud_grid_w
                bat_w = bat_w if bat_w is not None else cloud_bat_w
                bat_soc = bat_soc if bat_soc is not None else cloud_soc
                source = TelemetrySource.CLOUD_FALLBACK
            else:
                source = TelemetrySource.UNAVAILABLE

        ev_min_raw = self._read_float(data.get(CONF_EV_MIN_CURRENT_ENTITY))
        _, ev_ts = self._read_float_w(data.get(CONF_EV_POWER_ENTITY))
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
            ev_power_w=self._read_float_w(data.get(CONF_EV_POWER_ENTITY))[0] or 0.0,
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
            weather_daily_forecast=tuple(self._weather_cache),
            outdoor_temp_c=outdoor_temp,
            indoor_temp_c=indoor_temp,
            critical_sensor_invalid=critical_invalid,
        )
        self._last_snapshot = snap
        return snap

    def _check_w_return_and_external(self, snapshot: EnergySnapshot) -> None:
        """Verify W-return feedback & detect external manual changes (M18/M19)."""
        now = snapshot.now
        if self._expected_heater_on is not None:
            if snapshot.heater_on != self._expected_heater_on:
                self.runtime_state.heater_external_override_until = now + timedelta(
                    minutes=DEFAULT_EXTERNAL_OVERRIDE_MINUTES
                )
                self._expected_heater_on = None
                self._record_blackbox(
                    "external_override",
                    "Cambio externo detectado en Termo: control pausado 30 min",
                    target="heater",
                )
            elif (
                not self.runtime_state.heater_thermostat_idle
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

        if self._expected_pool_on is not None:
            if snapshot.pool_on != self._expected_pool_on:
                self.runtime_state.pool_external_override_until = now + timedelta(
                    minutes=DEFAULT_EXTERNAL_OVERRIDE_MINUTES
                )
                self._expected_pool_on = None
                self._record_blackbox(
                    "external_override",
                    "Cambio externo detectado en Depuradora: control pausado 30 min",
                    target="pool",
                )
            elif (
                snapshot.pool_on
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
        if entity_id and is_forbidden_entity(entity_id):
            raise SecurityDenylistViolation(
                f"Acción bloqueada por Denylist inmutable sobre entidad prohibida: {entity_id}"
            )

    def purge_pending_actions(self) -> int:
        """Clear any queued actions immediately when entering Sombra or Manual total."""
        count = len(self._pending_actions)
        self._pending_actions.clear()
        return count

    async def _async_execute_actions(self, actions: tuple[LoadAction, ...]) -> None:
        """Execute emitted actuator actions sequentially under a lock with Denylist guard."""
        if self.config.mode in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL):
            self.purge_pending_actions()
            return
        if not actions:
            return
        self._pending_actions.extend(actions)
        data = self.entry.data
        async with self._exec_lock:
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
                    if act.target == "heater":
                        entity_id = data.get(CONF_HEATER_SWITCH_ENTITY)
                        self._validate_target_entity(entity_id)
                        if entity_id:
                            await self.hass.services.async_call(
                                "switch",
                                act.action,
                                {"entity_id": entity_id},
                                blocking=True,
                            )
                            self._expected_heater_on = act.action == "turn_on"
                    elif act.target == "pool":
                        entity_id = data.get(CONF_POOL_SWITCH_ENTITY)
                        self._validate_target_entity(entity_id)
                        if entity_id:
                            await self.hass.services.async_call(
                                "switch",
                                act.action,
                                {"entity_id": entity_id},
                                blocking=True,
                            )
                            self._expected_pool_on = act.action == "turn_on"
                    elif act.target == "ev_min_amps":
                        entity_id = data.get(CONF_EV_MIN_CURRENT_ENTITY)
                        self._validate_target_entity(entity_id)
                        if entity_id and act.value is not None:
                            await self.hass.services.async_call(
                                "number",
                                "set_value",
                                {"entity_id": entity_id, "value": int(act.value)},
                                blocking=True,
                            )
                    elif act.target == "ev_mode":
                        svc = str(data.get(CONF_EV_MODE_SERVICE) or "").strip()
                        self._validate_target_entity(svc)
                        if svc and "." in svc and act.value is not None:
                            domain_name, service_name = svc.split(".", 1)
                            await self.hass.services.async_call(
                                domain_name,
                                service_name,
                                {"modo": int(act.value)},
                                blocking=True,
                            )
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

    async def _async_refresh_weather_cache(self) -> None:
        """Fetch 3-5 day daily weather forecast with 30-min cache (V3-M05)."""
        now = dt_util.utcnow()
        if (
            self._weather_cache_at is not None
            and (now - self._weather_cache_at).total_seconds() < 1800.0
        ):
            return
        weather_id = self.entry.data.get(CONF_WEATHER_ENTITY) or "weather.forecast_casa"
        if not self.hass.states.get(weather_id):
            return
        with contextlib.suppress(Exception):
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_id, "type": "daily"},
                blocking=True,
                return_response=True,
            )
            fc = (resp or {}).get(weather_id, {}).get("forecast", [])
            if isinstance(fc, list):
                self._weather_cache = fc[:5]
                self._weather_cache_at = now

    async def _async_update_data(self) -> DispatchPlan:
        await self._async_refresh_weather_cache()
        self._active_conflicts = self.detect_all_conflicts()
        snapshot = self._build_snapshot()
        self._check_w_return_and_external(snapshot)
        plan, next_state = evaluate(snapshot, self.config, self.runtime_state)
        self.runtime_state = next_state

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
            await self._async_execute_actions(plan.actions)

        await self.async_save_state()
        return plan

    async def async_set_mode(
        self, mode: OperatingMode, duration_minutes: int | None = None
    ) -> None:
        """Switch operating mode cleanly, purging queues on Sombra/Manual total."""
        now = dt_util.utcnow()
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

        if profile == ProfileName.VACATION and not vacation_until:
            raise ServiceValidationError("El perfil Vacaciones exige una fecha de fin.")

        self.config.profile = profile
        if profile == ProfileName.SAVING:
            self.config.house_soc_floor = 30.0
            self.config.ev_allow_grid_topup = False
        elif profile == ProfileName.COMFORT:
            self.config.house_soc_floor = 20.0
            self.config.ev_allow_grid_topup = True
        elif profile == ProfileName.FAST_CHARGE:
            self.config.mode = OperatingMode.FORCE_EV
        elif profile == ProfileName.VACATION:
            self.config.heater_enabled = False
            self.config.pool_enabled = True
            self.config.vacation_until = _parse_dt(vacation_until)

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
        self.config.ev_target_soc = max(20.0, min(100.0, float(target_soc)))
        self.config.ev_target_time_hhmm = target_time_hhmm
        self.config.ev_allow_grid_topup = bool(allow_grid_topup)
        self.config.ev_grid_deadline_hhmm = grid_deadline_hhmm
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
        now = dt_util.utcnow()
        until = now + timedelta(minutes=max(5, min(1440, duration_minutes)))
        if load_id == "ev" and action == "charge_now":
            self.config.ev_boost_until = until
            if self.config.mode not in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL):
                self.config.mode = OperatingMode.FORCE_EV
            self._record_blackbox(
                "override", f"Cargar ahora VE ({duration_minutes} min)", target="ev"
            )
        elif load_id == "ev" and action == "auto":
            self.config.ev_boost_until = None
            if self.config.mode == OperatingMode.FORCE_EV:
                self.config.mode = OperatingMode.AUTO_EV_MODULATED
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

    async def async_save_settings(self, data: dict[str, Any]) -> None:
        """Save tariff, contracted power, ICP margin, and advanced settings (M13/M14)."""
        for key in (
            "contracted_power_w",
            "icp_margin_w",
            "house_soc_floor",
            "ev_max_amps",
            "heater_target_temp_c",
            "pool_daily_max_minutes",
            "tariff_mode",
            "tariff_p1_eur",
            "tariff_p2_eur",
            "tariff_p3_eur",
            "export_comp_eur",
            "quiet_hours_enabled",
            "quiet_hours_start",
            "quiet_hours_end",
            "heater_enabled",
            "pool_enabled",
        ):
            if key in data:
                setattr(self.config, key, data[key])
        if self.config.contracted_power_w < 1000.0:
            raise ServiceValidationError("La potencia contratada debe ser >= 1000 W.")
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
        self.runtime_state.ev_nag_sent_day = dt_util.utcnow().date().isoformat()
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
        plan = self.data
        snap = self._last_snapshot
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
                "solar_forecast_tomorrow_kwh": snap.solar_forecast_tomorrow_kwh if snap else None,
                "weather_daily_forecast": list(snap.weather_daily_forecast) if snap else [],
                "tariff_price_eur": snap.tariff_price_eur if snap else None,
            },
            "plan": {
                "traffic_light": plan.traffic_light.value if plan else "shadow",
                "safety_state": plan.safety_state.value if plan else "observing",
                "degraded": plan.degraded if plan else True,
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

    async def async_handle_shutdown(self, _event: Event | None = None) -> None:
        """Attempt safe Trydan fallback (Mode 3 + 6 A) on orderly HA shutdown."""
        if self.config.mode in (OperatingMode.OBSERVING, OperatingMode.MANUAL_TOTAL):
            self.purge_pending_actions()
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
        await self._async_execute_actions(safe_actions)
