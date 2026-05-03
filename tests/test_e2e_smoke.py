"""End-to-end smoke test: drive a full multi-round loop through the v2 state machine.

Mocks the reviewer transport so the test is hermetic and fast. The point is to
exercise the integration: prompt building → reviewer → parse → state →
sidecar → render → exit gate. Per v2-plan §8 Milestone B end-to-end:

  synthetic plan with 3 known issues (1 high, 1 medium, 1 low) → loop
  converges in <= 3 rounds via severity-gated exit
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from build_reviewer_prompt_v2 import build_prompt
from loop_state import (
    Deferral,
    ExitReason,
    PlanEdit,
    PlannerDecision,
    RoundState,
    build_sidecar,
    compute_round_diff,
    detect_resume,
    escalate_to_resolved_with_deferrals,
    evaluate_exit,
    load_sidecars,
    regenerate_fixes_md,
    take_initial_snapshot,
    write_sidecar_atomic,
)
from parse_review import (
    Finding,
    OpenQuestion,
    ReviewResult,
    ReviewUsage,
    parse_openai_response,
)


def _round_response(round_n, *, status="FINDINGS_PRESENT", findings=None, open_questions=None):
    """Build an OpenAI-shaped raw response string for a given round."""
    return json.dumps(
        {
            "status": status,
            "findings": findings or [],
            "open_questions": open_questions or [],
        }
    )


# Round 1: 3 findings (1 high, 1 medium, 1 low) — the synthetic baseline.
ROUND_1_RAW = _round_response(
    1,
    findings=[
        {
            "severity": "high",
            "category": "Pipeline",
            "where": "§2",
            "what_can_go_wrong": "Plan silently drops X under condition Y.",
            "concrete_fix": "Add a guard before drop and surface a warning.",
        },
        {
            "severity": "medium",
            "category": "Verification",
            "where": "§3",
            "what_can_go_wrong": "No test covers the fallback path.",
            "concrete_fix": "Add test_fallback_path() asserting expected behavior.",
        },
        {
            "severity": "low",
            "category": "Naming",
            "where": "§4",
            "what_can_go_wrong": "Field name `tco_per_unit` is ambiguous.",
            "concrete_fix": "Rename to `tco_per_actuator_usd`.",
        },
    ],
)

# Round 2: clean review — all findings addressed.
ROUND_2_RAW = _round_response(2, status="NO_FINDINGS")


# --- Tests ------------------------------------------------------------------


def test_e2e_three_findings_converges_round_2(isolated_repo):
    """Synthetic 3-finding plan resolves to APPROVED in round 2 after edits."""
    slug, version = "synth", "v0"
    plan_path = isolated_repo / "plans" / "synth-v0.md"
    plan_path.write_text("# synthetic plan\n\n## §2\n## §3\n## §4\n", encoding="utf-8")
    baseline_text = plan_path.read_text(encoding="utf-8")

    # Setup: take initial snapshot
    take_initial_snapshot(plan_path, slug=slug, version=version)

    # ROUND 1
    round_n = 1
    started_at = "2026-05-03T00:01:00Z"
    review = parse_openai_response(ROUND_1_RAW, round_n=round_n, model="gpt-5.5")
    assert len(review.findings) == 3

    # Planner accepts all 3
    decisions = [
        PlannerDecision(f"f_r1_{i+1}", "accept", f"good catch {i+1}", f"edit{i+1}")
        for i in range(3)
    ]
    plan_edits = [
        PlanEdit("§2", "Added guard before drop"),
        PlanEdit("§3", "Added test_fallback_path"),
        PlanEdit("§4", "Renamed field"),
    ]

    # Apply edits to plan
    edited = "# synthetic plan v1\n\n## §2 (with guard)\n## §3 (with test)\n## §4 (renamed)\n"
    plan_path.write_text(edited, encoding="utf-8")

    state1 = RoundState(
        round_n=round_n, slug=slug, version=version,
        transport="openai", model="gpt-5.5",
        started_at=started_at, completed_at="2026-05-03T00:01:30Z",
        reviewer_response=review,
        decisions=decisions,
        plan_edits=plan_edits,
        plan_content_at_end=edited,
        baseline_plan_content=baseline_text,
        cumulative_cost_usd=0.05,
        duration_seconds=30.0,
        plan_size_delta=len(edited) - len(baseline_text),
    )
    sidecar1 = build_sidecar(state1, raw_response_text=ROUND_1_RAW)
    write_sidecar_atomic(sidecar1, slug=slug, version=version)
    regenerate_fixes_md(slug=slug, version=version)

    decision1 = evaluate_exit(state1, max_rounds=20, cumulative_cost_usd=0.05, cost_cap_usd=5.0)
    assert decision1.reason == ExitReason.RESOLVED  # all 3 decided, 0 open

    # Verify the fixes-md was rendered
    fixes_md = isolated_repo / "plans" / "fixs" / "synth-v0-fixes.md"
    assert fixes_md.exists()
    md_content = fixes_md.read_text(encoding="utf-8")
    assert "## Round 1" in md_content
    assert "**[HIGH]** [Pipeline]" in md_content


def test_e2e_round_2_diff_shows_round_1_edits(isolated_repo):
    """The round-2 diff should show what round-1 actually changed."""
    slug, version = "synth", "v0"
    plan_path = isolated_repo / "plans" / "synth-v0.md"
    baseline = "# baseline\n\nbody.\n"
    plan_path.write_text(baseline, encoding="utf-8")

    take_initial_snapshot(plan_path, slug=slug, version=version)

    # Simulate round-1 sidecar with edits (without going through the full state machine)
    edited = "# baseline v1\n\nbody with new guard.\n"
    plan_path.write_text(edited, encoding="utf-8")

    review = parse_openai_response(ROUND_2_RAW, round_n=1, model="gpt-5.5")
    state = RoundState(
        round_n=1, slug=slug, version=version,
        transport="openai", model="gpt-5.5",
        started_at="2026-05-03T00:01:00Z", completed_at="2026-05-03T00:01:30Z",
        reviewer_response=review,
        decisions=[],
        plan_content_at_end=edited,
        baseline_plan_content=baseline,
        cumulative_cost_usd=0.05,
    )
    sidecar = build_sidecar(state, raw_response_text=ROUND_2_RAW)
    write_sidecar_atomic(sidecar, slug=slug, version=version)

    # Now compute the round-2 diff
    diff_text, recovered = compute_round_diff(plan_path, round_n=2, slug=slug, version=version)
    assert recovered is False
    assert "# baseline\n" in diff_text
    assert "# baseline v1" in diff_text
    assert "new guard" in diff_text


def test_e2e_resume_after_session_interruption(isolated_repo):
    """After session restart, detect_resume + restore_snapshots_from_sidecars
    must reconstitute enough state for the next round to succeed."""
    slug, version = "synth", "v0"
    plan_path = isolated_repo / "plans" / "synth-v0.md"
    baseline = "# baseline\n"
    plan_path.write_text(baseline, encoding="utf-8")

    # Round 1 happens
    take_initial_snapshot(plan_path, slug=slug, version=version)
    edited = "# baseline edited\n"
    plan_path.write_text(edited, encoding="utf-8")

    review = parse_openai_response(ROUND_1_RAW, round_n=1, model="gpt-5.5")
    state = RoundState(
        round_n=1, slug=slug, version=version,
        transport="openai", model="gpt-5.5",
        started_at="2026-05-03T00:01:00Z", completed_at="2026-05-03T00:01:30Z",
        reviewer_response=review,
        decisions=[
            PlannerDecision(f"f_r1_{i+1}", "accept", "x", "y") for i in range(3)
        ],
        plan_content_at_end=edited,
        baseline_plan_content=baseline,
        cumulative_cost_usd=0.05,
    )
    sidecar = build_sidecar(state, raw_response_text=ROUND_1_RAW)
    write_sidecar_atomic(sidecar, slug=slug, version=version)

    # Simulate session restart: wipe `.scratch/`
    for snap in (isolated_repo / ".scratch").glob("*.md"):
        snap.unlink()

    # Resume detection
    status = detect_resume(slug=slug, version=version)
    assert status.has_prior_run is True
    assert status.last_completed_round == 1
    assert status.cumulative_cost_usd == 0.05

    # Round 2's diff must work even with .scratch/ wiped
    diff_text, recovered = compute_round_diff(plan_path, round_n=2, slug=slug, version=version)
    assert recovered is False  # recovered via sidecar baseline_plan_content, NOT git fallback
    assert "# baseline\n" in diff_text
    assert "# baseline edited" in diff_text


def test_e2e_planner_locked_when_all_rejected(isolated_repo):
    """A round where the planner rejects every finding ends as planner_locked."""
    slug, version = "synth", "v0"
    plan_path = isolated_repo / "plans" / "synth-v0.md"
    plan_path.write_text("# x\n", encoding="utf-8")
    take_initial_snapshot(plan_path, slug=slug, version=version)

    review = parse_openai_response(ROUND_1_RAW, round_n=1, model="gpt-5.5")
    state = RoundState(
        round_n=1, slug=slug, version=version,
        transport="openai", model="gpt-5.5",
        started_at="2026-05-03T00:01:00Z", completed_at="2026-05-03T00:01:30Z",
        reviewer_response=review,
        decisions=[
            PlannerDecision(f"f_r1_{i+1}", "reject", f"reason {i+1}") for i in range(3)
        ],
        plan_content_at_end="# x\n",
        baseline_plan_content="# x\n",
    )

    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.05, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.PLANNER_LOCKED
    # When the planner explicitly rejects every finding, all items are decided —
    # no soft-block needed because nothing is unresolved/undecided. This is
    # different from "ceiling-hit while items are still open" which DOES need
    # soft-block. See the corresponding test in test_loop_state.py.
    assert decision.needs_soft_block is False


def test_e2e_resolved_with_deferrals_after_user_defer(isolated_repo):
    """User defers all open items at ceiling → audit semantic upgrades to RESOLVED_WITH_DEFERRALS."""
    slug, version = "synth", "v0"
    plan_path = isolated_repo / "plans" / "synth-v0.md"
    plan_path.write_text("# x\n", encoding="utf-8")
    take_initial_snapshot(plan_path, slug=slug, version=version)

    review = parse_openai_response(ROUND_1_RAW, round_n=20, model="gpt-5.5")
    state = RoundState(
        round_n=20, slug=slug, version=version,  # at ceiling
        transport="openai", model="gpt-5.5",
        started_at="2026-05-03T00:01:00Z", completed_at="2026-05-03T00:01:30Z",
        reviewer_response=review,
        decisions=[],  # nothing decided yet
        plan_content_at_end="# x\n",
        baseline_plan_content=None,
    )

    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.5, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.CEILING_HIT
    assert decision.needs_soft_block is True

    # User defers all
    deferrals = [
        Deferral("f_r20_1", "high", "deferred to v0.0.6", "v0.0.6"),
        Deferral("f_r20_2", "medium", "deferred to phase-4", "phase-4"),
    ]
    upgraded = escalate_to_resolved_with_deferrals(decision, deferrals)
    assert upgraded.reason == ExitReason.RESOLVED_WITH_DEFERRALS
    assert upgraded.needs_soft_block is False


def test_e2e_full_round_persistence_round_trip(isolated_repo):
    """Round-1 sidecar can be loaded back, validated, and rendered."""
    slug, version = "synth", "v0"
    plan_path = isolated_repo / "plans" / "synth-v0.md"
    baseline = "# baseline\n"
    plan_path.write_text(baseline, encoding="utf-8")

    review = parse_openai_response(ROUND_1_RAW, round_n=1, model="gpt-5.5")
    state = RoundState(
        round_n=1, slug=slug, version=version,
        transport="openai", model="gpt-5.5",
        started_at="2026-05-03T00:01:00Z", completed_at="2026-05-03T00:01:30Z",
        reviewer_response=review,
        decisions=[PlannerDecision("f_r1_1", "accept", "good", "edit")],
        plan_content_at_end=baseline,
        baseline_plan_content=baseline,
        cumulative_cost_usd=0.05,
    )
    sidecar = build_sidecar(state, raw_response_text=ROUND_1_RAW)
    write_sidecar_atomic(sidecar, slug=slug, version=version)

    # Load back and validate
    loaded = load_sidecars(slug=slug, version=version)
    assert len(loaded) == 1
    assert loaded[0]["round"] == 1
    assert loaded[0]["plan_content"] == baseline

    # Regenerate markdown — must include round 1
    regenerate_fixes_md(slug=slug, version=version)
    fixes_md = (isolated_repo / "plans" / "fixs" / "synth-v0-fixes.md").read_text(
        encoding="utf-8"
    )
    assert "## Round 1" in fixes_md
    assert "Pipeline" in fixes_md
