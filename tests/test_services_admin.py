"""Tests for admin authorization, config_entry_id validation, and input schemas on services."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if importlib.util.find_spec("homeassistant") is None:
    pytest.skip("homeassistant package not installed", allow_module_level=True)

from homeassistant import bootstrap, loader
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser

import custom_components.hass_solar_orchestrator as _integration_mod
from custom_components.hass_solar_orchestrator import _register_services
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

try:
    from probatio.error import Invalid as _ProbatioInvalid
except ImportError:  # pragma: no cover - fallback when probatio is absent
    _ProbatioInvalid = _integration_mod.vol.Invalid

SCHEMA_INVALID = (_integration_mod.vol.Invalid, _ProbatioInvalid)

SERVICE_PAYLOADS: dict[str, dict[str, Any]] = {
    "set_mode": {"mode": "observing"},
    "apply_profile": {"profile": "saving", "preview_only": True},
    "set_priorities": {"priority_order": ["house", "ev", "heater", "pool"]},
    "set_ev_commitment": {
        "target_soc": 80,
        "target_time_hhmm": "07:30",
        "allow_grid_topup": False,
        "grid_deadline_hhmm": "05:00",
    },
    "override_load": {"load_id": "heater", "action": "auto", "duration_minutes": 30},
    "save_settings": {"data": {"heater_enabled": False}},
    "restore_defaults": {"section": "safety"},
    "clear_quarantine": {"load_id": "heater"},
    "set_load_mode": {"load_id": "pool", "mode": "observing"},
    "extra_shower_today": {},
    "mute_ev_nag_today": {},
}


def _make_entry(unique_id: str) -> ConfigEntry:
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title=f"Solar Excess Orchestrator ({unique_id})",
        data={
            CONF_SOLAR_POWER_ENTITY: "sensor.sungrow_modbus_solar_power",
            CONF_LOAD_POWER_ENTITY: "sensor.sungrow_modbus_load_power",
            CONF_GRID_POWER_ENTITY: "sensor.sungrow_modbus_grid_power",
            CONF_BATTERY_POWER_ENTITY: "sensor.sungrow_modbus_battery_power",
            CONF_BATTERY_SOC_ENTITY: "sensor.sungrow_modbus_battery_level",
        },
        options={CONF_EVAL_INTERVAL_SECONDS: 10},
        source="user",
        unique_id=unique_id,
        discovery_keys={},
        subentries_data=None,
    )


async def _setup_hass(tmp_path: Path) -> tuple[HomeAssistant, ConfigEntry]:
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parent.parent / "custom_components"
    )
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    await bootstrap.async_from_config_dict({}, hass)
    await hass.async_start()
    hass.states.async_set("sensor.sungrow_modbus_solar_power", "3200", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.sungrow_modbus_load_power", "800", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.sungrow_modbus_grid_power", "-2400", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.sungrow_modbus_battery_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.sungrow_modbus_battery_level", "85", {"unit_of_measurement": "%"})
    entry = _make_entry("test_orchestrator")
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return hass, entry


@pytest.mark.asyncio
async def test_services_admin_allowed_non_admin_rejected_and_system_allowed(
    tmp_path: Path,
) -> None:
    """Every service allows admin and system calls, and rejects non-admin users."""
    hass, entry = await _setup_hass(tmp_path)
    try:
        admin_user = await hass.auth.async_create_user("Admin User", group_ids=[GROUP_ID_ADMIN])
        regular_user = await hass.auth.async_create_user("Regular User", group_ids=[GROUP_ID_USER])

        for service_name, payload in SERVICE_PAYLOADS.items():
            # Non-admin user must be rejected with Unauthorized.
            with pytest.raises(Unauthorized):
                await hass.services.async_call(
                    DOMAIN,
                    service_name,
                    {"config_entry_id": entry.entry_id, **payload},
                    blocking=True,
                    context=Context(user_id=regular_user.id),
                )

            # Admin user must succeed.
            await hass.services.async_call(
                DOMAIN,
                service_name,
                {"config_entry_id": entry.entry_id, **payload},
                blocking=True,
                context=Context(user_id=admin_user.id),
            )

            # System / automation call (no user_id) must succeed.
            await hass.services.async_call(
                DOMAIN,
                service_name,
                {"config_entry_id": entry.entry_id, **payload},
                blocking=True,
                context=Context(),
            )
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_service_handlers_enforce_admin_directly() -> None:
    """Handlers themselves enforce admin check even if invoked outside HA's wrapper."""
    captured: dict[str, Any] = {}

    def fake_register(
        hass: Any,
        domain: str,
        name: str,
        handler: Any,
        schema: Any = None,
        supports_response: Any = None,
    ) -> None:
        captured[name] = handler

    coord = MagicMock()
    coord.async_set_mode = AsyncMock()
    coord.async_apply_profile = AsyncMock(return_value={"summary": "ok"})
    coord.async_set_priorities = AsyncMock()
    coord.async_set_ev_commitment = AsyncMock()
    coord.async_override_load = AsyncMock()
    coord.async_save_settings = AsyncMock()
    coord.async_restore_defaults = AsyncMock()
    coord.async_clear_quarantine = AsyncMock()
    coord.async_set_load_mode = AsyncMock()
    coord.async_trigger_extra_shower = AsyncMock()
    coord.async_mute_ev_nag_today = AsyncMock()

    hass = MagicMock()
    hass.data = {DOMAIN: {"entry_1": coord}}
    hass.auth.async_get_user = AsyncMock(
        side_effect=lambda uid: (
            None if uid == "ghost" else SimpleNamespace(is_admin=(uid == "admin_1"))
        )
    )

    with patch(
        "custom_components.hass_solar_orchestrator.async_register_admin_service",
        side_effect=fake_register,
    ):
        _register_services(hass)

    assert set(captured) == set(SERVICE_PAYLOADS)

    for service_name, payload in SERVICE_PAYLOADS.items():
        handler = captured[service_name]

        # Non-admin direct call -> Unauthorized
        with pytest.raises(Unauthorized):
            await handler(
                ServiceCall(
                    hass,
                    DOMAIN,
                    service_name,
                    {"config_entry_id": "entry_1", **payload},
                    context=Context(user_id="user_1"),
                )
            )

        # Unknown (e.g. deleted) user -> UnknownUser, never allowed through
        with pytest.raises(UnknownUser):
            await handler(
                ServiceCall(
                    hass,
                    DOMAIN,
                    service_name,
                    {"config_entry_id": "entry_1", **payload},
                    context=Context(user_id="ghost"),
                )
            )

        # Admin direct call -> allowed
        await handler(
            ServiceCall(
                hass,
                DOMAIN,
                service_name,
                {"config_entry_id": "entry_1", **payload},
                context=Context(user_id="admin_1"),
            )
        )

        # System direct call (no user_id) -> allowed
        await handler(
            ServiceCall(
                hass,
                DOMAIN,
                service_name,
                {"config_entry_id": "entry_1", **payload},
                context=Context(),
            )
        )


@pytest.mark.asyncio
async def test_services_config_entry_id_strict_and_input_validation(tmp_path: Path) -> None:
    """Unknown or ambiguous config_entry_id and invalid inputs raise validation errors."""
    hass, entry = await _setup_hass(tmp_path)
    try:
        # Unknown config_entry_id -> ServiceValidationError
        with pytest.raises(ServiceValidationError, match="not loaded"):
            await hass.services.async_call(
                DOMAIN,
                "set_mode",
                {"config_entry_id": "nonexistent", "mode": OperatingMode.PAUSED.value},
                blocking=True,
            )

        # Empty string config_entry_id must NOT fall back to the single loaded entry
        with pytest.raises(ServiceValidationError, match="not loaded"):
            await hass.services.async_call(
                DOMAIN,
                "set_mode",
                {"config_entry_id": "", "mode": OperatingMode.PAUSED.value},
                blocking=True,
            )

        # Multiple loaded entries without config_entry_id -> ServiceValidationError (no fallback)
        hass.data[DOMAIN]["second_entry"] = hass.data[DOMAIN][entry.entry_id]
        try:
            with pytest.raises(ServiceValidationError, match="config_entry_id"):
                await hass.services.async_call(
                    DOMAIN,
                    "set_mode",
                    {"mode": OperatingMode.PAUSED.value},
                    blocking=True,
                )
        finally:
            hass.data[DOMAIN].pop("second_entry", None)

        # Input schema validation
        with pytest.raises(SCHEMA_INVALID):
            await hass.services.async_call(
                DOMAIN,
                "set_mode",
                {"config_entry_id": entry.entry_id, "mode": "manual_total", "duration_minutes": 0},
                blocking=True,
            )
        with pytest.raises(SCHEMA_INVALID):
            await hass.services.async_call(
                DOMAIN,
                "override_load",
                {
                    "config_entry_id": entry.entry_id,
                    "load_id": "heater",
                    "action": "on",
                    "duration_minutes": 2,
                },
                blocking=True,
            )
        with pytest.raises(SCHEMA_INVALID):
            await hass.services.async_call(
                DOMAIN,
                "restore_defaults",
                {"config_entry_id": entry.entry_id, "section": "invalid_section"},
                blocking=True,
            )
        with pytest.raises(SCHEMA_INVALID):
            await hass.services.async_call(
                DOMAIN,
                "clear_quarantine",
                {"config_entry_id": entry.entry_id, "load_id": "house"},
                blocking=True,
            )
        with pytest.raises(SCHEMA_INVALID):
            await hass.services.async_call(
                DOMAIN,
                "set_load_mode",
                {"config_entry_id": entry.entry_id, "load_id": "pool", "mode": "bogus"},
                blocking=True,
            )
    finally:
        await hass.async_stop()
