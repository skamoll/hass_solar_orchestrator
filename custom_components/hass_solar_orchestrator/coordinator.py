"""Home Assistant coordinator for Solar Excess Orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_SLACK_W,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CLOUD_BATTERY_POWER_ENTITY,
    CONF_CLOUD_BATTERY_SOC_ENTITY,
    CONF_CLOUD_GRID_POWER_ENTITY,
    CONF_CLOUD_PV_POWER_ENTITY,
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
    CONF_EVAL_INTERVAL_SECONDS,
    CONF_GRID_POWER_ENTITY,
    CONF_HEATER_POWER_ENTITY,
    CONF_HEATER_START_EXPORT_W,
    CONF_HEATER_SWITCH_ENTITY,
    CONF_HEATER_TARGET_TEMP,
    CONF_HEATER_TEMP_ENTITY,
    CONF_HOUSE_SOC_FLOOR,
    CONF_IMPORT_SHED_THRESHOLD_W,
    CONF_LOAD_POWER_ENTITY,
    CONF_POOL_COOLDOWN_MINUTES,
    CONF_POOL_DAILY_MAX_MINUTES,
    CONF_POOL_POWER_ENTITY,
    CONF_POOL_START_EXPORT_W,
    CONF_POOL_SWITCH_ENTITY,
    CONF_SOLAR_POWER_ENTITY,
    CONF_STALE_TIMEOUT_SECONDS,
    DEFAULT_BATTERY_SLACK_W,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_EV_START_AMPS,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_EVAL_INTERVAL_SECONDS,
    DEFAULT_HEATER_START_EXPORT_W,
    DEFAULT_HEATER_TARGET_TEMP,
    DEFAULT_HOUSE_SOC_FLOOR,
    DEFAULT_IMPORT_SHED_THRESHOLD_W,
    DEFAULT_POOL_COOLDOWN_MINUTES,
    DEFAULT_POOL_DAILY_MAX_MINUTES,
    DEFAULT_POOL_START_EXPORT_W,
    DEFAULT_STALE_TIMEOUT_SECONDS,
    DOMAIN,
    OperatingMode,
    TelemetrySource,
)
from .engine import TRYDAN_MODE_SOLAR_ONLY, evaluate
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
        self._degraded_since: datetime | None = None
        self.config = self._build_config()
        self.runtime_state = OrchestratorState()

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
        )

    async def async_load_state(self) -> None:
        """Load persisted counters, ownership flags, and mode."""
        raw = await self._store.async_load()
        if not raw:
            return
        if "mode" in raw:
            with contextlib.suppress(ValueError):
                self.config.mode = OperatingMode(raw["mode"])
        for field_name in (
            "heater_enabled",
            "pool_enabled",
            "house_soc_floor",
            "ev_target_soc",
            "ev_max_amps",
            "heater_target_temp_c",
            "pool_daily_max_minutes",
            "battery_slack_w",
        ):
            if field_name in raw:
                setattr(self.config, field_name, raw[field_name])

        self.runtime_state = OrchestratorState(
            heater_solar_owned=bool(raw.get("heater_solar_owned", False)),
            pool_solar_owned=bool(raw.get("pool_solar_owned", False)),
            pool_manual_blocked_today=bool(raw.get("pool_manual_blocked_today", False)),
            pool_minutes_today=float(raw.get("pool_minutes_today", 0.0)),
            pool_last_day=raw.get("pool_last_day"),
            pool_last_tick=_parse_dt(raw.get("pool_last_tick")),
            pool_cooldown_until=_parse_dt(raw.get("pool_cooldown_until")),
        )
        _LOGGER.debug("Loaded orchestrator state for %s: %s", self.entry.entry_id, raw)

    async def async_save_state(self) -> None:
        """Persist runtime counters and user UI settings."""
        payload = {
            "mode": self.config.mode.value,
            "heater_enabled": self.config.heater_enabled,
            "pool_enabled": self.config.pool_enabled,
            "house_soc_floor": self.config.house_soc_floor,
            "ev_target_soc": self.config.ev_target_soc,
            "ev_max_amps": self.config.ev_max_amps,
            "heater_target_temp_c": self.config.heater_target_temp_c,
            "pool_daily_max_minutes": self.config.pool_daily_max_minutes,
            "battery_slack_w": self.config.battery_slack_w,
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
        }
        await self._store.async_save(payload)

    def _read_float_w(self, entity_id: str | None) -> tuple[float | None, datetime | None]:
        """Read a power sensor and convert kW to W if needed."""
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
        bat_w, _ = self._read_float_w(data.get(CONF_BATTERY_POWER_ENTITY))
        bat_soc = self._read_float(data.get(CONF_BATTERY_SOC_ENTITY))

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
        return EnergySnapshot(
            now=now,
            solar_power_w=solar_w,
            load_power_w=load_w,
            grid_power_w=grid_w,
            battery_power_w=bat_w,
            battery_soc=bat_soc,
            local_sample_time=local_ts,
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
            heater_power_w=self._read_float_w(data.get(CONF_HEATER_POWER_ENTITY))[0],
            pool_on=self._read_bool(data.get(CONF_POOL_SWITCH_ENTITY)),
            pool_power_w=self._read_float_w(data.get(CONF_POOL_POWER_ENTITY))[0],
        )

    async def _async_execute_actions(self, actions: tuple[LoadAction, ...]) -> None:
        """Execute emitted actuator actions sequentially under a lock."""
        if not actions:
            return
        data = self.entry.data
        async with self._exec_lock:
            for act in actions:
                try:
                    if act.target == "heater":
                        entity_id = data.get(CONF_HEATER_SWITCH_ENTITY)
                        if entity_id:
                            await self.hass.services.async_call(
                                "switch",
                                act.action,
                                {"entity_id": entity_id},
                                blocking=True,
                            )
                    elif act.target == "pool":
                        entity_id = data.get(CONF_POOL_SWITCH_ENTITY)
                        if entity_id:
                            await self.hass.services.async_call(
                                "switch",
                                act.action,
                                {"entity_id": entity_id},
                                blocking=True,
                            )
                    elif act.target == "ev_min_amps":
                        entity_id = data.get(CONF_EV_MIN_CURRENT_ENTITY)
                        if entity_id and act.value is not None:
                            await self.hass.services.async_call(
                                "number",
                                "set_value",
                                {"entity_id": entity_id, "value": int(act.value)},
                                blocking=True,
                            )
                    elif act.target == "ev_mode":
                        svc = str(data.get(CONF_EV_MODE_SERVICE) or "").strip()
                        if svc and "." in svc and act.value is not None:
                            domain_name, service_name = svc.split(".", 1)
                            await self.hass.services.async_call(
                                domain_name,
                                service_name,
                                {"modo": int(act.value)},
                                blocking=True,
                            )
                    _LOGGER.debug(
                        "Executed orchestrator action: %s.%s(%s) reason=%s",
                        act.target,
                        act.action,
                        act.value,
                        act.reason,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Failed orchestrator action %s.%s(%s): %s",
                        act.target,
                        act.action,
                        act.value,
                        err,
                    )

    async def _async_update_data(self) -> DispatchPlan:
        snapshot = self._build_snapshot()
        plan, next_state = evaluate(snapshot, self.config, self.runtime_state)
        self.runtime_state = next_state

        _LOGGER.debug(
            "Orchestrator tick: %s | reasons=%s | actions=%d",
            plan.decision_summary,
            list(plan.reasons),
            len(plan.actions),
        )

        # Surface sustained DEGRADED state as a native Home Assistant Repair
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

    async def async_handle_shutdown(self, _event: Event | None = None) -> None:
        """Attempt safe Trydan fallback (Mode 3 + 6 A) on orderly HA shutdown."""
        if self.config.mode == OperatingMode.OBSERVING:
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
