"""WebSocket API for the Solar Orchestrator sidebar panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .coordinator import SolarOrchestratorCoordinator


def _get_coordinator(
    hass: HomeAssistant, entry_id: str | None
) -> SolarOrchestratorCoordinator | None:
    entries: dict[str, SolarOrchestratorCoordinator] = hass.data.get(DOMAIN, {})
    if not entries:
        return None
    if entry_id and entry_id in entries:
        return entries[entry_id]
    return next(iter(entries.values()))


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register WebSocket commands for the sidebar panel (idempotent)."""
    if hass.data.get(f"{DOMAIN}_ws_registered"):
        return
    websocket_api.async_register_command(hass, ws_get_snapshot)
    websocket_api.async_register_command(hass, ws_validate_entity)
    hass.data[f"{DOMAIN}_ws_registered"] = True


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/snapshot",
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_get_snapshot(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return full panel snapshot, freshness, flex loads, alerts, and Blackbox."""
    coord = _get_coordinator(hass, msg.get("entry_id"))
    if coord is None:
        connection.send_error(
            msg["id"], "not_found", "No hay instancia configurada de Solar Orchestrator"
        )
        return
    connection.send_result(msg["id"], coord.get_panel_snapshot())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/validate_entity",
        vol.Required("entity_id"): str,
    }
)
@callback
def ws_validate_entity(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Inspect a live HA entity for the M09 onboarding sign & unit verifier."""
    entity_id = msg["entity_id"].strip()
    st = hass.states.get(entity_id)
    if st is None:
        connection.send_result(
            msg["id"],
            {
                "entity_id": entity_id,
                "exists": False,
                "state": None,
                "unit": None,
                "normalized_w": None,
                "hint": "Entidad no encontrada en Home Assistant.",
            },
        )
        return
    unit = str(st.attributes.get("unit_of_measurement", "")).strip()
    raw = st.state
    normalized_w: float | None = None
    hint = "Lectura válida."
    if raw in ("unknown", "unavailable", "none", ""):
        hint = "Entidad actualmente no disponible (unavailable/unknown)."
    else:
        try:
            val = float(raw)
            normalized_w = val * 1000.0 if unit.lower() == "kw" else val
            if normalized_w < 0:
                hint = (
                    f"Valor negativo ({normalized_w:.0f} W): "
                    "si es sensor de red, indica exportación."
                )
            else:
                hint = (
                    f"Valor positivo ({normalized_w:.0f} W): "
                    "si es sensor de red, indica importación."
                )
        except ValueError:
            hint = f"Estado no numérico: {raw}"

    connection.send_result(
        msg["id"],
        {
            "entity_id": entity_id,
            "exists": True,
            "state": raw,
            "unit": unit,
            "normalized_w": normalized_w,
            "hint": hint,
        },
    )
