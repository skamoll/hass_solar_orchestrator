"""Number platform for Solar Excess Orchestrator live thresholds."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SolarOrchestratorCoordinator
from .entity import OrchestratorEntity
from .models import OrchestratorConfig


@dataclass(frozen=True, kw_only=True)
class OrchestratorNumberDescription(NumberEntityDescription):
    """Describe a live numeric setpoint."""

    get_fn: Callable[[OrchestratorConfig], float]
    set_fn: Callable[[OrchestratorConfig, float], None]


NUMBERS: tuple[OrchestratorNumberDescription, ...] = (
    OrchestratorNumberDescription(
        key="house_soc_floor",
        translation_key="house_soc_floor",
        native_min_value=0,
        native_max_value=100,
        native_step=5,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        get_fn=lambda c: c.house_soc_floor,
        set_fn=lambda c, v: setattr(c, "house_soc_floor", float(v)),
    ),
    OrchestratorNumberDescription(
        key="ev_target_soc",
        translation_key="ev_target_soc",
        native_min_value=20,
        native_max_value=100,
        native_step=5,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        get_fn=lambda c: c.ev_target_soc,
        set_fn=lambda c, v: setattr(c, "ev_target_soc", float(v)),
    ),
    OrchestratorNumberDescription(
        key="ev_max_amps",
        translation_key="ev_max_amps",
        native_min_value=6,
        native_max_value=32,
        native_step=1,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        mode=NumberMode.SLIDER,
        get_fn=lambda c: float(c.ev_max_amps),
        set_fn=lambda c, v: setattr(c, "ev_max_amps", int(v)),
    ),
    OrchestratorNumberDescription(
        key="heater_target_temp",
        translation_key="heater_target_temp",
        native_min_value=30,
        native_max_value=65,
        native_step=1,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        get_fn=lambda c: c.heater_target_temp_c,
        set_fn=lambda c, v: setattr(c, "heater_target_temp_c", float(v)),
    ),
    OrchestratorNumberDescription(
        key="pool_daily_max_minutes",
        translation_key="pool_daily_max_minutes",
        native_min_value=0,
        native_max_value=720,
        native_step=15,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        get_fn=lambda c: float(c.pool_daily_max_minutes),
        set_fn=lambda c, v: setattr(c, "pool_daily_max_minutes", int(v)),
    ),
    OrchestratorNumberDescription(
        key="battery_slack_w",
        translation_key="battery_slack_w",
        native_min_value=0,
        native_max_value=600,
        native_step=50,
        native_unit_of_measurement=UnitOfPower.WATT,
        mode=NumberMode.SLIDER,
        get_fn=lambda c: c.battery_slack_w,
        set_fn=lambda c, v: setattr(c, "battery_slack_w", float(v)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Excess Orchestrator number entities."""
    coordinator: SolarOrchestratorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(OrchestratorNumber(coordinator, desc) for desc in NUMBERS)


class OrchestratorNumber(OrchestratorEntity, NumberEntity):
    """Number setpoint entity for Solar Excess Orchestrator."""

    entity_description: OrchestratorNumberDescription

    def __init__(
        self,
        coordinator: SolarOrchestratorCoordinator,
        description: OrchestratorNumberDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        """Return current configured setpoint."""
        return self.entity_description.get_fn(self.coordinator.config)

    async def async_set_native_value(self, value: float) -> None:
        """Update setpoint, persist, and refresh coordinator."""
        self.entity_description.set_fn(self.coordinator.config, value)
        await self.coordinator.async_save_state()
        await self.coordinator.async_request_refresh()
