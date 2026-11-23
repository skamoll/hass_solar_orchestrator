"""Select platform for Solar Excess Orchestrator operating mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, OperatingMode
from .coordinator import SolarOrchestratorCoordinator
from .entity import OrchestratorEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Excess Orchestrator select entity."""
    coordinator: SolarOrchestratorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OrchestratorModeSelect(coordinator)])


class OrchestratorModeSelect(OrchestratorEntity, SelectEntity):
    """Operating mode selector for Solar Excess Orchestrator."""

    _attr_translation_key = "operating_mode"
    _attr_options = [m.value for m in OperatingMode]

    def __init__(self, coordinator: SolarOrchestratorCoordinator) -> None:
        super().__init__(coordinator, "operating_mode")

    @property
    def current_option(self) -> str:
        """Return active operating mode."""
        return self.coordinator.config.mode.value

    async def async_select_option(self, option: str) -> None:
        """Switch operating mode safely (purging queue on observing/manual_total)."""
        await self.coordinator.async_set_mode(OperatingMode(option))
