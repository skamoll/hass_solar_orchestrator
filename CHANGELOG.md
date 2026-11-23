# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-23

### Added
- **Dedicated Home Assistant Sidebar Panel (`Solar Orchestrator`)**: Zero-build native Web Component (`frontend/solar-orchestrator-panel.js`) registered via `panel_custom` with two tabs: **Estado General** (visual live energy flow, "¿Qué pasa ahora?", ICP headroom bar, EV commitment/boost card, flex load radar, honest savings, local rotating Blackbox) and **Configuración** (1-touch profiles with impact preview, priority matrix with Casa locked #1, tariff/contracted power manager, live polarity/unit verifier, progressive disclosure advanced options).
- **10-Round Consejo de Sabios Unanimous Safety & Control**:
  - `Manual total` mode with auto-expiry strictly returning to `observing` (Modo Sombra) + persistent notification (`D1`).
  - Immutable anti-cut Denylist (`is_forbidden_entity` / `SecurityDenylistViolation`) + serialized executor with immediate queue purge on entering `observing`/`manual_total`.
  - Virtual power reservation (`virtual_power_reservation`) & `settling_lock` cooldown preventing double-dispatch on slow polling sensors.
  - `unavailable`/`unknown` telemetry never coerced to `0`, triggering `DEGRADED`/`SAFE_HOLD` and rendering `"—"` in UI.
  - WebSocket API (`hass_solar_orchestrator/snapshot`, `hass_solar_orchestrator/validate_entity`) and 8 native services (`services.yaml`).
  - New sensors (`traffic_light`, `icp_headroom`, `estimated_savings_today`) and acceptance test suite `tests/test_consejo_consensus.py` (`CA-1..CA-5`).

## [0.1.0] - 2026-11-23

### Added
- Pure Python deterministic orchestration engine (`engine.py` + `models.py`) with zero Home Assistant dependencies and unit test suite (`tests/test_engine.py`).
- Native Home Assistant integration (`ConfigFlow`, `OptionsFlow`, `Reconfigure`, persistent storage, `Repairs`, and bilingual `en`/`es` UI).
- Operating modes (`observing`, `auto_solar`, `auto_ev_modulated`, `force_ev`, `house_priority`, `paused`), live setpoints (`number`), load toggles (`switch`), and explanatory diagnostics (`sensor` + `binary_sensor`).
