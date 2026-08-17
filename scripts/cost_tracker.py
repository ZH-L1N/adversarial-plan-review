"""Token-cost estimation and cumulative tracking for the reviewer transport.

Phase 1+2 scope (per v2-plan §7 Milestone A): tracking only. Cost-cap pause
gating lands in Phase 4 with the rest of `loop_state.py`.

Cost rates are last-known per the v2-plan model table (cached 2026-04-15) plus
the gpt-5.5 launch in April 2026 and the gpt-5.6 family launch in July 2026.
Operators can override via env vars if their billing tier differs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# Per-1M-token rates in USD. Source: the official pricing pages —
# developers.openai.com/api/docs/pricing and platform.claude.com/docs/en/pricing.
# Take the OpenAI rows from that page ONLY: third-party aggregators publish
# inflated terra/luna figures (2.5/15 and 1.0/6.0 instead of 2.0/12 and
# 0.20/1.20), which is where this table's first draft went wrong.
# Operators on different tiers can override the `gpt*` rows via
# OPENAI_INPUT_USD_PER_1M / OPENAI_OUTPUT_USD_PER_1M env vars without code
# changes.
# Note: gpt-5.6 charges 2x input / 1.5x output on requests >272K input tokens;
# plan reviews stay far below that, so the standard rates are used here.
#
# The `claude-*` rows are the estimate FALLBACK for the claude CLI transport
# only — that path reads `total_cost_usd` straight off the result envelope
# (non-zero even on subscription sessions), so these rates are used solely when
# the field is absent. They must be keyed on the RESOLVED model id from
# `modelUsage[*].canonicalModel`, never on a CLI alias like `opus`.
_DEFAULT_RATES: dict[str, tuple[float, float]] = {
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.6": (5.0, 30.0),  # bare alias routes to sol
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-cyber": (12.50, 75.0),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.4": (5.0, 30.0),
    "gpt-5": (5.0, 30.0),
    "gpt-5-mini": (1.0, 4.0),
    # Every id `_CLAUDE_MODEL_ALIASES` can resolve to needs a row: an unknown
    # model estimates as $0.00, which silently disarms the cost cap on the
    # fallback path.
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


@dataclass
class RoundCost:
    """Cost figures for a single reviewer invocation."""

    tokens_input: int
    tokens_output: int
    cost_usd: float


def estimate_cost_usd(model: str, tokens_input: int, tokens_output: int) -> float:
    """Estimate USD cost from per-1M rates, with env-var override support.

    The `OPENAI_*_USD_PER_1M` overrides are an OpenAI billing-tier knob, so the
    gate ALLOW-lists `gpt*` rather than denying `claude*`: an operator who set
    them for their OpenAI contract must not silently mis-price a claude round,
    and the deny-list form leaked on anything that was neither (a bare `opus`
    CLI alias that escaped model resolution priced at the OpenAI rate). Belt and
    braces with `reviewer.CLAUDE_MODEL_ALIASES`, which keeps the alias from
    reaching this function in the first place.
    """
    input_rate_env = os.environ.get("OPENAI_INPUT_USD_PER_1M")
    output_rate_env = os.environ.get("OPENAI_OUTPUT_USD_PER_1M")
    env_override_applies = model.startswith("gpt")

    if env_override_applies and input_rate_env and output_rate_env:
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
    pattern = f"{version}-{slug}-round-*.json"
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
    prefix = f"{version}-{slug}-round-"
    suffix = ".json"
    name = path.name
    if name.startswith(prefix) and name.endswith(suffix):
        return int(name[len(prefix) : -len(suffix)])
    return -1
