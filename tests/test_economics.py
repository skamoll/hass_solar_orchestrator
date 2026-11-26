"""Tariff, precision and local calendar regressions for the deterministic engine."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hass_solar_orchestrator.engine import evaluate
from custom_components.hass_solar_orchestrator.models import (
    EnergySnapshot,
    OrchestratorConfig,
    OrchestratorState,
)


def test_small_export_payments_accumulate_at_ten_second_intervals():
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    cfg = OrchestratorConfig(tariff_mode="manual_20td", export_comp_eur=0.08)
    state = OrchestratorState()
    for i in range(361):
        tick = now + timedelta(seconds=10 * i)
        snapshot = EnergySnapshot(
            now=tick, local_sample_time=tick, grid_power_w=-100, solar_power_w=100
        )
        plan, state = evaluate(snapshot, cfg, state)
    assert state.grid_export_revenue_today_eur == pytest.approx(0.008)
    assert plan.grid_export_revenue_today_eur == 0.01


def test_tariff_and_daily_quotas_follow_local_calendar():
    # Monday 10:05 in Madrid during summer time, 08:05 UTC.
    now = datetime(2026, 6, 1, 8, 5, tzinfo=UTC)
    snap = EnergySnapshot(
        now=now,
        calendar_timezone=ZoneInfo("Europe/Madrid"),
        local_sample_time=now,
        grid_power_w=1000,
        solar_power_w=0,
    )
    cfg = OrchestratorConfig(tariff_mode="manual_20td", tariff_p1_eur=0.30, tariff_p2_eur=0.1)
    state = OrchestratorState(savings_last_tick=now - timedelta(minutes=5))
    _, state = evaluate(snap, cfg, state)
    assert state.grid_import_cost_today_eur == pytest.approx(0.025)
    midnight = datetime(2026, 6, 1, 22, tzinfo=UTC)
    _, state = evaluate(replace(snap, now=midnight, local_sample_time=midnight), cfg, state)
    assert state.savings_last_day == state.pool_last_day == "2026-06-02"
    assert state.grid_import_cost_today_eur == 0


def test_missing_and_negative_sensor_tariff():
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    snap = EnergySnapshot(now=now, local_sample_time=now, grid_power_w=2000, solar_power_w=0)
    cfg = OrchestratorConfig(tariff_mode="sensor")
    plan, state = evaluate(snap, cfg, OrchestratorState())
    assert plan.grid_import_cost_today_eur is None
    later = now + timedelta(minutes=10)
    _, state = evaluate(
        replace(snap, now=later, local_sample_time=later, tariff_price_eur=-0.15), cfg, state
    )
    assert state.grid_import_cost_today_eur == pytest.approx(-0.05)
