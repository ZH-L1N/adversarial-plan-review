"""Token-cost estimation and cumulative tracking for the reviewer transport.

Phase 1+2 scope (per v2-plan §7 Milestone A): tracking only. Cost-cap pause
gating lands in Phase 4 with the rest of `loop_state.py`.

Cost rates are last-known per the v2-plan model table (cached 2026-04-15) plus
the gpt-5.5 launch in April 2026. Operators can override via env vars if their
billing tier differs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# Per-1M-token rates in USD. Source: v2-plan §3 + April 2026 gpt-5.5 release notes.
# Operators on different tiers can override via OPENAI_INPUT_USD_PER_1M /
# OPENAI_OUTPUT_USD_PER_1M env vars without code changes.
_DEFAULT_RATES: dict[str, tuple[float, float]] = {
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.4": (5.0, 30.0),
    "gpt-5": (5.0, 30.0),
    "gpt-5-mini": (1.0, 4.0),
}


@dataclass
class RoundCost:
    """Cost figures for a single reviewer invocation."""

    tokens_input: int
    tokens_output: int
    cost_usd: float


def estimate_cost_usd(model: str, tokens_input: int, tokens_output: int) -> float:
    """Estimate USD cost from per-1M rates, with env-var override support."""
    input_rate_env = os.environ.get("OPENAI_INPUT_USD_PER_1M")
    output_rate_env = os.environ.get("OPENAI_OUTPUT_USD_PER_1M")

    if input_rate_env and output_rate_env:
        input_rate = float(input_rate_env)
        output_rate = float(output_rate_env)
    else:
        rates = _DEFAULT_RATES.get(model)
        if rates is None:
            # Unknown model — return 0 rather than guess. Caller can log a warning.
            return 0.0
        input_rate, output_rate = rates

    return (tokens_input * input_rate + tokens_output * output_rate) / 1_000_000


class CumulativeCostTracker:
    """Tracks cost across rounds within a single loop run.

    Persisted to / restored from the round JSON sidecar's
    `stats.cumulative_cost_usd` field on resume (Phase 4 wiring).
    """

    def __init__(self, *, initial_cumulative_usd: float = 0.0) -> None:
        self._cumulative_usd = float(initial_cumulative_usd)
        self._per_round: list[RoundCost] = []

    @property
    def cumulative_usd(self) -> float:
        return self._cumulative_usd

    @property
    def per_round(self) -> list[RoundCost]:
        return list(self._per_round)

    def record(
        self,
        *,
        model: str,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float | None = None,
    ) -> RoundCost:
        """Add one round's cost to the cumulative total.

        If `cost_usd` is None, estimate from the rate table; otherwise trust
        the caller's measured figure (e.g. from OpenAI usage response).
        """
        if cost_usd is None:
            cost_usd = estimate_cost_usd(model, tokens_input, tokens_output)
        round_cost = RoundCost(
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
        )
        self._per_round.append(round_cost)
        self._cumulative_usd += cost_usd
        return round_cost


def cap_threshold_usd() -> float:
    """Per-run cost cap (USD). Default $5; overridable via env."""
    return float(os.environ.get("ADVERSARIAL_MAX_COST_USD", "5.0"))


def restore_cumulative_from_sidecars(fixs_dir: Path, slug: str, version: str) -> float:
    """Read the latest round sidecar's stats.cumulative_cost_usd for resume.

    Returns 0.0 if no sidecars exist or none carry cost info. Used by the
    resume flow (§5.9) so cost continuity survives session restarts — a v2
    improvement over v1 which always started cost tracking at 0 on resume.
    """
    pattern = f"{slug}-{version}-round-*.json"
    sidecars = sorted(
        fixs_dir.glob(pattern),
        key=lambda p: _round_number_from_path(p, slug, version),
    )
    if not sidecars:
        return 0.0
    latest = sidecars[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0
    stats = data.get("stats") or {}
    cumulative = stats.get("cumulative_cost_usd")
    if not isinstance(cumulative, (int, float)):
        return 0.0
    return float(cumulative)


def _round_number_from_path(path: Path, slug: str, version: str) -> int:
    """Extract the round number from a sidecar path, for sort ordering."""
    prefix = f"{slug}-{version}-round-"
    suffix = ".json"
    name = path.name
    if name.startswith(prefix) and name.endswith(suffix):
        return int(name[len(prefix) : -len(suffix)])
    return -1
