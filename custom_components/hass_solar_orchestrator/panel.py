"""Sidebar panel registration for Solar Orchestrator."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_COMPONENT_NAME, PANEL_STATIC_URL, PANEL_URL_PATH

FRONTEND_DIR = Path(__file__).parent / "frontend"


async def async_register_sidebar_panel(hass: HomeAssistant) -> None:
    """Register the static JS bundle and custom left-sidebar panel in Home Assistant."""
    if hass.data.get(f"{DOMAIN}_panel_registered"):
        return

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                url_path=PANEL_STATIC_URL,
                path=str(FRONTEND_DIR),
                cache_headers=False,
            )
        ]
    )

    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_COMPONENT_NAME,
        frontend_url_path=PANEL_URL_PATH,
        sidebar_title="Solar Orchestrator",
        sidebar_icon="mdi:solar-power-variant",
        module_url=f"{PANEL_STATIC_URL}/solar-orchestrator-panel.js?v=0.2.0",
        require_admin=False,
        embed_iframe=False,
    )
    hass.data[f"{DOMAIN}_panel_registered"] = True
