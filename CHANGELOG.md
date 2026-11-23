# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-11-23

### Added
- **Smart Water Heater (`1054 W` Real Calibration, Thermostat Idle & Real Shower Pattern):**
  - Corrected default water heater nominal power from `2000 W` to `1054 W` (`DEFAULT_HEATER_START_EXPORT_W = 1150 W`).
  - Mechanical thermostat cut detection (`>800 W` observed then `<100 W` fresh for `300 s` → `heater_thermostat_idle`) releases `1054 W` without triggering false quarantine (`unavailable`/`unknown`/stale never coerced to `0 W`).
  - Real shower pattern detector (`V3-M02`): triggers via flow `>= 2.5 L/min` (`45 s`), volume spike `>= 2 L` (`60 s`), or abrupt drop `ΔT <= -1.5 °C` (`10 min`) → blocks optimization `turn_off` for `15 min` post-flow (hard capped at `90 min`, persisted across restarts, never overrides electrical/ICP safety cuts).
  - One-touch `+ Ducha extra hoy` (`extra_shower_today`) expiring at local midnight.
- **Concurrent Surplus Sharing, Multi-Day Forecast & Proactive BYD/Climate:**
  - Simultaneous load sharing (`V3-M06`): Pool (`250 W`) + Heater (`1054 W`) + EV (`>= 6 A`) share surplus instead of rigid mutexes, stepping down EV amps first on deficit.
  - 3–5 day `weather.get_forecasts` daily integration (`30 min` cache) with `storm_alert` (`⛈️ Temporal en camino`) + enriched `climate.aire_comedor` solar advice (`800/500 W` hysteresis).
  - Unplugged BYD solar surplus alert (`zone.home >= 10 min`, `trydan_conectado == off`, surplus `>= 1400 W`, SoC `< 85%`) with 1-tap `Silenciar hoy` (`mute_ev_nag_today`).
  - Cloud-bridging leaky bucket (`0.2 kWh / 5 min / 0.5 kWh day`) tracked passively in `observing` metrics.
- **Granular Per-Load Activation, HA Conflict Interlock & Zero-Focus-Loss UX/UI:**
  - Independent `observing`/`active` toggles per load (`pool`, `heater`, `ev`) with automatic HA automation conflict detection (`detect_conflicts`).
  - 1-click HA Entity Auto-Discovery (`hass_solar_orchestrator/entities_scan`) with live ✅/⚠️ validation.
  - Refactored `solar-orchestrator-panel.js` with single-mount Shadow DOM + `requestAnimationFrame` surgical DOM patching (never loses input focus, scroll, or slider edits), animated CSS/SVG energy flow (`prefers-reduced-motion` safe), `fmtW()`, and filterable Blackbox with `×N` collapse + JSON export.

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
