"""Base entity class for Solar Excess Orchestrator."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SolarOrchestratorCoordinator


class OrchestratorEntity(CoordinatorEntity[SolarOrchestratorCoordinator]):
    """Base entity attached to the Solar Excess Orchestrator hub device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarOrchestratorCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return single orchestrator device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            manufacturer="skamoll",
            model="Solar Excess Orchestrator",
            name="Solar Excess Orchestrator",
        )
