"""Switch platform for Solar Excess Orchestrator load enables and daily overrides."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SolarOrchestratorCoordinator
from .entity import OrchestratorEntity


@dataclass(frozen=True, kw_only=True)
class OrchestratorSwitchDescription(SwitchEntityDescription):
    """Describe an orchestrator toggle."""

    get_fn: Callable[[SolarOrchestratorCoordinator], bool]
    set_fn: Callable[[SolarOrchestratorCoordinator, bool], None]


SWITCHES: tuple[OrchestratorSwitchDescription, ...] = (
    OrchestratorSwitchDescription(
        key="heater_enabled",
        translation_key="heater_enabled",
        get_fn=lambda c: c.config.heater_enabled,
        set_fn=lambda c, v: setattr(c.config, "heater_enabled", v),
    ),
    OrchestratorSwitchDescription(
        key="pool_enabled",
        translation_key="pool_enabled",
        get_fn=lambda c: c.config.pool_enabled,
        set_fn=lambda c, v: setattr(c.config, "pool_enabled", v),
    ),
    OrchestratorSwitchDescription(
        key="pool_manual_blocked_today",
        translation_key="pool_manual_blocked_today",
        get_fn=lambda c: c.runtime_state.pool_manual_blocked_today,
        set_fn=lambda c, v: setattr(c.runtime_state, "pool_manual_blocked_today", v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Excess Orchestrator switches."""
    coordinator: SolarOrchestratorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(OrchestratorSwitch(coordinator, desc) for desc in SWITCHES)


class OrchestratorSwitch(OrchestratorEntity, SwitchEntity):
    """Switch entity for Solar Excess Orchestrator."""

    entity_description: OrchestratorSwitchDescription

    def __init__(
        self,
        coordinator: SolarOrchestratorCoordinator,
        description: OrchestratorSwitchDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return True when enabled."""
        return self.entity_description.get_fn(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable flag, persist, and refresh."""
        self.entity_description.set_fn(self.coordinator, True)
        await self.coordinator.async_save_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable flag, persist, and refresh."""
        self.entity_description.set_fn(self.coordinator, False)
        await self.coordinator.async_save_state()
        await self.coordinator.async_request_refresh()
