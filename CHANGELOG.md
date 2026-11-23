# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-23

### Added
- Pure Python deterministic orchestration engine (`engine.py` + `models.py`) with zero Home Assistant dependencies and unit test suite (`tests/test_engine.py`).
- Native Home Assistant integration (`ConfigFlow`, `OptionsFlow`, `Reconfigure`, persistent storage, `Repairs`, and bilingual `en`/`es` UI).
- Operating modes (`observing`, `auto_solar`, `auto_ev_modulated`, `force_ev`, `house_priority`, `paused`), live setpoints (`number`), load toggles (`switch`), and explanatory diagnostics (`sensor` + `binary_sensor`).
