"""Tests verifying the 10-round Consejo de Sabios unanimous consensus (CA-1..CA-5)."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from custom_components.hass_solar_orchestrator.const import (
    OperatingMode,
    ProfileName,
    SafetyState,
    TelemetrySource,
)
from custom_components.hass_solar_orchestrator.engine import (
    SecurityDenylistViolation,
    evaluate,
    evaluate_profile_preview,
    is_forbidden_entity,
)
from custom_components.hass_solar_orchestrator.models import (
    EnergySnapshot,
    OrchestratorConfig,
    OrchestratorState,
)


def _now() -> datetime:
    return datetime(2026, 11, 23, 13, 0, 0)


def test_ca1_engine_and_models_have_zero_homeassistant_imports() -> None:
    """CA-1: Pure engine.py and models.py must never import homeassistant."""
    base = Path(__file__).resolve().parent.parent / "custom_components" / "hass_solar_orchestrator"
    for fname in ("engine.py", "models.py", "const.py"):
        tree = ast.parse((base / fname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("homeassistant")
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("homeassistant")


def test_ca3_manual_total_expires_always_to_observing() -> None:
    """D1 / CA-3: When Manual total expires, engine ALWAYS returns to OBSERVING."""
    now = _now()
    cfg = OrchestratorConfig(
        mode=OperatingMode.MANUAL_TOTAL,
        manual_total_until=now - timedelta(seconds=1),
    )
    snap = EnergySnapshot(
        now=now,
        solar_power_w=4000.0,
        load_power_w=800.0,
        grid_power_w=-3200.0,
        battery_power_w=0.0,
        battery_soc=80.0,
        local_sample_time=now,
        telemetry_source=TelemetrySource.MODBUS,
    )
    plan, next_state = evaluate(snap, cfg, OrchestratorState())
    assert plan.effective_mode == OperatingMode.OBSERVING
    assert next_state.manual_total_expired is True
    assert plan.actions == ()


def test_ca3_denylist_blocks_critical_entities() -> None:
    """CA-3: Immutable anti-cut Denylist rejects general, ICP, inverter, BMS, BYD, router."""
    for bad in (
        "switch.cuadro_general",
        "switch.icp_principal",
        "switch.diferencial_casa",
        "number.sungrow_inverter_limit",
        "switch.byd_bms_power",
        "switch.frigorifico_cocina",
        "switch.router_fibra",
    ):
        assert is_forbidden_entity(bad) is True
    assert is_forbidden_entity("switch.termo_electrico") is False
    assert is_forbidden_entity("switch.depuradora_piscina") is False
    assert SecurityDenylistViolation is not None


def test_ca4_observing_mode_emits_zero_physical_actions() -> None:
    """CA-4: In OBSERVING mode, actions is always empty even with huge solar surplus."""
    now = _now()
    cfg = OrchestratorConfig(mode=OperatingMode.OBSERVING, heater_hold_seconds=0)
    snap = EnergySnapshot(
        now=now,
        solar_power_w=6000.0,
        load_power_w=500.0,
        grid_power_w=-5500.0,
        battery_power_w=0.0,
        battery_soc=95.0,
        local_sample_time=now,
        telemetry_source=TelemetrySource.MODBUS,
        heater_temp_c=35.0,
    )
    plan, _ = evaluate(snap, cfg, OrchestratorState())
    assert plan.actions == ()
    assert plan.effective_mode == OperatingMode.OBSERVING


def test_ca5_unavailable_critical_telemetry_never_coerced_to_zero() -> None:
    """CA-5: Unavailable/invalid telemetry enters DEGRADED and never emits turn_on/step-ups."""
    now = _now()
    cfg = OrchestratorConfig(mode=OperatingMode.AUTO_EV_MODULATED)
    snap = EnergySnapshot(
        now=now,
        solar_power_w=None,
        load_power_w=None,
        grid_power_w=None,
        local_sample_time=None,
        telemetry_source=TelemetrySource.UNAVAILABLE,
        critical_sensor_invalid=True,
    )
    plan, _ = evaluate(snap, cfg, OrchestratorState())
    assert plan.degraded is True
    assert plan.safety_state == SafetyState.DEGRADED
    assert plan.icp_headroom_w is None
    assert all(a.action != "turn_on" for a in plan.actions)


def test_r10_virtual_power_reservation_prevents_double_dispatch() -> None:
    """R10 Devil's Advocate: turn_on reserves nominal W during settling_lock."""
    now = _now()
    cfg = OrchestratorConfig(
        mode=OperatingMode.AUTO_SOLAR,
        heater_hold_seconds=0,
        pool_hold_seconds=0,
        heater_nominal_w=2000.0,
        settling_lock_seconds=15,
    )
    snap0 = EnergySnapshot(
        now=now,
        solar_power_w=3000.0,
        load_power_w=500.0,
        grid_power_w=-2500.0,
        battery_power_w=0.0,
        battery_soc=90.0,
        local_sample_time=now,
        telemetry_source=TelemetrySource.MODBUS,
        heater_temp_c=35.0,
    )
    _, state0 = evaluate(snap0, cfg, OrchestratorState())
    snap1 = EnergySnapshot(
        now=now + timedelta(seconds=1),
        solar_power_w=3000.0,
        load_power_w=500.0,
        grid_power_w=-2500.0,
        battery_power_w=0.0,
        battery_soc=90.0,
        local_sample_time=now + timedelta(seconds=1),
        telemetry_source=TelemetrySource.MODBUS,
        heater_temp_c=35.0,
    )
    plan1, state1 = evaluate(snap1, cfg, state0)
    assert any(a.target == "heater" and a.action == "turn_on" for a in plan1.actions)
    assert state1.virtual_reserved_w == pytest.approx(2000.0)
    assert state1.settling_until is not None

    # 5 seconds later, grid sensor is still stale/slow poll: pool must NOT start!
    snap2 = EnergySnapshot(
        now=now + timedelta(seconds=5),
        solar_power_w=3000.0,
        load_power_w=500.0,
        grid_power_w=-2500.0,
        battery_power_w=0.0,
        battery_soc=90.0,
        local_sample_time=now + timedelta(seconds=5),
        telemetry_source=TelemetrySource.MODBUS,
        heater_on=True,
        heater_temp_c=35.0,
    )
    plan2, _ = evaluate(snap2, cfg, state1)
    assert plan2.settling_remaining_s > 0
    assert not any(a.target == "pool" and a.action == "turn_on" for a in plan2.actions)


def test_profile_preview_and_frontend_assets_exist() -> None:
    """Verify M10 profile preview and frontend sidebar panel JS bundle."""
    preview = evaluate_profile_preview(ProfileName.SAVING, OrchestratorConfig())
    assert preview["profile"] == "saving"
    assert "30%" in preview["house_soc_floor"]

    base = Path(__file__).resolve().parent.parent / "custom_components" / "hass_solar_orchestrator"
    assert (base / "panel.py").is_file()
    assert (base / "websocket_api.py").is_file()
    assert (base / "services.yaml").is_file()
    assert (base / "frontend" / "solar-orchestrator-panel.js").is_file()
