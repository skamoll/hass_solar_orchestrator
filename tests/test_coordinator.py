"""Regression tests for coordinator decisions at the HA boundary."""

from __future__ import annotations

import importlib.util
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

if importlib.util.find_spec("homeassistant") is None:
    pytest.skip("homeassistant package not installed", allow_module_level=True)

from homeassistant.exceptions import ServiceValidationError

from custom_components.hass_solar_orchestrator import coordinator as integration
from custom_components.hass_solar_orchestrator.const import (
    CONF_CLOUD_GRID_POWER_ENTITY,
    CONF_CLOUD_PV_POWER_ENTITY,
    CONF_EV_MIN_CURRENT_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_HEATER_SWITCH_ENTITY,
    CONF_SOLAR_POWER_ENTITY,
    CONF_WEATHER_ENTITY,
    OperatingMode,
    ProfileName,
    TelemetrySource,
)
from custom_components.hass_solar_orchestrator.models import LoadAction, OrchestratorState

NOW = datetime(2026, 6, 13, 10, tzinfo=UTC)


@pytest.fixture
def make_coordinator(monkeypatch):
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: NOW)
    # No HA startup, filesystem storage or live services.
    monkeypatch.setattr(integration, "Store", MagicMock())
    monkeypatch.setattr(
        integration.DataUpdateCoordinator,
        "__init__",
        lambda self, hass, *args, **kwargs: setattr(self, "hass", hass),
    )

    def make():
        hass = MagicMock()
        states = {}
        hass.states.get.side_effect = states.get
        hass.states.async_all.return_value = []
        hass.services.async_call = AsyncMock()
        entry = SimpleNamespace(
            entry_id="test",
            data={
                CONF_GRID_POWER_ENTITY: "sensor.grid",
                CONF_SOLAR_POWER_ENTITY: "sensor.solar",
                CONF_HEATER_SWITCH_ENTITY: "switch.water",
            },
            options={},
        )
        coord = integration.SolarOrchestratorCoordinator(hass, entry)
        coord._store = AsyncMock()
        coord.async_request_refresh = AsyncMock()
        coord.config.boot_grace_seconds = 0
        coord.config.load_modes["heater"] = "active"
        coord.config.mode = OperatingMode.AUTO_SOLAR

        def report(entity, value, unit="W", age=0):
            states[entity] = SimpleNamespace(
                state=value,
                attributes={"unit_of_measurement": unit},
                last_reported=NOW - timedelta(seconds=age),
            )

        report("sensor.grid", "-2000")
        report("sensor.solar", "2.5", "kW")
        report("switch.water", "off")
        return coord, report

    return make


@pytest.mark.parametrize(
    "patch",
    [
        {"contracted_power_w": 500},
        {"icp_margin_w": -10},
        {"icp_margin_w": 10000},
        {"ev_max_amps": 99},
        {"ev_max_amps": 6.5},
        {"tariff_p1_eur": "nan"},
        {"quiet_hours_start": "25:00"},
        {"pool_enabled": "false"},
        {"tariff_mode": "typo"},
    ],
)
async def test_rejected_patch_is_atomic(make_coordinator, patch):
    coord, _ = make_coordinator()
    with pytest.raises(ServiceValidationError):
        await coord.async_save_settings({"heater_enabled": False, **patch})
    assert coord.config.heater_enabled is True
    coord._store.async_save.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


async def test_invalid_service_inputs_leave_state_untouched(make_coordinator):
    coord, _ = make_coordinator()
    with pytest.raises(ServiceValidationError):
        await coord.async_set_priorities(["house", "ev", "ev", "pool"])
    with pytest.raises(ServiceValidationError):
        await coord.async_set_ev_commitment(50, "25:33", True, "07:00")
    with pytest.raises(ServiceValidationError):
        await coord.async_override_load("heater", "charge_now")
    with pytest.raises(ServiceValidationError):
        await coord.async_apply_profile(ProfileName.VACATION, vacation_until="tomorrow")
    assert coord.config.profile == ProfileName.COMFORT
    assert coord.config.ev_target_soc == 90
    coord._store.async_save.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "unit", "age"),
    [
        ("2", "kW", 60),
        ("2", "kW", -60),
        ("NaN", "W", 0),
        ("Inf", "W", 0),
        ("5", "kWh", 0),
    ],
)
async def test_invalid_or_old_solar_cannot_enable_starts(make_coordinator, value, unit, age):
    coord, report = make_coordinator()
    assert coord._build_snapshot().solar_power_w == 2500
    report("sensor.solar", value, unit, age)
    report("sensor.grid", "-2000")
    report("number.ev_amps", "nan")
    coord.entry.data[CONF_EV_MIN_CURRENT_ENTITY] = "number.ev_amps"
    plan = await coord._async_update_data()
    assert plan.degraded
    assert coord._last_snapshot.ev_min_amps_a == coord.config.ev_min_amps
    coord.hass.services.async_call.assert_not_awaited()


async def test_failed_start_never_claims_manual_switch(make_coordinator):
    coord, _ = make_coordinator()
    coord.config.heater_hold_seconds = 0
    coord.runtime_state.heater_export_since = NOW - timedelta(minutes=10)
    coord.hass.services.async_call.side_effect = RuntimeError("device offline")
    plan = await coord._async_update_data()
    assert any(a.target == "heater" and a.action == "turn_on" for a in plan.actions)
    assert coord.blackbox[0]["type"] == "action_error"
    assert not coord.runtime_state.heater_solar_owned
    assert coord.runtime_state.heater_last_changed_at is None
    assert not coord._store.async_save.await_args.args[0]["heater_solar_owned"]


@pytest.mark.parametrize("failure", ["unmapped", "service", "denylisted"])
async def test_failed_shed_keeps_ownership_and_retries(make_coordinator, failure):
    coord, report = make_coordinator()
    if failure == "denylisted":
        coord.entry.data[CONF_HEATER_SWITCH_ENTITY] = "switch.general"
        report("switch.general", "on")
    else:
        report("switch.water", "on")
    coord.config.mode = OperatingMode.HOUSE_PRIORITY
    coord.runtime_state.heater_solar_owned = True
    if failure == "service":
        coord.hass.services.async_call.side_effect = RuntimeError("offline")
    elif failure == "unmapped":
        # Mapping disappears after acquisition but before physical dispatch.
        build = coord._build_snapshot

        def snapshot():
            result = build()
            del coord.entry.data[CONF_HEATER_SWITCH_ENTITY]
            return result

        coord._build_snapshot = snapshot
    plan = await coord._async_update_data()
    assert any(a.target == "heater" and a.action == "turn_off" for a in plan.actions)
    assert coord.runtime_state.heater_solar_owned
    assert coord.runtime_state.handoff_waiting_for_off is None
    assert all(not event["executed"] for event in coord.blackbox)


async def test_electrical_shed_works_during_shower_veto(make_coordinator):
    coord, report = make_coordinator()
    coord.runtime_state.shower_veto_until = NOW + timedelta(minutes=10)
    coord.runtime_state.heater_solar_owned = True
    report("switch.water", "on")
    report("sensor.solar", "unavailable")
    report("sensor.grid", "7000")
    await coord._async_execute_actions(
        (LoadAction("heater", "turn_off", reason="heater_target_temp"),)
    )
    coord.hass.services.async_call.assert_not_awaited()
    await coord._async_update_data()
    coord.hass.services.async_call.assert_awaited_once_with(
        "switch",
        "turn_off",
        {"entity_id": "switch.water"},
        blocking=True,
    )
    assert not coord.runtime_state.heater_solar_owned
    assert coord.runtime_state.handoff_waiting_for_off == "heater"


async def test_restore_timers_and_expire_boost(make_coordinator):
    coord, _ = make_coordinator()
    await coord.async_override_load("heater", "off", 60)
    coord.runtime_state.pool_external_override_until = NOW + timedelta(minutes=20)
    await coord.async_override_load("ev", "charge_now", 30)
    assert coord.config.mode == OperatingMode.FORCE_EV
    stored = dict(coord._store.async_save.await_args.args[0])
    stored["ev_boost_until"] = (NOW - timedelta(minutes=1)).isoformat()
    restarted, _ = make_coordinator()
    restarted._store.async_load.return_value = stored
    await restarted.async_load_state()
    assert restarted.config.heater_override == "off"
    assert restarted.config.heater_override_until == NOW + timedelta(hours=1)
    assert restarted.runtime_state.pool_external_override_until == NOW + timedelta(minutes=20)
    plan = await restarted._async_update_data()
    assert plan.effective_mode == OperatingMode.AUTO_SOLAR
    assert restarted.config.ev_boost_until is None


async def test_invalid_legacy_settings_boot_in_observing(make_coordinator):
    coord, _ = make_coordinator()
    coord._store.async_load.return_value = {
        "mode": "force_ev",
        "contracted_power_w": "n/a",
        "shower_veto_until": 123,
        "pool_minutes_today": 34,
    }
    await coord.async_load_state()
    assert coord.config.mode == OperatingMode.OBSERVING
    assert coord.runtime_state.pool_minutes_today == 34
    assert coord.runtime_state.shower_veto_until is None


@pytest.mark.parametrize("stop_method", ["async_shutdown", "async_handle_shutdown"])
async def test_routine_checkpoints_and_final_flush(make_coordinator, monkeypatch, stop_method):
    coord, _ = make_coordinator()
    coord.config.mode = OperatingMode.OBSERVING
    monkeypatch.setattr(integration.DataUpdateCoordinator, "async_shutdown", AsyncMock())
    for _ in range(3):
        await coord._async_update_data()
    assert coord._store.async_save.await_count == 1
    coord._next_checkpoint = 0
    await coord._async_update_data()
    assert coord._store.async_save.await_count == 2
    await coord.async_save_settings({"heater_enabled": False})
    assert coord._store.async_save.await_count == 3
    coord.runtime_state.pool_minutes_today = 10
    await coord._async_update_data()
    assert coord._store.async_save.await_count == 3
    await getattr(coord, stop_method)()
    assert coord._store.async_save.await_count == 4
    assert coord._store.async_save.await_args.args[0]["pool_minutes_today"] == 10


async def test_successful_action_checkpoints_ownership_immediately(make_coordinator):
    coord, _ = make_coordinator()
    await coord._async_update_data()
    coord.runtime_state.heater_export_since = NOW - timedelta(minutes=10)
    await coord._async_update_data()
    assert coord._store.async_save.await_count == 2
    assert coord._store.async_save.await_args.args[0]["heater_solar_owned"]


async def test_failed_grid_shed_keeps_hold_timer_for_immediate_retry(make_coordinator):
    coord, report = make_coordinator()
    report("switch.water", "on")
    report("sensor.grid", "1500")
    report("sensor.solar", "0.3", "kW")
    coord.runtime_state.heater_solar_owned = True
    coord.runtime_state.heater_last_changed_at = NOW - timedelta(hours=1)
    held_since = NOW - timedelta(minutes=10)
    coord.runtime_state.heater_shed_since = held_since
    coord.hass.services.async_call.side_effect = RuntimeError("offline")
    plan = await coord._async_update_data()
    assert any(a.target == "heater" and a.action == "turn_off" for a in plan.actions), plan
    assert coord.runtime_state.heater_solar_owned
    assert coord.runtime_state.heater_shed_since == held_since
    coord.hass.services.async_call.side_effect = None
    coord.hass.services.async_call.reset_mock()
    plan = await coord._async_update_data()
    coord.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.water"}, blocking=True
    )
    assert not coord.runtime_state.heater_solar_owned
    assert coord.runtime_state.heater_shed_since is None


async def test_store_failure_does_not_fail_refresh(make_coordinator):
    coord, _ = make_coordinator()
    coord._store.async_save.side_effect = OSError("disk full")
    plan = await coord._async_update_data()
    assert plan is not None
    assert coord._urgent_save or coord._checkpoint_pending
    coord._store.async_save.side_effect = None
    await coord._async_update_data()
    assert coord._store.async_save.await_count == 2


async def test_restore_drops_unknown_protected_and_blocked_loads(make_coordinator):
    coord, _ = make_coordinator()
    coord._store.async_load.return_value = {
        "protected_loads": ["ev", "garage", "ev", {"x": 1}],
        "blocked_loads": ["pool", None],
    }
    await coord.async_load_state()
    assert coord.config.protected_loads == ("ev",)
    assert coord.config.blocked_loads == ("pool",)


def test_persisted_timestamp_fields_cover_orchestrator_state():
    past = set(integration._PAST_TIMESTAMP_STATE_FIELDS)
    until = set(integration._UNTIL_TIMESTAMP_STATE_FIELDS)
    transient = set(integration._TRANSIENT_TIMESTAMP_STATE_FIELDS)

    assert not (past & until)
    assert not (past & transient)
    assert not (until & transient)

    state_dt_fields = {f.name for f in fields(OrchestratorState) if f.type == "datetime | None"}
    categorized = past | until | transient
    assert state_dt_fields == categorized


async def test_persist_and_restore_hold_shed_and_cooldown_timers(make_coordinator):
    coord, _ = make_coordinator()
    coord.runtime_state.heater_shed_since = NOW - timedelta(seconds=90)
    coord.runtime_state.aux_shed_since = NOW - timedelta(seconds=45)
    coord.runtime_state.heater_export_since = NOW - timedelta(minutes=3)
    coord.runtime_state.pool_export_since = NOW - timedelta(minutes=2)
    coord.runtime_state.ev_step_up_since = NOW - timedelta(seconds=30)
    coord.runtime_state.pool_cooldown_until = NOW + timedelta(minutes=15)
    coord.runtime_state.settling_until = NOW + timedelta(seconds=20)
    await coord.async_save_state()

    stored = dict(coord._store.async_save.await_args.args[0])
    restarted, _ = make_coordinator()
    restarted.config.boot_grace_seconds = 60
    restarted._store.async_load.return_value = stored
    await restarted.async_load_state()

    assert restarted.runtime_state.heater_shed_since == NOW - timedelta(seconds=90)
    assert restarted.runtime_state.aux_shed_since == NOW - timedelta(seconds=45)
    assert restarted.runtime_state.heater_export_since == NOW - timedelta(minutes=3)
    assert restarted.runtime_state.pool_export_since == NOW - timedelta(minutes=2)
    assert restarted.runtime_state.ev_step_up_since == NOW - timedelta(seconds=30)
    assert restarted.runtime_state.pool_cooldown_until == NOW + timedelta(minutes=15)
    assert restarted.runtime_state.settling_until == NOW + timedelta(seconds=20)
    assert restarted.runtime_state.started_at == NOW

    plan = await restarted._async_update_data()
    assert "boot_grace_active" in plan.reasons


async def test_restore_drops_invalid_and_future_past_timestamps_and_keeps_legacy_compat(
    make_coordinator,
):
    coord, _ = make_coordinator()
    coord._store.async_load.return_value = {
        "mode": "auto_solar",
        # Legacy state without new timer fields; plus invalid/future values
        "heater_shed_since": (NOW + timedelta(minutes=5)).isoformat(),
        "aux_shed_since": "not-a-timestamp",
        "heater_export_since": 12345,
        "pool_cooldown_until": (NOW + timedelta(minutes=10)).isoformat(),
        "settling_until": "bad",
    }
    await coord.async_load_state()
    assert coord.runtime_state.heater_shed_since is None
    assert coord.runtime_state.aux_shed_since is None
    assert coord.runtime_state.heater_export_since is None
    assert coord.runtime_state.pool_export_since is None
    assert coord.runtime_state.pool_cooldown_until == NOW + timedelta(minutes=10)
    assert coord.runtime_state.settling_until is None


async def test_weather_fetch_exponential_backoff_and_recovery(make_coordinator, monkeypatch):
    current_now = NOW
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: current_now)
    coord, report = make_coordinator()
    coord.entry.data[CONF_WEATHER_ENTITY] = "weather.home"
    report("weather.home", "sunny")

    coord.hass.services.async_call.side_effect = RuntimeError("weather api down")
    await coord._async_refresh_weather_cache()
    assert coord._weather_failures == 1
    assert coord._weather_next_retry_at == current_now + timedelta(minutes=1)
    assert coord.hass.services.async_call.await_count == 1

    # Inside backoff window: must not retry
    current_now += timedelta(seconds=30)
    await coord._async_refresh_weather_cache()
    assert coord.hass.services.async_call.await_count == 1

    # Past 1-min backoff: retries and doubles to 2 min
    current_now += timedelta(seconds=31)
    await coord._async_refresh_weather_cache()
    assert coord.hass.services.async_call.await_count == 2
    assert coord._weather_failures == 2
    assert coord._weather_next_retry_at == current_now + timedelta(minutes=2)

    # Verify cap at 30 min (after 6+ failures)
    for _ in range(5):
        current_now = coord._weather_next_retry_at
        await coord._async_refresh_weather_cache()
    assert coord._weather_next_retry_at == current_now + timedelta(minutes=30)

    snap_diag = coord.get_panel_snapshot()["snapshot"]
    assert snap_diag["weather_fetch_failed"] is True
    assert snap_diag["weather_failures"] == 7
    assert "weather api down" in snap_diag["weather_last_error"]
    assert snap_diag["weather_next_retry_at"] is not None

    # Recovery resets backoff and failure state
    current_now = coord._weather_next_retry_at
    coord.hass.services.async_call.side_effect = None
    coord.hass.services.async_call.return_value = {
        "weather.home": {"forecast": [{"condition": "sunny"}]}
    }
    await coord._async_refresh_weather_cache()
    assert coord._weather_failures == 0
    assert coord._weather_next_retry_at is None
    assert coord._weather_last_error is None
    assert coord._weather_cache == [{"condition": "sunny"}]
    snap_diag = coord.get_panel_snapshot()["snapshot"]
    assert snap_diag["weather_fetch_failed"] is False


async def test_weather_fetch_keeps_last_good_forecast_within_validity_and_drops_after(
    make_coordinator, monkeypatch
):
    current_now = NOW
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: current_now)
    coord, report = make_coordinator()
    coord.entry.data[CONF_WEATHER_ENTITY] = "weather.home"
    report("weather.home", "sunny")
    coord.hass.services.async_call.return_value = {
        "weather.home": {"forecast": [{"condition": "rainy"}]}
    }
    await coord._async_refresh_weather_cache()
    assert coord._build_snapshot().weather_daily_forecast == ({"condition": "rainy"},)

    # Past 30-min refresh TTL, fetch fails -> last good forecast kept within 6h validity
    current_now += timedelta(minutes=35)
    coord.hass.services.async_call.side_effect = RuntimeError("timeout")
    await coord._async_refresh_weather_cache()
    assert coord._weather_failures == 1
    assert coord._build_snapshot().weather_daily_forecast == ({"condition": "rainy"},)

    # Past 6h validity -> stale forecast dropped
    current_now += timedelta(hours=6)
    await coord._async_refresh_weather_cache()
    assert coord._build_snapshot().weather_daily_forecast == ()


async def test_cloud_fallback_fresh_surfaces_distinct_status_and_blocks_starts(make_coordinator):
    coord, report = make_coordinator()
    coord.config.heater_hold_seconds = 0
    coord.runtime_state.heater_export_since = NOW - timedelta(minutes=10)
    coord.entry.data[CONF_CLOUD_PV_POWER_ENTITY] = "sensor.cloud_solar"
    coord.entry.data[CONF_CLOUD_GRID_POWER_ENTITY] = "sensor.cloud_grid"
    report("sensor.solar", "unavailable")
    report("sensor.grid", "unavailable")
    report("sensor.cloud_solar", "3000", "W", age=5)
    report("sensor.cloud_grid", "-2500", "W", age=5)

    plan = await coord._async_update_data()
    coord.data = plan
    assert coord._last_snapshot.telemetry_source == TelemetrySource.CLOUD_FALLBACK
    assert plan.degraded is True
    coord.hass.services.async_call.assert_not_awaited()

    panel = coord.get_panel_snapshot()
    assert panel["snapshot"]["telemetry_source"] == "cloud_fallback"
    assert panel["snapshot"]["telemetry_status"] == "cloud_fallback"
    assert panel["snapshot"]["cloud_fallback"] is True
    assert panel["plan"]["telemetry_status"] == "cloud_fallback"


async def test_cloud_fallback_stale_cannot_enable_starts_and_marks_unavailable(make_coordinator):
    coord, report = make_coordinator()
    coord.config.heater_hold_seconds = 0
    coord.runtime_state.heater_export_since = NOW - timedelta(minutes=10)
    coord.entry.data[CONF_CLOUD_PV_POWER_ENTITY] = "sensor.cloud_solar"
    coord.entry.data[CONF_CLOUD_GRID_POWER_ENTITY] = "sensor.cloud_grid"
    report("sensor.solar", "unavailable")
    report("sensor.grid", "unavailable")
    report("sensor.cloud_solar", "3000", "W", age=120)
    report("sensor.cloud_grid", "-2500", "W", age=120)

    plan = await coord._async_update_data()
    coord.data = plan
    assert coord._last_snapshot.telemetry_source == TelemetrySource.UNAVAILABLE
    assert plan.degraded is True
    coord.hass.services.async_call.assert_not_awaited()

    panel = coord.get_panel_snapshot()
    assert panel["snapshot"]["telemetry_source"] == "unavailable"
    assert panel["snapshot"]["telemetry_status"] == "degraded"
    assert panel["snapshot"]["cloud_fallback"] is False
