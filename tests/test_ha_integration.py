"""End-to-end Home Assistant Core integration tests (runs when homeassistant is installed)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

if importlib.util.find_spec("homeassistant") is None:
    pytest.skip("homeassistant package not installed", allow_module_level=True)

from homeassistant import bootstrap, loader
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.hass_solar_orchestrator.const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EVAL_INTERVAL_SECONDS,
    CONF_GRID_POWER_ENTITY,
    CONF_LOAD_POWER_ENTITY,
    CONF_SOLAR_POWER_ENTITY,
    DOMAIN,
    OperatingMode,
)


@pytest.mark.asyncio
async def test_ha_setup_entities_mode_switch_and_unload(tmp_path: Path) -> None:
    """Verify full setup, OBSERVING mode, UI entity updates, and unload on HA Core."""
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parent.parent / "custom_components"
    )
    hass = HomeAssistant(str(tmp_path))
    hass.config.skip_pip = True  # deps are preinstalled; never pip at test time
    loader.async_setup(hass)
    await bootstrap.async_from_config_dict({}, hass)
    await hass.async_start()
    try:
        hass.states.async_set(
            "sensor.sungrow_modbus_solar_power", "3200", {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            "sensor.sungrow_modbus_load_power", "800", {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            "sensor.sungrow_modbus_grid_power", "-2400", {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            "sensor.sungrow_modbus_battery_power", "0", {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            "sensor.sungrow_modbus_battery_level", "85", {"unit_of_measurement": "%"}
        )

        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Solar Excess Orchestrator",
            data={
                CONF_SOLAR_POWER_ENTITY: "sensor.sungrow_modbus_solar_power",
                CONF_LOAD_POWER_ENTITY: "sensor.sungrow_modbus_load_power",
                CONF_GRID_POWER_ENTITY: "sensor.sungrow_modbus_grid_power",
                CONF_BATTERY_POWER_ENTITY: "sensor.sungrow_modbus_battery_power",
                CONF_BATTERY_SOC_ENTITY: "sensor.sungrow_modbus_battery_level",
            },
            options={CONF_EVAL_INTERVAL_SECONDS: 10},
            source="user",
            unique_id="test_orchestrator",
            discovery_keys={},
            subentries_data=None,
        )
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        ent_reg = er.async_get(hass)
        entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        registered = {entity.unique_id.removeprefix(f"{entry.entry_id}_") for entity in entities}
        assert {
            "decision_summary",
            "operating_mode",
            "degraded",
            "ev_max_amps",
            "heater_enabled",
            "grid_import_cost_today",
            "grid_export_revenue_today",
            "net_cost_today",
        } <= registered

        surplus = hass.states.get("sensor.solar_excess_orchestrator_usable_solar_surplus")
        assert surplus is not None
        assert float(surplus.state) == 2400.0

        mode_state = hass.states.get("select.solar_excess_orchestrator_operating_mode")
        assert mode_state is not None
        assert mode_state.state == OperatingMode.OBSERVING.value

        await hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": "select.solar_excess_orchestrator_operating_mode",
                "option": OperatingMode.AUTO_SOLAR.value,
            },
            blocking=True,
        )
        assert (
            hass.states.get("select.solar_excess_orchestrator_operating_mode").state
            == OperatingMode.AUTO_SOLAR.value
        )

        with pytest.raises(ServiceValidationError, match="not loaded"):
            await hass.services.async_call(
                DOMAIN, "set_mode", {"config_entry_id": "unknown", "mode": "paused"}, blocking=True
            )
        await hass.services.async_call(
            DOMAIN,
            "set_mode",
            {"config_entry_id": entry.entry_id, "mode": "observing"},
            blocking=True,
        )
        assert hass.data[DOMAIN][entry.entry_id].config.mode is OperatingMode.OBSERVING
        assert frontend.async_panel_exists(hass, "solar-orchestrator")
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert not hass.services.has_service(DOMAIN, "set_mode")
        assert not frontend.async_panel_exists(hass, "solar-orchestrator")
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert frontend.async_panel_exists(hass, "solar-orchestrator")
        assert hass.services.has_service(DOMAIN, "set_mode")
    finally:
        await hass.async_stop()
