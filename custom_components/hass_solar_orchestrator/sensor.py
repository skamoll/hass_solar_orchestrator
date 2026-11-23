"""Sensor platform for Solar Excess Orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SolarOrchestratorCoordinator
from .entity import OrchestratorEntity
from .models import DispatchPlan


@dataclass(frozen=True, kw_only=True)
class OrchestratorSensorDescription(SensorEntityDescription):
    """Describe an orchestrator sensor."""

    value_fn: Callable[[SolarOrchestratorCoordinator, DispatchPlan], Any]
    attrs_fn: Callable[[SolarOrchestratorCoordinator, DispatchPlan], dict[str, Any] | None] = (
        lambda _c, _p: None
    )


SENSORS: tuple[OrchestratorSensorDescription, ...] = (
    OrchestratorSensorDescription(
        key="decision_summary",
        translation_key="decision_summary",
        value_fn=lambda _c, p: p.decision_summary[:255],
        attrs_fn=lambda c, p: {
            "effective_mode": p.effective_mode,
            "reasons": list(p.reasons),
            "pending_actions": [
                {"target": a.target, "action": a.action, "value": a.value, "reason": a.reason}
                for a in p.actions
            ],
            "heater_solar_owned": c.runtime_state.heater_solar_owned,
            "pool_solar_owned": c.runtime_state.pool_solar_owned,
            "pool_cooldown_until": (
                c.runtime_state.pool_cooldown_until.isoformat()
                if c.runtime_state.pool_cooldown_until
                else None
            ),
            "handoff_waiting_for_off": c.runtime_state.handoff_waiting_for_off,
        },
    ),
    OrchestratorSensorDescription(
        key="usable_surplus",
        translation_key="usable_surplus",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda _c, p: p.usable_surplus_w,
    ),
    OrchestratorSensorDescription(
        key="ev_budget",
        translation_key="ev_budget",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda _c, p: p.ev_budget_w,
    ),
    OrchestratorSensorDescription(
        key="recommended_ev_amps",
        translation_key="recommended_ev_amps",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=lambda _c, p: p.recommended_ev_amps,
    ),
    OrchestratorSensorDescription(
        key="pool_minutes_today",
        translation_key="pool_minutes_today",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda c, _p: round(c.runtime_state.pool_minutes_today, 1),
    ),
    OrchestratorSensorDescription(
        key="telemetry_source",
        translation_key="telemetry_source",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _c, p: p.telemetry_source.value,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Excess Orchestrator sensors."""
    coordinator: SolarOrchestratorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(OrchestratorSensor(coordinator, desc) for desc in SENSORS)


class OrchestratorSensor(OrchestratorEntity, SensorEntity):
    """Sensor entity exposing orchestrator telemetry and explanations."""

    entity_description: OrchestratorSensorDescription

    def __init__(
        self,
        coordinator: SolarOrchestratorCoordinator,
        description: OrchestratorSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return current sensor value."""
        plan = self.coordinator.data
        if plan is None:
            return None
        return self.entity_description.value_fn(self.coordinator, plan)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra diagnostic attributes."""
        plan = self.coordinator.data
        if plan is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator, plan)
