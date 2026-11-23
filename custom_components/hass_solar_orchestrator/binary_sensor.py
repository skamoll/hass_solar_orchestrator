"""Binary sensor platform for Solar Excess Orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SolarOrchestratorCoordinator
from .entity import OrchestratorEntity
from .models import DispatchPlan


@dataclass(frozen=True, kw_only=True)
class OrchestratorBinaryDescription(BinarySensorEntityDescription):
    """Describe an orchestrator binary sensor."""

    value_fn: Callable[[SolarOrchestratorCoordinator, DispatchPlan], bool]


BINARY_SENSORS: tuple[OrchestratorBinaryDescription, ...] = (
    OrchestratorBinaryDescription(
        key="degraded",
        translation_key="degraded",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _c, p: p.degraded,
    ),
    OrchestratorBinaryDescription(
        key="heater_solar_owned",
        translation_key="heater_solar_owned",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, _p: c.runtime_state.heater_solar_owned,
    ),
    OrchestratorBinaryDescription(
        key="pool_solar_owned",
        translation_key="pool_solar_owned",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, _p: c.runtime_state.pool_solar_owned,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Excess Orchestrator binary sensors."""
    coordinator: SolarOrchestratorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(OrchestratorBinarySensor(coordinator, desc) for desc in BINARY_SENSORS)


class OrchestratorBinarySensor(OrchestratorEntity, BinarySensorEntity):
    """Binary sensor for orchestrator state."""

    entity_description: OrchestratorBinaryDescription

    def __init__(
        self,
        coordinator: SolarOrchestratorCoordinator,
        description: OrchestratorBinaryDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return True when active."""
        plan = self.coordinator.data
        if plan is None:
            return None
        return self.entity_description.value_fn(self.coordinator, plan)
