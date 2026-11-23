"""Config flow and options flow for Solar Excess Orchestrator."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

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
)


def _entity_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_SOLAR_POWER_ENTITY,
                default=d.get(CONF_SOLAR_POWER_ENTITY, "sensor.sungrow_modbus_solar_power"),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_LOAD_POWER_ENTITY,
                default=d.get(CONF_LOAD_POWER_ENTITY, "sensor.sungrow_modbus_load_power"),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_GRID_POWER_ENTITY,
                default=d.get(CONF_GRID_POWER_ENTITY, "sensor.sungrow_modbus_grid_power"),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_BATTERY_POWER_ENTITY,
                default=d.get(CONF_BATTERY_POWER_ENTITY, "sensor.sungrow_modbus_battery_power"),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_BATTERY_SOC_ENTITY,
                default=d.get(CONF_BATTERY_SOC_ENTITY, "sensor.sungrow_modbus_battery_level"),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_CLOUD_PV_POWER_ENTITY,
                default=d.get(CONF_CLOUD_PV_POWER_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_CLOUD_GRID_POWER_ENTITY,
                default=d.get(CONF_CLOUD_GRID_POWER_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_CLOUD_BATTERY_POWER_ENTITY,
                default=d.get(CONF_CLOUD_BATTERY_POWER_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_CLOUD_BATTERY_SOC_ENTITY,
                default=d.get(CONF_CLOUD_BATTERY_SOC_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_EV_CONNECTED_ENTITY,
                default=d.get(CONF_EV_CONNECTED_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_EV_CHARGING_ENTITY,
                default=d.get(CONF_EV_CHARGING_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_EV_POWER_ENTITY,
                default=d.get(CONF_EV_POWER_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_EV_CURRENT_ENTITY,
                default=d.get(CONF_EV_CURRENT_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_EV_MIN_CURRENT_ENTITY,
                default=d.get(CONF_EV_MIN_CURRENT_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_EV_MODE_ENTITY,
                default=d.get(CONF_EV_MODE_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_EV_SOC_ENTITY,
                default=d.get(CONF_EV_SOC_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_EV_MODE_SERVICE,
                default=d.get(CONF_EV_MODE_SERVICE, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_HEATER_SWITCH_ENTITY,
                default=d.get(CONF_HEATER_SWITCH_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_HEATER_TEMP_ENTITY,
                default=d.get(CONF_HEATER_TEMP_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_HEATER_POWER_ENTITY,
                default=d.get(CONF_HEATER_POWER_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_POOL_SWITCH_ENTITY,
                default=d.get(CONF_POOL_SWITCH_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_POOL_POWER_ENTITY,
                default=d.get(CONF_POOL_POWER_ENTITY, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        }
    )


class SolarOrchestratorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Solar Excess Orchestrator."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            return self.async_create_entry(
                title="Solar Excess Orchestrator",
                data=user_input,
                options={
                    CONF_EVAL_INTERVAL_SECONDS: DEFAULT_EVAL_INTERVAL_SECONDS,
                    CONF_STALE_TIMEOUT_SECONDS: DEFAULT_STALE_TIMEOUT_SECONDS,
                    CONF_HOUSE_SOC_FLOOR: DEFAULT_HOUSE_SOC_FLOOR,
                    CONF_EV_TARGET_SOC: DEFAULT_EV_TARGET_SOC,
                    CONF_EV_MIN_AMPS: DEFAULT_EV_MIN_AMPS,
                    CONF_EV_MAX_AMPS: DEFAULT_EV_MAX_AMPS,
                    CONF_EV_START_AMPS: DEFAULT_EV_START_AMPS,
                    CONF_HEATER_TARGET_TEMP: DEFAULT_HEATER_TARGET_TEMP,
                    CONF_HEATER_START_EXPORT_W: DEFAULT_HEATER_START_EXPORT_W,
                    CONF_POOL_START_EXPORT_W: DEFAULT_POOL_START_EXPORT_W,
                    CONF_POOL_DAILY_MAX_MINUTES: DEFAULT_POOL_DAILY_MAX_MINUTES,
                    CONF_POOL_COOLDOWN_MINUTES: DEFAULT_POOL_COOLDOWN_MINUTES,
                    CONF_IMPORT_SHED_THRESHOLD_W: DEFAULT_IMPORT_SHED_THRESHOLD_W,
                    CONF_BATTERY_SLACK_W: DEFAULT_BATTERY_SLACK_W,
                },
            )
        return self.async_show_form(step_id="user", data_schema=_entity_schema())

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle entity mapping reconfiguration in place."""
        reconfigure_entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(
                reconfigure_entry,
                data_updates=user_input,
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_entity_schema(dict(reconfigure_entry.data)),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SolarOrchestratorOptionsFlow()


class SolarOrchestratorOptionsFlow(OptionsFlow):
    """Handle options flow for Solar Excess Orchestrator."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage orchestrator thresholds."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_EVAL_INTERVAL_SECONDS,
                    default=opts.get(CONF_EVAL_INTERVAL_SECONDS, DEFAULT_EVAL_INTERVAL_SECONDS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=5, max=60, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
                    )
                ),
                vol.Required(
                    CONF_STALE_TIMEOUT_SECONDS,
                    default=opts.get(CONF_STALE_TIMEOUT_SECONDS, DEFAULT_STALE_TIMEOUT_SECONDS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=15,
                        max=300,
                        step=5,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_HEATER_START_EXPORT_W,
                    default=opts.get(CONF_HEATER_START_EXPORT_W, DEFAULT_HEATER_START_EXPORT_W),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=500,
                        max=5000,
                        step=50,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="W",
                    )
                ),
                vol.Required(
                    CONF_POOL_START_EXPORT_W,
                    default=opts.get(CONF_POOL_START_EXPORT_W, DEFAULT_POOL_START_EXPORT_W),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=300,
                        max=3000,
                        step=50,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="W",
                    )
                ),
                vol.Required(
                    CONF_IMPORT_SHED_THRESHOLD_W,
                    default=opts.get(CONF_IMPORT_SHED_THRESHOLD_W, DEFAULT_IMPORT_SHED_THRESHOLD_W),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=100,
                        max=2000,
                        step=50,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="W",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
