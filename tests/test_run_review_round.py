"""Tests for scripts/run_review_round.py.

The integration tests here are the point of the extraction. The original defect
— a fallback argv containing a *comment* where `--diff-file` should be — was
invisible to every green suite because nothing can import a Markdown code
block. A test that asserts against a MOCKED builder would still miss it: the
builder is what rejects `--round > 1` without a diff. So these run the real
`build_reviewer_prompt_v2.py` CLI and mock only `invoke_reviewer`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import run_review_round as rrr
from loop_state import (
    PlannerDecision,
    RoundState,
    build_sidecar,
    take_initial_snapshot,
    write_sidecar_atomic,
)
from parse_review import (
    Finding,
    ReviewResult,
    ReviewSchemaError,
    ReviewUsage,
)
from reviewer import QuotaExhaustedError, TransportError, TransportSelection

SLUG, VERSION = "demo", "v0.1"
PLAN_R1 = "# Demo plan\n\nOriginal body.\n"
PLAN_R2 = "# Demo plan\n\nOriginal body.\n\n## Added in round 1\n\nNew section.\n"


def _result(*, transport="claude", model="claude-opus-5", cost=0.25):
    return ReviewResult(
        status="FINDINGS_PRESENT",
        findings=[Finding("medium", "cat", "where", "what", "fix")],
        open_questions=[],
        raw_response_text="{}",
        transport=transport,
        model=model,
        usage=ReviewUsage(1000, 500, cost),
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with a completed round 1: plan, snapshot, and sidecar on disk."""
    (tmp_path / "plans" / "fixs").mkdir(parents=True)
    plan = tmp_path / "plans" / f"{VERSION}-{SLUG}.md"
    plan.write_text(PLAN_R1, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    take_initial_snapshot(plan, slug=SLUG, version=VERSION)

    state = RoundState(
        round_n=1,
        slug=SLUG,
        version=VERSION,
        transport="openai",
        model="gpt-5.6-sol",
        started_at="2026-08-16T00:00:00Z",
        completed_at="2026-08-16T00:01:00Z",
        reviewer_response=_result(transport="openai", model="gpt-5.6-sol"),
        decisions=[PlannerDecision("f_r1_1", "accept", "ok", "edited")],
        plan_content_at_end=PLAN_R1,
        baseline_plan_content=PLAN_R1,
        cumulative_cost_usd=0.25,
        duration_seconds=60.0,
    )
    write_sidecar_atomic(
        build_sidecar(state, raw_response_text="{}"), slug=SLUG, version=VERSION
    )

    # Round 1's accepted finding produced this edit; round 2 diffs against it.
    plan.write_text(PLAN_R2, encoding="utf-8")
    return tmp_path


# --- integration: the test that would have caught the original defect --------


def test_round2_quota_fallback_rebuilds_a_usable_claude_prompt(repo, monkeypatch):
    """The fallback must produce a prompt the REAL builder accepted.

    This is the regression test for the original defect. The old fallback
    passed a comment instead of `--diff-file`, so the builder exited 2 and
    `check=True` raised before Claude was ever invoked — from round 2 onward
    the advertised quota fallback could not work at all.
    """
    prompts: list[str] = []
    calls: list[str] = []

    def fake_invoke(prompt, *, round_n, transport=None, repo_root=None, model=None):
        prompts.append(prompt)
        calls.append(transport.name)
        if transport.name == "openai":
            raise QuotaExhaustedError("no credit")
        return _result()

    monkeypatch.setattr(rrr, "invoke_reviewer", fake_invoke)
    monkeypatch.setattr(rrr, "_is_claude_cli_available", lambda env: True)

    outcome = rrr.run_review_round(
        repo_root=repo,
        slug=SLUG,
        version=VERSION,
        round_n=2,
        selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
        cumulative_cost_usd=0.25,
    )

    assert calls == ["openai", "claude"]
    assert outcome.transport == "claude"

    # Both prompts came out of the real builder, so reaching here at all means
    # the rebuilt argv was accepted for round 2.
    assert len(prompts) == 2
    claude_prompt = prompts[1]
    assert "Added in round 1" in claude_prompt, "round-2 diff missing from rebuild"
    assert "0.25" in claude_prompt, "cumulative cost not carried into the rebuild"
    # The claude calibration blocks are the whole reason for rebuilding rather
    # than reusing the openai prompt.
    assert claude_prompt != prompts[0]


def test_round2_rebuild_keeps_the_degraded_diff_warning(repo, monkeypatch):
    """`--diff-recovered-from-git` must travel with the diff on BOTH builds.

    Carrying the bytes without the flag keeps the prompt syntactically valid
    while silently changing what it claims about the diff's accuracy — the same
    class of omission as the original defect, one flag over.
    """
    monkeypatch.setattr(
        rrr, "compute_round_diff", lambda *a, **k: ("--- a\n+++ b\n+recovered\n", True)
    )
    seen: list[list[str]] = []
    real_argv = rrr._BuildContext.argv

    def spy_argv(self, transport_name):
        argv = real_argv(self, transport_name)
        seen.append(argv)
        return argv

    monkeypatch.setattr(rrr._BuildContext, "argv", spy_argv)

    def fake_invoke(prompt, *, round_n, transport=None, repo_root=None, model=None):
        if transport.name == "openai":
            raise QuotaExhaustedError("no credit")
        return _result()

    monkeypatch.setattr(rrr, "invoke_reviewer", fake_invoke)
    monkeypatch.setattr(rrr, "_is_claude_cli_available", lambda env: True)

    rrr.run_review_round(
        repo_root=repo,
        slug=SLUG,
        version=VERSION,
        round_n=2,
        selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
    )

    assert len(seen) == 2
    for argv in seen:
        assert "--diff-recovered-from-git" in argv
        assert "--diff-file" in argv


def test_round1_passes_no_diff_file(repo, monkeypatch):
    seen: list[list[str]] = []
    real_argv = rrr._BuildContext.argv
    monkeypatch.setattr(
        rrr._BuildContext,
        "argv",
        lambda self, t: (seen.append(real_argv(self, t)) or seen[-1]),
    )
    monkeypatch.setattr(
        rrr, "invoke_reviewer",
        lambda p, **k: _result(transport="openai", model="gpt-5.6-sol"),
    )

    rrr.run_review_round(
        repo_root=repo,
        slug=SLUG,
        version=VERSION,
        round_n=1,
        selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
    )
    assert "--diff-file" not in seen[0]


# --- attempt policy ---------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected_calls",
    [
        (TransportError("t", kind="max_turns"), 2),
        (TransportError("t", kind="max_budget"), 2),
        (TransportError("t", kind="api"), 2),
        (ReviewSchemaError("bad json"), 2),
        (TransportError("t", kind="wall_timeout"), 1),
        (TransportError("t", kind="permanent"), 1),
    ],
)
def test_retry_policy_per_kind(repo, monkeypatch, exc, expected_calls):
    """Retry-once covers malformed output too — it was already paid for.

    `wall_timeout` is excluded because the retry inherits the same timeout: one
    round would cost twice the wait and fail anyway.
    """
    calls = {"n": 0}

    def fake_invoke(prompt, **kwargs):
        calls["n"] += 1
        raise exc

    monkeypatch.setattr(rrr, "invoke_reviewer", fake_invoke)

    with pytest.raises(rrr.RoundRunError) as excinfo:
        rrr.run_review_round(
            repo_root=repo,
            slug=SLUG,
            version=VERSION,
            round_n=1,
            selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
        )
    assert calls["n"] == expected_calls
    # The ledger survives the failure — that is the point of wrapping.
    assert isinstance(excinfo.value.__cause__, (TransportError, ReviewSchemaError))
    assert len(excinfo.value.attempts) == expected_calls


def test_costs_of_failed_attempts_are_summed(repo, monkeypatch):
    """A failed attempt that spent money must reach the round total.

    Counting only the winning call under-reports the round — and the cost cap
    gates on that number.
    """
    seq = [
        TransportError("truncated", kind="max_turns", cost_usd=0.40,
                       tokens_input=800, tokens_output=100),
        _result(cost=0.25),
    ]

    def fake_invoke(prompt, **kwargs):
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(rrr, "invoke_reviewer", fake_invoke)

    outcome = rrr.run_review_round(
        repo_root=repo,
        slug=SLUG,
        version=VERSION,
        round_n=1,
        selection=TransportSelection("claude", "Claude CLI on PATH", source="claude_path"),
    )
    assert outcome.result.usage.cost_usd == 0.25  # the winning call alone
    assert outcome.total_cost_usd == pytest.approx(0.65)  # what the round cost
    assert [a.outcome for a in outcome.attempts] == ["max_turns", "success"]


def test_explicit_transport_does_not_fall_back_on_quota(repo, monkeypatch):
    """An operator who named openai asked for openai; the error is the answer."""
    monkeypatch.setattr(
        rrr, "invoke_reviewer",
        lambda p, **k: (_ for _ in ()).throw(QuotaExhaustedError("no credit")),
    )
    monkeypatch.setattr(rrr, "_is_claude_cli_available", lambda env: True)

    with pytest.raises(rrr.RoundRunError) as excinfo:
        rrr.run_review_round(
            repo_root=repo,
            slug=SLUG,
            version=VERSION,
            round_n=1,
            selection=TransportSelection("openai", "ADVERSARIAL_TRANSPORT=openai", source="explicit"),
        )


def test_quota_without_claude_cli_reraises(repo, monkeypatch):
    monkeypatch.setattr(
        rrr, "invoke_reviewer",
        lambda p, **k: (_ for _ in ()).throw(QuotaExhaustedError("no credit")),
    )
    monkeypatch.setattr(rrr, "_is_claude_cli_available", lambda env: False)

    with pytest.raises(rrr.RoundRunError) as excinfo:
        rrr.run_review_round(
            repo_root=repo,
            slug=SLUG,
            version=VERSION,
            round_n=1,
            selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
        )


def test_cwd_must_match_repo_root(repo, tmp_path, monkeypatch):
    """The implicit cwd contract is made explicit rather than silently wrong.

    `compute_round_diff` and the builder both resolve `.scratch/` and
    `plans/fixs/` from the process cwd, so a mismatch would mix repositories.
    """
    other = tmp_path.parent / "elsewhere"
    other.mkdir(exist_ok=True)
    monkeypatch.chdir(other)

    with pytest.raises(rrr.RoundRunError, match="cwd == repo_root"):
        rrr.run_review_round(
            repo_root=repo,
            slug=SLUG,
            version=VERSION,
            round_n=1,
            selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
        )


def test_prompt_paths_are_not_shared_between_runs(repo, monkeypatch):
    """Fixed /tmp/round-N-prompt.txt was cross-run shared state."""
    paths: list[Path] = []

    def fake_invoke(prompt, **kwargs):
        return _result(transport="openai", model="gpt-5.6-sol")

    monkeypatch.setattr(rrr, "invoke_reviewer", fake_invoke)
    for _ in range(2):
        outcome = rrr.run_review_round(
            repo_root=repo,
            slug=SLUG,
            version=VERSION,
            round_n=1,
            selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
            keep_prompt=True,
        )
        paths.append(outcome.prompt_debug_path)

    assert paths[0] != paths[1]


def test_four_attempt_composition_records_order_and_sums(repo, monkeypatch):
    """The composition the plan promised: api -> quota -> claude api -> success.

    Retry and fallback have to compose without losing an attempt or exceeding
    the ceiling, and every paid try must land in the total.
    """
    seq = [
        TransportError("blip", kind="api", cost_usd=0.10, tokens_input=100),
        QuotaExhaustedError("no credit"),
        TransportError("blip", kind="api", cost_usd=0.20, tokens_input=200),
        _result(cost=0.30),
    ]

    def fake_invoke(prompt, **kwargs):
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(rrr, "invoke_reviewer", fake_invoke)
    monkeypatch.setattr(rrr, "_is_claude_cli_available", lambda env: True)

    outcome = rrr.run_review_round(
        repo_root=repo, slug=SLUG, version=VERSION, round_n=1,
        selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
    )

    assert [(a.transport, a.outcome) for a in outcome.attempts] == [
        ("openai", "api"),
        ("openai", "quota"),
        ("claude", "api"),
        ("claude", "success"),
    ]
    assert outcome.total_cost_usd == pytest.approx(0.60)
    assert outcome.cost_complete is True
    assert outcome.transport == "claude"


def test_unknown_attempt_cost_marks_accounting_incomplete(repo, monkeypatch):
    """An unpriceable attempt must not look free.

    Collapsing unknown to 0.0 would quietly shrink the cost cap's denominator.
    """
    seq = [TransportError("blip", kind="api"), _result(cost=0.30)]  # cost_usd=None

    def fake_invoke(prompt, **kwargs):
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(rrr, "invoke_reviewer", fake_invoke)
    outcome = rrr.run_review_round(
        repo_root=repo, slug=SLUG, version=VERSION, round_n=1,
        selection=TransportSelection("claude", "Claude CLI on PATH", source="claude_path"),
    )
    assert outcome.attempts[0].cost_usd is None
    assert outcome.cost_complete is False
    assert outcome.total_cost_usd == pytest.approx(0.30)


def test_kept_prompt_actually_survives_the_call(repo, monkeypatch):
    """`keep_prompt=True` must hand back a file that still exists.

    `TemporaryDirectory` owns a finalizer, so merely skipping cleanup() left
    the returned path pointing at a deleted tree — and a test that only
    compared two path strings passed anyway.
    """
    monkeypatch.setattr(
        rrr, "invoke_reviewer",
        lambda p, **k: _result(transport="openai", model="gpt-5.6-sol"),
    )
    outcome = rrr.run_review_round(
        repo_root=repo, slug=SLUG, version=VERSION, round_n=1,
        selection=TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key"),
        keep_prompt=True,
    )
    assert outcome.prompt_debug_path.exists()
    assert outcome.prompt_debug_path.read_text(encoding="utf-8").strip()


def test_round_totals_reach_the_sidecar_not_just_the_winner(repo, monkeypatch):
    """Runner -> persistence: a failed paid attempt must appear in stats.cost_usd.

    Aggregating in memory is pointless if the authoritative sidecar, the resume
    total and the cost cap still read the winning call alone.
    """
    seq = [
        TransportError("truncated", kind="max_turns", cost_usd=0.40, tokens_input=800),
        _result(cost=0.25),
    ]

    def fake_invoke(prompt, **kwargs):
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(rrr, "invoke_reviewer", fake_invoke)
    outcome = rrr.run_review_round(
        repo_root=repo, slug=SLUG, version=VERSION, round_n=1,
        selection=TransportSelection("claude", "Claude CLI on PATH", source="claude_path"),
    )

    state = RoundState(
        round_n=1, slug=SLUG, version=VERSION,
        transport=outcome.transport, model=outcome.result.model,
        started_at="2026-08-16T00:00:00Z", completed_at="2026-08-16T00:01:00Z",
        reviewer_response=outcome.result,
        round_usage=ReviewUsage(
            outcome.tokens_input, outcome.tokens_output, outcome.total_cost_usd
        ),
        plan_content_at_end=PLAN_R2, baseline_plan_content=PLAN_R1,
        cumulative_cost_usd=outcome.total_cost_usd,
    )
    sidecar = build_sidecar(state, raw_response_text="{}")

    assert sidecar["stats"]["cost_usd"] == pytest.approx(0.65)
    assert sidecar["stats"]["cost_usd"] != outcome.result.usage.cost_usd
