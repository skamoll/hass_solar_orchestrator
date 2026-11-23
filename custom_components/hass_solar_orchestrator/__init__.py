"""The Solar Excess Orchestrator integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, OperatingMode, ProfileName
from .coordinator import SolarOrchestratorCoordinator
from .panel import async_register_sidebar_panel
from .websocket_api import async_register_websocket_api

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
]


def _first_coordinator(hass: HomeAssistant) -> SolarOrchestratorCoordinator | None:
    entries: dict[str, SolarOrchestratorCoordinator] = hass.data.get(DOMAIN, {})
    return next(iter(entries.values()), None)


def _register_services(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_services_registered"):
        return

    async def handle_set_mode(call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_set_mode(
                OperatingMode(call.data["mode"]),
                call.data.get("duration_minutes"),
            )

    async def handle_apply_profile(call: ServiceCall) -> dict[str, str]:
        if coord := _first_coordinator(hass):
            return await coord.async_apply_profile(
                ProfileName(call.data["profile"]),
                vacation_until=call.data.get("vacation_until"),
                preview_only=bool(call.data.get("preview_only", False)),
            )
        return {}

    async def handle_set_priorities(call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_set_priorities(
                list(call.data["priority_order"]),
                call.data.get("protected_loads"),
                call.data.get("blocked_loads"),
            )

    async def handle_set_ev_commitment(call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_set_ev_commitment(
                float(call.data["target_soc"]),
                str(call.data["target_time_hhmm"]),
                bool(call.data["allow_grid_topup"]),
                str(call.data["grid_deadline_hhmm"]),
            )

    async def handle_override_load(call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_override_load(
                str(call.data["load_id"]),
                str(call.data["action"]),
                int(call.data.get("duration_minutes", 60)),
            )

    async def handle_save_settings(call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_save_settings(dict(call.data["data"]))

    async def handle_restore_defaults(call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_restore_defaults(str(call.data.get("section", "all")))

    async def handle_clear_quarantine(call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_clear_quarantine(str(call.data["load_id"]))

    async def handle_set_load_mode(call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_set_load_mode(str(call.data["load_id"]), str(call.data["mode"]))

    async def handle_extra_shower_today(_call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_trigger_extra_shower()

    async def handle_mute_ev_nag_today(_call: ServiceCall) -> None:
        if coord := _first_coordinator(hass):
            await coord.async_mute_ev_nag_today()

    hass.services.async_register(
        DOMAIN,
        "set_mode",
        handle_set_mode,
        schema=vol.Schema(
            {
                vol.Required("mode"): cv.string,
                vol.Optional("duration_minutes"): vol.Coerce(int),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "apply_profile",
        handle_apply_profile,
        schema=vol.Schema(
            {
                vol.Required("profile"): cv.string,
                vol.Optional("vacation_until"): cv.string,
                vol.Optional("preview_only", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_priorities",
        handle_set_priorities,
        schema=vol.Schema(
            {
                vol.Required("priority_order"): [cv.string],
                vol.Optional("protected_loads"): [cv.string],
                vol.Optional("blocked_loads"): [cv.string],
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "set_ev_commitment",
        handle_set_ev_commitment,
        schema=vol.Schema(
            {
                vol.Required("target_soc"): vol.Coerce(float),
                vol.Required("target_time_hhmm"): cv.string,
                vol.Required("allow_grid_topup"): cv.boolean,
                vol.Required("grid_deadline_hhmm"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "override_load",
        handle_override_load,
        schema=vol.Schema(
            {
                vol.Required("load_id"): cv.string,
                vol.Required("action"): cv.string,
                vol.Optional("duration_minutes", default=60): vol.Coerce(int),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "save_settings",
        handle_save_settings,
        schema=vol.Schema({vol.Required("data"): dict}),
    )
    hass.services.async_register(
        DOMAIN,
        "restore_defaults",
        handle_restore_defaults,
        schema=vol.Schema({vol.Optional("section", default="all"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        "clear_quarantine",
        handle_clear_quarantine,
        schema=vol.Schema({vol.Required("load_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        "set_load_mode",
        handle_set_load_mode,
        schema=vol.Schema(
            {
                vol.Required("load_id"): cv.string,
                vol.Required("mode"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "extra_shower_today",
        handle_extra_shower_today,
        schema=vol.Schema({}),
    )
    hass.services.async_register(
        DOMAIN,
        "mute_ev_nag_today",
        handle_mute_ev_nag_today,
        schema=vol.Schema({}),
    )
    hass.data[f"{DOMAIN}_services_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar Excess Orchestrator from a config entry."""
    coordinator = SolarOrchestratorCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_handle_shutdown)
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_register_websocket_api(hass)
    await async_register_sidebar_panel(hass)
    _register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
