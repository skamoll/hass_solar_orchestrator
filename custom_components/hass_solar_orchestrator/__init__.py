"""The Solar Excess Orchestrator integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN, OperatingMode, ProfileName
from .coordinator import SolarOrchestratorCoordinator
from .panel import async_register_sidebar_panel, async_remove_sidebar_panel
from .websocket_api import async_register_websocket_api

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
]

_ORCHESTRABLE_LOADS = ("ev", "heater", "pool")
_RESTORE_SECTIONS = ("all", "safety", "loads")
_OVERRIDE_ACTIONS = ("on", "off", "auto", "charge_now")
_GRANULAR_LOAD_MODES = ("observing", "active")


async def _async_ensure_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Reject non-admin users while allowing system/automation calls without user_id."""
    user_id = call.context.user_id if call.context else None
    if user_id:
        user = await hass.auth.async_get_user(user_id)
        if user is None:
            raise UnknownUser(context=call.context, user_id=user_id)
        if not user.is_admin:
            raise Unauthorized(context=call.context, user_id=user_id)


def _service_coordinator(hass: HomeAssistant, call: ServiceCall) -> SolarOrchestratorCoordinator:
    entries: dict[str, SolarOrchestratorCoordinator] = hass.data.get(DOMAIN, {})
    if "config_entry_id" in call.data and call.data["config_entry_id"] is not None:
        entry_id = call.data["config_entry_id"]
        if entry_id in entries:
            return entries[entry_id]
        raise ServiceValidationError("The selected Solar Orchestrator entry is not loaded")
    if len(entries) == 1:
        return next(iter(entries.values()))
    raise ServiceValidationError("Select a loaded Solar Orchestrator config_entry_id")


async def _async_service_coordinator(
    hass: HomeAssistant, call: ServiceCall
) -> SolarOrchestratorCoordinator:
    """Authorize caller and resolve the target config entry coordinator."""
    await _async_ensure_admin(hass, call)
    return _service_coordinator(hass, call)


def _register_services(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_services_registered"):
        return

    async def handle_set_mode(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_set_mode(
            OperatingMode(call.data["mode"]),
            call.data.get("duration_minutes"),
        )

    async def handle_apply_profile(call: ServiceCall) -> dict[str, str]:
        coord = await _async_service_coordinator(hass, call)
        return await coord.async_apply_profile(
            ProfileName(call.data["profile"]),
            vacation_until=call.data.get("vacation_until"),
            preview_only=bool(call.data.get("preview_only", False)),
        )

    async def handle_set_priorities(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_set_priorities(
            list(call.data["priority_order"]),
            call.data.get("protected_loads"),
            call.data.get("blocked_loads"),
        )

    async def handle_set_ev_commitment(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_set_ev_commitment(
            float(call.data["target_soc"]),
            str(call.data["target_time_hhmm"]),
            bool(call.data["allow_grid_topup"]),
            str(call.data["grid_deadline_hhmm"]),
        )

    async def handle_override_load(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_override_load(
            str(call.data["load_id"]),
            str(call.data["action"]),
            int(call.data.get("duration_minutes", 60)),
        )

    async def handle_save_settings(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_save_settings(dict(call.data["data"]))

    async def handle_restore_defaults(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_restore_defaults(str(call.data.get("section", "all")))

    async def handle_clear_quarantine(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_clear_quarantine(str(call.data["load_id"]))

    async def handle_set_load_mode(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_set_load_mode(str(call.data["load_id"]), str(call.data["mode"]))

    async def handle_extra_shower_today(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_trigger_extra_shower()

    async def handle_mute_ev_nag_today(call: ServiceCall) -> None:
        coord = await _async_service_coordinator(hass, call)
        await coord.async_mute_ev_nag_today()

    def register(
        name: str,
        handler: Callable[..., Any],
        schema: vol.Schema,
        supports_response: SupportsResponse = SupportsResponse.NONE,
    ) -> None:
        async_register_admin_service(
            hass,
            DOMAIN,
            name,
            handler,
            schema.extend({vol.Optional("config_entry_id"): cv.string}),
            supports_response=supports_response,
        )

    register(
        "set_mode",
        handle_set_mode,
        schema=vol.Schema(
            {
                vol.Required("mode"): vol.In([m.value for m in OperatingMode]),
                vol.Optional("duration_minutes"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=1440)
                ),
            }
        ),
    )
    register(
        "apply_profile",
        handle_apply_profile,
        schema=vol.Schema(
            {
                vol.Required("profile"): vol.In([p.value for p in ProfileName]),
                vol.Optional("vacation_until"): cv.string,
                vol.Optional("preview_only", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    register(
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
    register(
        "set_ev_commitment",
        handle_set_ev_commitment,
        schema=vol.Schema(
            {
                vol.Required("target_soc"): vol.All(vol.Coerce(float), vol.Range(min=20, max=100)),
                vol.Required("target_time_hhmm"): vol.Match(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"),
                vol.Required("allow_grid_topup"): cv.boolean,
                vol.Required("grid_deadline_hhmm"): vol.Match(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"),
            }
        ),
    )
    register(
        "override_load",
        handle_override_load,
        schema=vol.Schema(
            {
                vol.Required("load_id"): vol.In(_ORCHESTRABLE_LOADS),
                vol.Required("action"): vol.In(_OVERRIDE_ACTIONS),
                vol.Optional("duration_minutes", default=60): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=1440)
                ),
            }
        ),
    )
    register(
        "save_settings",
        handle_save_settings,
        schema=vol.Schema({vol.Required("data"): dict}),
    )
    register(
        "restore_defaults",
        handle_restore_defaults,
        schema=vol.Schema({vol.Optional("section", default="all"): vol.In(_RESTORE_SECTIONS)}),
    )
    register(
        "clear_quarantine",
        handle_clear_quarantine,
        schema=vol.Schema({vol.Required("load_id"): vol.In(_ORCHESTRABLE_LOADS)}),
    )
    register(
        "set_load_mode",
        handle_set_load_mode,
        schema=vol.Schema(
            {
                vol.Required("load_id"): vol.In(_ORCHESTRABLE_LOADS),
                vol.Required("mode"): vol.In(_GRANULAR_LOAD_MODES),
            }
        ),
    )
    register(
        "extra_shower_today",
        handle_extra_shower_today,
        schema=vol.Schema({}),
    )
    register(
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
        entries = hass.data[DOMAIN]
        entries.pop(entry.entry_id, None)
        if not entries:
            async_remove_sidebar_panel(hass)
            for name in list(hass.services.async_services().get(DOMAIN, {})):
                hass.services.async_remove(DOMAIN, name)
            hass.data.pop(f"{DOMAIN}_services_registered", None)
    return unload_ok
