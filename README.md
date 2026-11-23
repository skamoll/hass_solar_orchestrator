# Solar Excess Orchestrator for Home Assistant

[![CI](https://github.com/skamoll/hass_solar_orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/skamoll/hass_solar_orchestrator/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A deterministic, native Home Assistant custom integration (`hass_solar_orchestrator`) that unifies PV surplus orchestration across **local inverter Modbus**, **cloud fallback** (e.g. `hass_sungrow_isolarcloud`), **EV chargers** (e.g. V2C Trydan + BYD vehicle SoC), and **auxiliary loads** (water heater, pool pump) through a single priority state machine and native UI.

---

## Architecture & Core Invariants

- **Single Control Authority:** Never duplicates Modbus TCP sockets. Reads your existing Home Assistant entities (`sungrow_modbus_*`, `pv_<ps_id>_*`, Trydan, BYD) and resolves priorities in one place:
  1. **House & Home Battery** (respects configurable `house_soc_floor`)
  2. **EV Charger** (`Auto Solar` lets Trydan run autonomous `FV Exclusivo` while HA manages auxiliaries; `Auto EV Modulated` enables HA `Mode 2` current control with `T_settle >= 10 s` and `T_min_run/stop >= 180 s` anti-chattering)
  3. **Water Heater** (respects manual overrides via `heater_solar_owned` and uses measured power when available)
  4. **Pool Pump** (daily quota + 60-minute cooldown after forced yield, persisted across HA restarts)
- **Confirmed Handoff (*Cesión confirmada*):** When shedding a lower-priority load to allocate power upward, waits for `off` confirmation and a fresh post-off local sample before stepping up.
- **Safe `OBSERVING` (dry-run) Launch Mode:** Computes surplus, budget, target amps, and full explanation (`sensor.solar_excess_orchestrator_decision_summary`) without actuating any relay or charger until you explicitly switch to `auto_solar` or `auto_ev_modulated`.
- **Fail-Safe `DEGRADED` Mode & Shutdown Hook:** If local Modbus telemetry goes stale (`> 30 s`), blocks all step-ups/starts, creates an auto-clearing native Home Assistant Repair, and sets Trydan to autonomous Mode 3 (`FV Exclusivo`).

---

## Installation & Configuration

1. Install via **HACS → Custom repositories** (`https://github.com/skamoll/hass_solar_orchestrator`, category **Integration**) or copy `custom_components/hass_solar_orchestrator` into `<config>/custom_components/`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration → Solar Excess Orchestrator** and map your entities.
4. Keep **`Operating mode = observing`** while validating decisions against your existing setup, then switch to **`auto_solar`** (or **`auto_ev_modulated`**) once verified.

---

## Contributing & License

See [CONTRIBUTING.md](CONTRIBUTING.md) for local test setup (`pytest`, `ruff`). Licensed under the [MIT License](LICENSE).
