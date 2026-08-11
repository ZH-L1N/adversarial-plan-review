"""Tests for scripts/cost_tracker.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_tracker import (
    CumulativeCostTracker,
    cap_threshold_usd,
    estimate_cost_usd,
    restore_cumulative_from_sidecars,
)


# --- estimate_cost_usd -------------------------------------------------------


def test_estimate_cost_known_model_gpt55():
    """gpt-5.5 = $5/1M input + $30/1M output."""
    cost = estimate_cost_usd("gpt-5.5", tokens_input=1000, tokens_output=500)
    expected = (1000 * 5 + 500 * 30) / 1_000_000
    assert cost == pytest.approx(expected)


def test_estimate_cost_known_model_gpt55_pro():
    """gpt-5.5-pro = $30/1M input + $180/1M output."""
    cost = estimate_cost_usd("gpt-5.5-pro", 1000, 500)
    expected = (1000 * 30 + 500 * 180) / 1_000_000
    assert cost == pytest.approx(expected)


def test_estimate_cost_known_model_gpt56_sol():
    """gpt-5.6-sol = $5/1M input + $30/1M output (standard <=272K context)."""
    cost = estimate_cost_usd("gpt-5.6-sol", tokens_input=1000, tokens_output=500)
    expected = (1000 * 5 + 500 * 30) / 1_000_000
    assert cost == pytest.approx(expected)


def test_estimate_cost_known_model_claude_opus_5():
    """claude-opus-5 = $5/1M input + $25/1M output, keyed by the CANONICAL id."""
    cost = estimate_cost_usd("claude-opus-5", tokens_input=38209, tokens_output=500)
    expected = (38209 * 5 + 500 * 25) / 1_000_000
    assert cost == pytest.approx(expected)


def test_estimate_cost_known_model_claude_sonnet_5():
    cost = estimate_cost_usd("claude-sonnet-5", tokens_input=1000, tokens_output=500)
    expected = (1000 * 3 + 500 * 15) / 1_000_000
    assert cost == pytest.approx(expected)


def test_estimate_cost_claude_cli_alias_is_not_a_rate_row():
    """The `opus` CLI alias must never reach the rate table — resolve first."""
    assert estimate_cost_usd("opus", 1_000_000, 1_000_000) == 0.0


def test_estimate_cost_openai_env_override_does_not_touch_claude_rows(monkeypatch):
    """OPENAI_*_USD_PER_1M is an OpenAI billing-tier knob, not a global one."""
    monkeypatch.setenv("OPENAI_INPUT_USD_PER_1M", "999.0")
    monkeypatch.setenv("OPENAI_OUTPUT_USD_PER_1M", "999.0")
    cost = estimate_cost_usd("claude-opus-5", tokens_input=1_000_000, tokens_output=0)
    assert cost == pytest.approx(5.0)  # table rate, not the override
    # ...while the openai rows still honour it.
    assert estimate_cost_usd("gpt-5.6-sol", 1_000_000, 0) == pytest.approx(999.0)


def test_estimate_cost_openai_env_override_applies_only_to_gpt_models(monkeypatch):
    """Belt and braces: the gate allow-LISTS `gpt*` instead of denying `claude*`.

    A CLI alias that slipped through model resolution (`opus`, `sonnet`) is
    neither a gpt id nor a `claude-*` id, so the deny-list form silently priced
    it at the operator's OpenAI contract rates.
    """
    monkeypatch.setenv("OPENAI_INPUT_USD_PER_1M", "999.0")
    monkeypatch.setenv("OPENAI_OUTPUT_USD_PER_1M", "999.0")
    for non_openai_model in ("opus", "sonnet", "claude-opus-5-20260101"):
        assert estimate_cost_usd(non_openai_model, 1_000_000, 0) == 0.0
    assert estimate_cost_usd("gpt-5.6-sol", 1_000_000, 0) == pytest.approx(999.0)


def test_estimate_cost_unknown_model_returns_zero():
    """Unknown model → 0.0 (caller can warn/log)."""
    assert estimate_cost_usd("future-flagship", 1_000_000, 1_000_000) == 0.0


def test_estimate_cost_zero_tokens():
    assert estimate_cost_usd("gpt-5.5", 0, 0) == 0.0


def test_estimate_cost_env_override(monkeypatch):
    """OPENAI_INPUT_USD_PER_1M / OUTPUT override the rate table."""
    monkeypatch.setenv("OPENAI_INPUT_USD_PER_1M", "10.0")
    monkeypatch.setenv("OPENAI_OUTPUT_USD_PER_1M", "60.0")
    cost = estimate_cost_usd("gpt-5.5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(10.0 + 60.0)


def test_estimate_cost_env_override_requires_both(monkeypatch):
    """Setting only one env var falls back to the table."""
    monkeypatch.setenv("OPENAI_INPUT_USD_PER_1M", "10.0")
    cost = estimate_cost_usd("gpt-5.5", 1_000_000, 0)
    # Only INPUT set → falls back to table → $5
    assert cost == pytest.approx(5.0)


# --- CumulativeCostTracker ---------------------------------------------------


def test_tracker_starts_at_zero():
    tracker = CumulativeCostTracker()
    assert tracker.cumulative_usd == 0.0
    assert tracker.per_round == []


def test_tracker_initial_value_restored():
    tracker = CumulativeCostTracker(initial_cumulative_usd=1.5)
    assert tracker.cumulative_usd == 1.5


def test_tracker_record_with_cost_usd_passed():
    tracker = CumulativeCostTracker()
    rc = tracker.record(model="gpt-5.5", tokens_input=100, tokens_output=50, cost_usd=0.25)
    assert rc.cost_usd == 0.25
    assert tracker.cumulative_usd == 0.25


def test_tracker_record_with_estimated_cost():
    tracker = CumulativeCostTracker()
    rc = tracker.record(model="gpt-5.5", tokens_input=1000, tokens_output=500)
    expected = (1000 * 5 + 500 * 30) / 1_000_000
    assert rc.cost_usd == pytest.approx(expected)
    assert tracker.cumulative_usd == pytest.approx(expected)


def test_tracker_accumulates_across_rounds():
    tracker = CumulativeCostTracker()
    tracker.record(model="gpt-5.5", tokens_input=1000, tokens_output=500, cost_usd=0.1)
    tracker.record(model="gpt-5.5", tokens_input=2000, tokens_output=1000, cost_usd=0.2)
    tracker.record(model="gpt-5.5", tokens_input=3000, tokens_output=1500, cost_usd=0.3)
    assert tracker.cumulative_usd == pytest.approx(0.6)
    assert len(tracker.per_round) == 3


def test_tracker_per_round_returns_copy():
    """Mutating the returned list doesn't corrupt internal state."""
    tracker = CumulativeCostTracker()
    tracker.record(model="gpt-5.5", tokens_input=100, tokens_output=50, cost_usd=0.1)
    snapshot = tracker.per_round
    snapshot.clear()  # mutate the returned copy
    assert len(tracker.per_round) == 1  # internal state preserved


# --- cap_threshold_usd -------------------------------------------------------


def test_cap_threshold_default():
    assert cap_threshold_usd() == 5.0


def test_cap_threshold_env_override(monkeypatch):
    monkeypatch.setenv("ADVERSARIAL_MAX_COST_USD", "20.0")
    assert cap_threshold_usd() == 20.0


# --- restore_cumulative_from_sidecars ----------------------------------------


def test_restore_cumulative_no_sidecars(tmp_path):
    fixs = tmp_path / "fixs"
    fixs.mkdir()
    assert restore_cumulative_from_sidecars(fixs, "test", "v1") == 0.0


def test_restore_cumulative_reads_latest_sidecar(tmp_path):
    fixs = tmp_path / "fixs"
    fixs.mkdir()
    for n, cost in [(1, 0.10), (2, 0.30), (3, 0.55)]:
        sidecar = {"stats": {"cumulative_cost_usd": cost}}
        (fixs / f"v1-test-round-{n}.json").write_text(json.dumps(sidecar))
    assert restore_cumulative_from_sidecars(fixs, "test", "v1") == 0.55


def test_restore_cumulative_picks_highest_round_number(tmp_path):
    """Lexicographic sort would put round-10 BEFORE round-2; numeric sort fixes."""
    fixs = tmp_path / "fixs"
    fixs.mkdir()
    for n, cost in [(1, 0.05), (10, 1.50), (2, 0.10)]:
        sidecar = {"stats": {"cumulative_cost_usd": cost}}
        (fixs / f"v1-test-round-{n}.json").write_text(json.dumps(sidecar))
    assert restore_cumulative_from_sidecars(fixs, "test", "v1") == 1.50


def test_restore_cumulative_handles_missing_field(tmp_path):
    """Sidecar missing cumulative_cost_usd → 0.0, not exception."""
    fixs = tmp_path / "fixs"
    fixs.mkdir()
    (fixs / "v1-test-round-1.json").write_text(json.dumps({"stats": {}}))
    assert restore_cumulative_from_sidecars(fixs, "test", "v1") == 0.0


def test_restore_cumulative_handles_corrupt_json(tmp_path):
    """Corrupt JSON → 0.0 (defensive; resume can still proceed)."""
    fixs = tmp_path / "fixs"
    fixs.mkdir()
    (fixs / "v1-test-round-1.json").write_text("not json")
    assert restore_cumulative_from_sidecars(fixs, "test", "v1") == 0.0
