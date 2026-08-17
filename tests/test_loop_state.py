"""Tests for scripts/loop_state.py — the v2 state machine."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from loop_state import (
    DEFAULT_MAX_ROUNDS,
    Deferral,
    ExitDecision,
    ExitReason,
    PlanEdit,
    PlannerDecision,
    ResumeIntegrityError,
    RoundState,
    SCHEMA_VERSION,
    SidecarSchemaError,
    StartOverPlan,
    _all_rejected,
    _open_items,
    build_sidecar,
    cleanup_snapshots,
    compute_round_diff,
    detect_resume,
    escalate_to_resolved_with_deferrals,
    evaluate_bloat,
    evaluate_exit,
    execute_start_over,
    load_sidecars,
    plan_start_over,
    regenerate_fixes_md,
    restore_snapshots_from_sidecars,
    take_initial_snapshot,
    validate_sidecar,
    write_sidecar_atomic,
)
from parse_review import (
    Finding,
    OpenQuestion,
    ReviewResult,
    ReviewUsage,
)


# --- Helpers ----------------------------------------------------------------


def _review(status="FINDINGS_PRESENT", findings=None, open_questions=None,
            tokens_input=1000, tokens_output=500, cost_usd=0.05):
    return ReviewResult(
        status=status,
        findings=findings or [],
        open_questions=open_questions or [],
        raw_response_text="{}",
        transport="openai",
        model="gpt-5.5",
        usage=ReviewUsage(tokens_input, tokens_output, cost_usd),
    )


def _state(round_n=1, review=None, decisions=None, plan_content="# plan", baseline=None):
    return RoundState(
        round_n=round_n,
        slug="test",
        version="v0",
        transport="openai",
        model="gpt-5.5",
        started_at=f"2026-05-03T00:0{round_n}:00Z",
        completed_at=f"2026-05-03T00:0{round_n}:30Z",
        reviewer_response=review or _review(status="NO_FINDINGS"),
        decisions=decisions or [],
        plan_content_at_end=plan_content,
        baseline_plan_content=baseline,
        cumulative_cost_usd=0.05,
        duration_seconds=30.0,
    )


# --- evaluate_exit priority -------------------------------------------------


def test_exit_approved_for_no_findings():
    state = _state(review=_review(status="NO_FINDINGS"))
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.0, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.APPROVED
    assert decision.needs_soft_block is False


def test_exit_planner_locked_when_all_rejected_takes_priority_over_resolved():
    """C1: planner-locked must check BEFORE resolved, since rejections count as decided."""
    finding = Finding("medium", "X", "Y", "Z", "W")
    state = _state(
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
        decisions=[PlannerDecision("f_r1_1", "reject", "out of scope")],
    )
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.0, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.PLANNER_LOCKED


def test_exit_planner_locked_only_with_via_user_rejects():
    """User-supplied rejects also count for planner-lock."""
    finding = Finding("high", "X", "Y", "Z", "W")
    state = _state(
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
        decisions=[PlannerDecision("f_r1_1", "reject_via_user", "user said no")],
    )
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.0, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.PLANNER_LOCKED


def test_exit_no_exit_when_accepts_present_even_with_no_opens():
    """An accepted finding produces plan edits that need round N+1 to validate.

    Previously this returned RESOLVED, which exited before any reviewer
    saw the edited plan — a silent convergence bug that let the loop stop
    after one round even when the planner made substantive changes the
    reviewer never re-checked.
    """
    finding = Finding("medium", "X", "Y", "Z", "W")
    state = _state(
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
        decisions=[PlannerDecision("f_r1_1", "accept", "good catch", "edit X")],
    )
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.0, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.NO_EXIT
    assert decision.needs_soft_block is False


def test_exit_no_exit_when_accept_via_user_present():
    """`accept_via_user` also implies edits — same guard as `accept`."""
    finding = Finding("high", "X", "Y", "Z", "W")
    state = _state(
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
        decisions=[PlannerDecision("f_r1_1", "accept_via_user", "user said yes", "edit Y")],
    )
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.0, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.NO_EXIT


def test_exit_cost_capped_when_cost_exceeds_cap():
    finding = Finding("high", "X", "Y", "Z", "W")
    state = _state(review=_review(status="FINDINGS_PRESENT", findings=[finding]))
    # No decisions → finding is open → cost cap fires soft-block
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=10.0, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.COST_CAPPED
    assert decision.needs_soft_block is True


def test_exit_ceiling_hit():
    finding = Finding("medium", "X", "Y", "Z", "W")
    state = _state(round_n=20, review=_review(status="FINDINGS_PRESENT", findings=[finding]))
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.5, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.CEILING_HIT
    assert decision.needs_soft_block is True


def test_default_max_rounds_is_five():
    """The documented ceiling lives here, not in SKILL.md prose.

    v1 let the default drift: the Termination section still read "Round 10"
    long after the loop had moved to 20, because the number was only ever
    written in markdown. Pin it in code so the docs can cite it instead.
    """
    assert DEFAULT_MAX_ROUNDS == 5


def test_exit_ceiling_hit_uses_default_max_rounds():
    """Omitting `max_rounds` applies DEFAULT_MAX_ROUNDS rather than erroring."""
    finding = Finding("medium", "X", "Y", "Z", "W")
    state = _state(
        round_n=DEFAULT_MAX_ROUNDS,
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
    )
    decision = evaluate_exit(state, cumulative_cost_usd=0.5, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.CEILING_HIT
    assert decision.needs_soft_block is True


def test_exit_below_default_ceiling_keeps_looping():
    """The round before the default ceiling must not exit — guards an off-by-one."""
    finding = Finding("medium", "X", "Y", "Z", "W")
    state = _state(
        round_n=DEFAULT_MAX_ROUNDS - 1,
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
        decisions=[],
    )
    decision = evaluate_exit(state, cumulative_cost_usd=0.5, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.NO_EXIT


def _all_accepted_state(round_n):
    """A round whose every finding was accepted — i.e. the plan was edited."""
    findings = [
        Finding("high", "cat", "breaks X", "fix X", "ev"),
        Finding("medium", "cat", "breaks Y", "fix Y", "ev"),
    ]
    review = _review(status="FINDINGS_PRESENT", findings=findings)
    decisions = [
        PlannerDecision(f"f_r{round_n}_{i}", "accept", "good catch", "edited")
        for i in range(1, len(findings) + 1)
    ]
    return _state(round_n=round_n, review=review, decisions=decisions)


def test_ceiling_with_all_accepted_soft_blocks():
    """A ceiling exit must not silently end the loop on an unvalidated plan.

    Accepting every finding leaves zero OPEN items, so gating the soft-block
    on `has_open` alone exited without any reviewer reading the edits the
    accepts produced — the exact case the RESOLVED branch's accept guard
    exists to prevent. Rare at a ceiling of 20, ordinary at 5.
    """
    decision = evaluate_exit(
        _all_accepted_state(DEFAULT_MAX_ROUNDS), cumulative_cost_usd=0.5, cost_cap_usd=5.0
    )
    assert decision.reason == ExitReason.CEILING_HIT
    assert decision.needs_soft_block is True
    assert not decision.open_highs and not decision.open_mediums
    assert decision.unvalidated_accepts == [
        (f"f_r{DEFAULT_MAX_ROUNDS}_1", "high"),
        (f"f_r{DEFAULT_MAX_ROUNDS}_2", "medium"),
    ]


def test_cost_cap_with_all_accepted_soft_blocks():
    """The cost cap has the identical hole and is reachable well before the ceiling."""
    decision = evaluate_exit(
        _all_accepted_state(2), cumulative_cost_usd=6.0, cost_cap_usd=5.0
    )
    assert decision.reason == ExitReason.COST_CAPPED
    assert decision.needs_soft_block is True
    assert [sev for _, sev in decision.unvalidated_accepts] == ["high", "medium"]


def test_planner_locked_all_rejected_needs_no_soft_block():
    """All-rejected means no plan edits, so there is nothing to validate."""
    findings = [Finding("medium", "cat", "breaks Y", "fix Y", "ev")]
    review = _review(status="FINDINGS_PRESENT", findings=findings)
    state = _state(
        round_n=DEFAULT_MAX_ROUNDS,
        review=review,
        decisions=[PlannerDecision(f"f_r{DEFAULT_MAX_ROUNDS}_1", "reject", "out of scope")],
    )
    decision = evaluate_exit(state, cumulative_cost_usd=0.5, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.PLANNER_LOCKED
    assert decision.needs_soft_block is False
    assert decision.unvalidated_accepts == []


def test_decision_naming_unknown_item_id_is_rejected():
    """A decision on a nonexistent item would fire the gate with nothing to show.

    `needs_soft_block` is derived from the enumerated accepts, so a stale or
    typoed `item_id` must not be able to reach `evaluate_exit` — otherwise the
    soft-block prompt appears with zero items to display or persist.
    """
    review = _review(status="FINDINGS_PRESENT",
                     findings=[Finding("high", "c", "X", "fix", "ev")])
    state = _state(round_n=3, review=review,
                   decisions=[PlannerDecision("f_r1_1", "accept", "stale id")])
    with pytest.raises(ValueError, match="unknown item_id"):
        evaluate_exit(state, cumulative_cost_usd=0.5, cost_cap_usd=5.0)


def test_duplicate_decision_for_one_item_is_rejected():
    review = _review(status="FINDINGS_PRESENT",
                     findings=[Finding("high", "c", "X", "fix", "ev")])
    state = _state(
        round_n=3,
        review=review,
        decisions=[
            PlannerDecision("f_r3_1", "accept", "first"),
            PlannerDecision("f_r3_1", "reject", "second"),
        ],
    )
    with pytest.raises(ValueError, match="duplicate decision"):
        evaluate_exit(state, cumulative_cost_usd=0.5, cost_cap_usd=5.0)


def test_malformed_decision_is_not_buildable_into_a_sidecar():
    """Validation must precede persistence, not follow it.

    `evaluate_exit` also checks, but it runs at step 7 — by then step 6 has
    written and rendered the sidecar, so a malformed decision would already be
    baked into the authoritative audit record when the exception fired.
    """
    review = _review(status="FINDINGS_PRESENT",
                     findings=[Finding("high", "c", "X", "fix", "ev")])
    state = _state(round_n=3, review=review,
                   decisions=[PlannerDecision("f_r1_1", "accept", "stale id")])
    with pytest.raises(ValueError, match="unknown item_id"):
        build_sidecar(state, raw_response_text="{}")


def test_resume_rejects_a_sidecar_with_dangling_decision_ids():
    """A sidecar written before this invariant existed must not be trusted.

    JSON Schema can express shape but not "this item_id names a finding in
    this same document", so without the semantic check a malformed historical
    sidecar passes resume validation and is believed.
    """
    review = _review(status="FINDINGS_PRESENT",
                     findings=[Finding("high", "c", "X", "fix", "ev")])
    good = _state(round_n=1, review=review, baseline="# plan",
                  decisions=[PlannerDecision("f_r1_1", "accept", "ok")])
    sidecar = build_sidecar(good, raw_response_text="{}")
    validate_sidecar(sidecar)  # precondition: the well-formed one passes

    sidecar["planner_decisions"][0]["item_id"] = "f_r9_7"
    with pytest.raises(SidecarSchemaError, match="unknown item_id"):
        validate_sidecar(sidecar)


def test_escalation_preserves_unvalidated_accepts():
    """`unvalidated_accepts` has a default, so omitting it here would silently
    empty it and the end report would lose the skipped-validation items this
    escalation exists to record."""
    decision = evaluate_exit(
        _all_accepted_state(DEFAULT_MAX_ROUNDS), cumulative_cost_usd=0.5, cost_cap_usd=5.0
    )
    assert decision.unvalidated_accepts  # precondition
    escalated = escalate_to_resolved_with_deferrals(
        decision,
        [Deferral(fid, sev, "validation skipped at exit", "accepted-at-exit")
         for fid, sev in decision.unvalidated_accepts],
    )
    assert escalated.reason == ExitReason.RESOLVED_WITH_DEFERRALS
    assert escalated.unvalidated_accepts == decision.unvalidated_accepts


def test_exit_no_exit_sentinel_when_continuing():
    """C2: NO_EXIT is the explicit "continue to N+1" signal."""
    finding = Finding("medium", "X", "Y", "Z", "W")
    state = _state(
        round_n=3,
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
        decisions=[],  # not decided yet
    )
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.5, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.NO_EXIT
    assert decision.needs_soft_block is False
    assert decision.open_mediums == ["f_r3_1"]


def test_exit_priority_order():
    """When multiple conditions could fire, the priority order matters.

    NO_FINDINGS > planner_locked > resolved > cost_cap > ceiling > no_exit.
    Test: at ceiling AND cost-capped AND all-rejected, planner_locked wins.
    """
    finding = Finding("high", "X", "Y", "Z", "W")
    state = _state(
        round_n=20,
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
        # Must name THIS round's item: the fixture previously said "f_r1_1"
        # against round 20, which `_validate_decision_ids` now rejects.
        decisions=[PlannerDecision("f_r20_1", "reject", "no")],
    )
    decision = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=10.0, cost_cap_usd=5.0)
    assert decision.reason == ExitReason.PLANNER_LOCKED


# --- escalate_to_resolved_with_deferrals -----------------------------------


def test_escalate_promotes_ceiling_to_resolved_with_deferrals():
    """C3: after the user defers via soft-block, the audit semantic upgrades."""
    finding = Finding("medium", "X", "Y", "Z", "W")
    state = _state(
        round_n=20,
        review=_review(status="FINDINGS_PRESENT", findings=[finding]),
    )
    original = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.5, cost_cap_usd=5.0)
    assert original.reason == ExitReason.CEILING_HIT

    deferrals = [Deferral("f_r20_1", "medium", "deferred to v2.1", "v2.1")]
    upgraded = escalate_to_resolved_with_deferrals(original, deferrals)
    assert upgraded.reason == ExitReason.RESOLVED_WITH_DEFERRALS
    assert upgraded.needs_soft_block is False
    # round_n=20 so the finding ID is f_r20_1, not f_r1_1
    assert upgraded.open_mediums == ["f_r20_1"]


def test_escalate_no_op_with_empty_deferrals():
    """If no deferrals collected, the original decision stands."""
    finding = Finding("medium", "X", "Y", "Z", "W")
    state = _state(round_n=20, review=_review(status="FINDINGS_PRESENT", findings=[finding]))
    original = evaluate_exit(state, max_rounds=20, cumulative_cost_usd=0.5, cost_cap_usd=5.0)
    same = escalate_to_resolved_with_deferrals(original, [])
    assert same is original


# --- _open_items -------------------------------------------------------------


def test_open_items_separates_by_severity():
    findings = [
        Finding("high", "A", "B", "C", "D"),
        Finding("medium", "E", "F", "G", "H"),
        Finding("low", "I", "J", "K", "L"),
    ]
    state = _state(review=_review(status="FINDINGS_PRESENT", findings=findings))
    highs, meds, oqs = _open_items(state)
    assert highs == ["f_r1_1"]
    assert meds == ["f_r1_2"]
    assert oqs == []
    # IM3 (review): explicit assertion that the low finding is NOT in any
    # gate list. Previously this was tested only by omission — a refactor
    # that incorrectly included lows in `meds` would have passed.
    low_id = "f_r1_3"
    assert low_id not in highs
    assert low_id not in meds
    assert low_id not in oqs


def test_open_items_excludes_decided():
    findings = [Finding("high", "A", "B", "C", "D")]
    state = _state(
        review=_review(status="FINDINGS_PRESENT", findings=findings),
        decisions=[PlannerDecision("f_r1_1", "accept", "yes", "edit")],
    )
    highs, meds, oqs = _open_items(state)
    assert highs == []


def test_open_items_includes_undecided_open_questions():
    state = _state(
        review=_review(
            status="FINDINGS_PRESENT",
            open_questions=[OpenQuestion("oq_r1_1", "Q?")],
        ),
    )
    highs, meds, oqs = _open_items(state)
    assert oqs == ["oq_r1_1"]


def test_all_rejected_helper():
    assert _all_rejected([
        PlannerDecision("a", "reject", "x"),
        PlannerDecision("b", "reject_via_user", "y"),
    ]) is True

    assert _all_rejected([
        PlannerDecision("a", "reject", "x"),
        PlannerDecision("b", "accept", "y", "z"),
    ]) is False

    assert _all_rejected([]) is False  # empty → not "all rejected"


# --- evaluate_bloat ---------------------------------------------------------


def test_bloat_triggers_on_growth_with_no_new_highs(make_sidecar_factory):
    sidecars = [
        make_sidecar_factory(round_n=n, plan_content="x" * 1000, findings=[])
        for n in range(1, 4)
    ]
    verdict = evaluate_bloat(
        sidecars=sidecars,
        current_plan_size_chars=1300,  # +30%
        threshold=0.20,
        window=3,
    )
    assert verdict.triggered is True
    assert verdict.growth_fraction == pytest.approx(0.30)
    assert verdict.new_high_findings == 0


def test_bloat_does_not_trigger_with_new_high_finding(make_sidecar_factory):
    sidecars = [
        make_sidecar_factory(
            round_n=1, plan_content="x" * 1000,
            findings=[{"id": "f_r1_1", "severity": "high", "category": "X", "where": "Y", "what_can_go_wrong": "Z", "concrete_fix": "W"}],
        ),
        make_sidecar_factory(round_n=2, plan_content="x" * 1000, findings=[]),
        make_sidecar_factory(round_n=3, plan_content="x" * 1000, findings=[]),
    ]
    verdict = evaluate_bloat(
        sidecars=sidecars,
        current_plan_size_chars=1300,
        threshold=0.20,
        window=3,
    )
    assert verdict.triggered is False
    assert verdict.new_high_findings == 1


def test_bloat_does_not_trigger_below_threshold(make_sidecar_factory):
    sidecars = [
        make_sidecar_factory(round_n=n, plan_content="x" * 1000, findings=[])
        for n in range(1, 4)
    ]
    verdict = evaluate_bloat(
        sidecars=sidecars,
        current_plan_size_chars=1100,  # +10%, below 0.20 threshold
        threshold=0.20,
        window=3,
    )
    assert verdict.triggered is False


def test_bloat_skips_when_too_few_rounds(make_sidecar_factory):
    sidecars = [make_sidecar_factory(round_n=1, plan_content="x" * 1000)]
    verdict = evaluate_bloat(
        sidecars=sidecars, current_plan_size_chars=10000,
        threshold=0.20, window=3,
    )
    assert verdict.triggered is False


# --- Snapshot machinery -----------------------------------------------------


def test_take_initial_snapshot_writes_r1(isolated_repo):
    plan = isolated_repo / "plans" / "test-v0.md"
    plan.write_text("# baseline plan", encoding="utf-8")
    take_initial_snapshot(plan, slug="test", version="v0")
    snapshot = isolated_repo / ".scratch" / "v0-test-plan-snapshot-r1.md"
    assert snapshot.exists()
    assert snapshot.read_text(encoding="utf-8") == "# baseline plan"


def test_take_initial_snapshot_namespaced_does_not_collide(isolated_repo):
    """Two different (slug, version) pairs produce distinct snapshots."""
    plan_a = isolated_repo / "plans" / "a-v0.md"
    plan_b = isolated_repo / "plans" / "b-v0.md"
    plan_a.write_text("# A", encoding="utf-8")
    plan_b.write_text("# B", encoding="utf-8")
    take_initial_snapshot(plan_a, slug="a", version="v0")
    take_initial_snapshot(plan_b, slug="b", version="v0")
    assert (isolated_repo / ".scratch" / "v0-a-plan-snapshot-r1.md").read_text() == "# A"
    assert (isolated_repo / ".scratch" / "v0-b-plan-snapshot-r1.md").read_text() == "# B"


def test_compute_round_diff_uses_snapshot_when_hash_matches(isolated_repo):
    """Happy path: snapshot exists, hash matches sidecar → use it."""
    plan = isolated_repo / "plans" / "test-v0.md"
    baseline = "# old\n"
    plan.write_text(baseline, encoding="utf-8")
    take_initial_snapshot(plan, slug="test", version="v0")

    # Write round-1 sidecar with baseline hash
    baseline_sha = hashlib.sha256(baseline.encode("utf-8")).hexdigest()
    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "round": 1,
        "started_at": "2026-05-03T00:00:00Z",
        "completed_at": "2026-05-03T00:00:30Z",
        "transport": "openai",
        "model": "gpt-5.5",
        "raw_response_text": "{}",
        "plan_content_sha256": baseline_sha,
        "plan_content": baseline,
        "baseline_plan_content_sha256": baseline_sha,
        "baseline_plan_content": baseline,
        "restart_metadata": None,
        "deferrals_at_exit": None,
        "reviewer_response": {"status": "NO_FINDINGS", "findings": [], "open_questions": []},
        "planner_decisions": [],
        "plan_edits_applied": [],
        "stats": {
            "tokens_input": 0, "tokens_output": 0, "cost_usd": 0.0,
            "cumulative_cost_usd": 0.0, "duration_seconds": 0.0,
            "plan_size_chars": len(baseline), "plan_size_delta": 0,
            "severity_histogram": {"high": 0, "medium": 0, "low": 0},
        },
    }
    (isolated_repo / "plans" / "fixs" / "v0-test-round-1.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )

    # Now edit the plan and compute round-2 diff
    plan.write_text("# new content\n", encoding="utf-8")
    diff_text, recovered = compute_round_diff(plan, round_n=2, slug="test", version="v0")
    assert recovered is False
    assert "# old" in diff_text
    assert "# new content" in diff_text


def test_compute_round_diff_recovers_from_sidecar_when_snapshot_missing(isolated_repo):
    """Code-review C1 from earlier dogfood: sidecar recovery before git fallback."""
    plan = isolated_repo / "plans" / "test-v0.md"
    baseline = "# baseline\n"
    plan.write_text(baseline, encoding="utf-8")

    baseline_sha = hashlib.sha256(baseline.encode("utf-8")).hexdigest()
    sidecar = {
        "schema_version": SCHEMA_VERSION, "round": 1,
        "started_at": "2026-05-03T00:00:00Z",
        "completed_at": "2026-05-03T00:00:30Z",
        "transport": "openai", "model": "gpt-5.5",
        "raw_response_text": "{}",
        "plan_content_sha256": baseline_sha, "plan_content": baseline,
        "baseline_plan_content_sha256": baseline_sha,
        "baseline_plan_content": baseline,
        "restart_metadata": None, "deferrals_at_exit": None,
        "reviewer_response": {"status": "NO_FINDINGS", "findings": [], "open_questions": []},
        "planner_decisions": [], "plan_edits_applied": [],
        "stats": {
            "tokens_input": 0, "tokens_output": 0, "cost_usd": 0.0,
            "cumulative_cost_usd": 0.0, "duration_seconds": 0.0,
            "plan_size_chars": len(baseline), "plan_size_delta": 0,
            "severity_histogram": {"high": 0, "medium": 0, "low": 0},
        },
    }
    (isolated_repo / "plans" / "fixs" / "v0-test-round-1.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )

    # No initial snapshot taken — round-2 should still succeed via sidecar recovery
    plan.write_text("# changed\n", encoding="utf-8")
    diff_text, recovered = compute_round_diff(plan, round_n=2, slug="test", version="v0")
    assert recovered is False  # recovered via sidecar, not git
    assert "# baseline" in diff_text
    assert "# changed" in diff_text


def test_compute_round_diff_branch3_recovers_when_snapshot_mismatched(isolated_repo):
    """IM1 (review): snapshot present but hash-mismatched → sidecar recovery
    succeeds → snapshot file rewritten from sidecar → recovered=False (still
    snapshot-accurate, just regenerated). Branch 3 of the §5.3.1 priority chain.
    """
    plan = isolated_repo / "plans" / "test-v0.md"
    baseline = "# real baseline\n"
    plan.write_text(baseline, encoding="utf-8")

    # Write a CORRUPT snapshot (wrong content)
    take_initial_snapshot(plan, slug="test", version="v0")
    snapshot_path = isolated_repo / ".scratch" / "v0-test-plan-snapshot-r1.md"
    snapshot_path.write_text("# corrupted snapshot — should be ignored\n", encoding="utf-8")

    # And a CORRECT round-1 sidecar pointing at the real baseline
    baseline_sha = hashlib.sha256(baseline.encode("utf-8")).hexdigest()
    sidecar = {
        "schema_version": SCHEMA_VERSION, "round": 1,
        "started_at": "2026-05-03T00:00:00Z", "completed_at": "2026-05-03T00:00:30Z",
        "transport": "openai", "model": "gpt-5.5",
        "raw_response_text": "{}",
        "plan_content_sha256": baseline_sha, "plan_content": baseline,
        "baseline_plan_content_sha256": baseline_sha, "baseline_plan_content": baseline,
        "restart_metadata": None, "deferrals_at_exit": None,
        "reviewer_response": {"status": "NO_FINDINGS", "findings": [], "open_questions": []},
        "planner_decisions": [], "plan_edits_applied": [],
        "stats": {
            "tokens_input": 0, "tokens_output": 0, "cost_usd": 0.0,
            "cumulative_cost_usd": 0.0, "duration_seconds": 0.0,
            "plan_size_chars": len(baseline), "plan_size_delta": 0,
            "severity_histogram": {"high": 0, "medium": 0, "low": 0},
        },
    }
    (isolated_repo / "plans" / "fixs" / "v0-test-round-1.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )

    plan.write_text("# new content\n", encoding="utf-8")
    diff_text, recovered = compute_round_diff(
        plan, round_n=2, slug="test", version="v0"
    )

    # Should recover via sidecar (NOT git fallback) → recovered=False
    assert recovered is False
    # Diff should show the REAL baseline against new content (not the corrupt snapshot)
    assert "# real baseline" in diff_text
    assert "# new content" in diff_text
    assert "corrupted snapshot" not in diff_text
    # Snapshot file should now contain the recovered baseline
    assert snapshot_path.read_text(encoding="utf-8") == baseline


def test_compute_round_diff_warns_on_hash_mismatch(isolated_repo, capsys):
    """I7: stderr warning when snapshot hash mismatches sidecar."""
    plan = isolated_repo / "plans" / "test-v0.md"
    plan.write_text("# baseline", encoding="utf-8")
    take_initial_snapshot(plan, slug="test", version="v0")

    # Write a sidecar with a DIFFERENT baseline hash than what's in the snapshot
    sidecar = {
        "schema_version": SCHEMA_VERSION, "round": 1,
        "started_at": "2026-05-03T00:00:00Z", "completed_at": "2026-05-03T00:00:30Z",
        "transport": "openai", "model": "gpt-5.5",
        "raw_response_text": "{}",
        "plan_content_sha256": "0" * 64, "plan_content": "# different",
        "baseline_plan_content_sha256": "1" * 64,
        "baseline_plan_content": "# also different",
        "restart_metadata": None, "deferrals_at_exit": None,
        "reviewer_response": {"status": "NO_FINDINGS", "findings": [], "open_questions": []},
        "planner_decisions": [], "plan_edits_applied": [],
        "stats": {
            "tokens_input": 0, "tokens_output": 0, "cost_usd": 0.0,
            "cumulative_cost_usd": 0.0, "duration_seconds": 0.0,
            "plan_size_chars": 0, "plan_size_delta": 0,
            "severity_histogram": {"high": 0, "medium": 0, "low": 0},
        },
    }
    (isolated_repo / "plans" / "fixs" / "v0-test-round-1.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )

    plan.write_text("# current", encoding="utf-8")
    # Hash mismatch but sidecar baseline_plan_content also has wrong hash
    # (it claims sha 1*64 but content is "# also different") — recovery returns None
    # → falls through to git fallback (branch 4 of §5.3.1).
    diff, recovered = compute_round_diff(plan, round_n=2, slug="test", version="v0")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err or "warning" in captured.err.lower()
    # IM1 (review): explicitly assert recovered=True so a regression that makes
    # `compute_round_diff` always return False on this branch is caught.
    assert recovered is True


def test_cleanup_snapshots_removes_namespaced_files(isolated_repo):
    plan = isolated_repo / "plans" / "test-v0.md"
    plan.write_text("# x", encoding="utf-8")
    take_initial_snapshot(plan, slug="test", version="v0")
    take_initial_snapshot(plan, slug="other", version="v0")

    n = cleanup_snapshots(slug="test", version="v0")
    assert n == 1
    assert not (isolated_repo / ".scratch" / "v0-test-plan-snapshot-r1.md").exists()
    # Other slug's snapshot survives
    assert (isolated_repo / ".scratch" / "v0-other-plan-snapshot-r1.md").exists()


# --- build_sidecar -----------------------------------------------------------


def test_build_sidecar_round_1_requires_baseline():
    state = _state(round_n=1, baseline=None)
    with pytest.raises(ValueError, match="baseline_plan_content"):
        build_sidecar(state, raw_response_text="{}")


def test_build_sidecar_round_n_baseline_is_null():
    state = _state(round_n=2, baseline=None)
    sidecar = build_sidecar(state, raw_response_text="{}")
    assert sidecar["baseline_plan_content"] is None
    assert sidecar["baseline_plan_content_sha256"] is None


def test_build_sidecar_serializes_findings_with_ids():
    findings = [
        Finding("high", "Pipeline", "§2", "X", "Y"),
        Finding("medium", "Verify", "§3", "Z", "W"),
    ]
    state = _state(
        round_n=1,
        review=_review(status="FINDINGS_PRESENT", findings=findings),
        baseline="# baseline",
    )
    sidecar = build_sidecar(state, raw_response_text="{}")
    f = sidecar["reviewer_response"]["findings"]
    assert f[0]["id"] == "f_r1_1"
    assert f[1]["id"] == "f_r1_2"


def test_build_sidecar_records_severity_histogram():
    findings = [
        Finding("high", "A", "B", "C", "D"),
        Finding("high", "A", "B", "C", "D"),
        Finding("low", "A", "B", "C", "D"),
    ]
    state = _state(
        review=_review(status="FINDINGS_PRESENT", findings=findings),
        baseline="# baseline",
    )
    sidecar = build_sidecar(state, raw_response_text="{}")
    assert sidecar["stats"]["severity_histogram"] == {"high": 2, "medium": 0, "low": 1}


# --- write_sidecar_atomic ---------------------------------------------------


def test_write_sidecar_atomic_creates_file(isolated_repo):
    state = _state(round_n=1, baseline="# baseline")
    sidecar = build_sidecar(state, raw_response_text="{}")
    target = write_sidecar_atomic(sidecar, slug="test", version="v0")
    assert target.name == "v0-test-round-1.json"
    assert target.exists()
    # File is well-formed JSON
    json.loads(target.read_text(encoding="utf-8"))


def test_write_sidecar_atomic_no_tmp_left_behind(isolated_repo):
    state = _state(round_n=1, baseline="# baseline")
    sidecar = build_sidecar(state, raw_response_text="{}")
    write_sidecar_atomic(sidecar, slug="test", version="v0")
    tmp_files = list((isolated_repo / "plans" / "fixs").glob("*.tmp"))
    assert tmp_files == []


# --- load_sidecars ----------------------------------------------------------


def test_load_sidecars_returns_empty_when_none(isolated_repo):
    assert load_sidecars(slug="test", version="v0") == []


def test_load_sidecars_in_numeric_order(isolated_repo, make_sidecar_factory):
    for n in [3, 1, 2]:  # write out of order
        sidecar = make_sidecar_factory(
            round_n=n, plan_content="# x",
            baseline_plan_content="# x" if n == 1 else None,
        )
        path = isolated_repo / "plans" / "fixs" / f"v0-test-round-{n}.json"
        path.write_text(json.dumps(sidecar), encoding="utf-8")
    sidecars = load_sidecars(slug="test", version="v0")
    assert [s["round"] for s in sidecars] == [1, 2, 3]


def test_load_sidecars_refuses_non_contiguous(isolated_repo, make_sidecar_factory):
    """§5.9 gap detection: missing round in middle of sequence."""
    for n in [1, 2, 4]:
        sidecar = make_sidecar_factory(
            round_n=n, plan_content="# x",
            baseline_plan_content="# x" if n == 1 else None,
        )
        path = isolated_repo / "plans" / "fixs" / f"v0-test-round-{n}.json"
        path.write_text(json.dumps(sidecar), encoding="utf-8")
    with pytest.raises(ResumeIntegrityError, match="Non-contiguous"):
        load_sidecars(slug="test", version="v0")


# --- validate_sidecar -------------------------------------------------------


def test_validate_sidecar_passes_known_good(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
    )
    validate_sidecar(sidecar)  # no raise


def test_validate_sidecar_rejects_round_2_with_baseline(make_sidecar_factory):
    """Round >= 2 must NOT carry baseline_plan_content."""
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x", baseline_plan_content="# leaked",
    )
    with pytest.raises(SidecarSchemaError):
        validate_sidecar(sidecar)


def test_validate_sidecar_rejects_round_1_without_baseline(make_sidecar_factory):
    sidecar = make_sidecar_factory(round_n=1, plan_content="# x", baseline_plan_content=None)
    with pytest.raises(SidecarSchemaError):
        validate_sidecar(sidecar)


def test_validate_sidecar_rejects_sha_mismatch(make_sidecar_factory):
    sidecar = make_sidecar_factory(round_n=1, plan_content="# x", baseline_plan_content="# y")
    sidecar["plan_content_sha256"] = "f" * 64  # fake hash
    with pytest.raises(SidecarSchemaError):
        validate_sidecar(sidecar)


# --- Resume + restore -------------------------------------------------------


def test_detect_resume_no_prior(isolated_repo):
    status = detect_resume(slug="test", version="v0")
    assert status.has_prior_run is False
    assert status.last_completed_round == 0
    assert status.cumulative_cost_usd == 0.0


def test_detect_resume_finds_prior_runs(isolated_repo, make_sidecar_factory):
    for n in [1, 2, 3]:
        sidecar = make_sidecar_factory(
            round_n=n, plan_content="# x",
            baseline_plan_content="# x" if n == 1 else None,
            cumulative_cost_usd=0.1 * n,
        )
        path = isolated_repo / "plans" / "fixs" / f"v0-test-round-{n}.json"
        path.write_text(json.dumps(sidecar), encoding="utf-8")

    status = detect_resume(slug="test", version="v0")
    assert status.has_prior_run is True
    assert status.last_completed_round == 3
    assert status.cumulative_cost_usd == pytest.approx(0.3)
    assert status.sidecar_count == 3


def test_restore_snapshots_from_sidecars(isolated_repo, make_sidecar_factory):
    """Materialize r1 from baseline + r{N+1} from each round's plan_content."""
    for n in [1, 2]:
        sidecar = make_sidecar_factory(
            round_n=n,
            plan_content=f"# round-{n} end",
            baseline_plan_content="# baseline" if n == 1 else None,
        )
        path = isolated_repo / "plans" / "fixs" / f"v0-test-round-{n}.json"
        path.write_text(json.dumps(sidecar), encoding="utf-8")

    count = restore_snapshots_from_sidecars(slug="test", version="v0")
    assert count == 3  # r1 (baseline), r2 (end of round 1), r3 (end of round 2)

    r1 = isolated_repo / ".scratch" / "v0-test-plan-snapshot-r1.md"
    r2 = isolated_repo / ".scratch" / "v0-test-plan-snapshot-r2.md"
    r3 = isolated_repo / ".scratch" / "v0-test-plan-snapshot-r3.md"
    assert r1.read_text(encoding="utf-8") == "# baseline"
    assert r2.read_text(encoding="utf-8") == "# round-1 end"
    assert r3.read_text(encoding="utf-8") == "# round-2 end"


# --- regenerate_fixes_md ----------------------------------------------------


def test_regenerate_fixes_md_creates_from_sidecars(isolated_repo, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
    )
    path = isolated_repo / "plans" / "fixs" / "v0-test-round-1.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    target = regenerate_fixes_md(slug="test", version="v0")
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "# Fixes log: test v0" in content
    assert "## Round 1" in content


def test_regenerate_fixes_md_overwrites_hand_edits(
    isolated_repo, make_sidecar_factory
):
    """§5.7.5 case B: hand edits are silently overwritten on regen."""
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
    )
    path = isolated_repo / "plans" / "fixs" / "v0-test-round-1.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    fixes_md = isolated_repo / "plans" / "fixs" / "v0-test-fixes.md"
    fixes_md.write_text("HAND EDITED — should be overwritten", encoding="utf-8")

    regenerate_fixes_md(slug="test", version="v0")
    assert "HAND EDITED" not in fixes_md.read_text(encoding="utf-8")
    assert "# Fixes log" in fixes_md.read_text(encoding="utf-8")


# --- plan_start_over / execute_start_over ----------------------------------


def test_plan_start_over_excludes_plan_markdown(
    isolated_repo, make_sidecar_factory
):
    """§5.0 NEVER permitted: plan markdown deletion."""
    plan_md = isolated_repo / "plans" / "test-v0.md"
    plan_md.write_text("# user-authored plan", encoding="utf-8")

    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
    )
    (isolated_repo / "plans" / "fixs" / "v0-test-round-1.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )
    (isolated_repo / "plans" / "fixs" / "v0-test-fixes.md").write_text(
        "rendered", encoding="utf-8"
    )
    (isolated_repo / ".scratch" / "v0-test-plan-snapshot-r1.md").write_text(
        "snap", encoding="utf-8"
    )

    plan = plan_start_over(slug="test", version="v0")
    assert all(plan_md != p for p in plan.sidecars)
    assert plan.fixes_md is not None
    assert plan_md != plan.fixes_md
    assert len(plan.sidecars) == 1
    assert len(plan.snapshots) == 1


def test_execute_start_over_deletes_files_and_returns_metadata(
    isolated_repo, make_sidecar_factory
):
    sidecar_path = isolated_repo / "plans" / "fixs" / "v0-test-round-1.json"
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y"
    )
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    fixes_md = isolated_repo / "plans" / "fixs" / "v0-test-fixes.md"
    fixes_md.write_text("rendered", encoding="utf-8")

    plan_obj = plan_start_over(slug="test", version="v0")
    metadata = execute_start_over(
        plan_obj,
        user_decision="user chose start over via AskUserQuestion",
        previous_run_summary={"last_round": 1, "last_status": "ceiling_hit"},
    )

    assert not sidecar_path.exists()
    assert not fixes_md.exists()
    assert "user chose start over" in metadata["user_decision"]
    assert metadata["previous_run_summary"]["last_round"] == 1
    # `plan_start_over` walks Path("plans/fixs") which is cwd-relative, so
    # paths in deleted_files are relative. Match by suffix instead of full path.
    assert any(
        p.endswith("v0-test-round-1.json") for p in metadata["deleted_files"]
    )


# --- Schema version ---------------------------------------------------------


def test_schema_version_is_2_0_0():
    assert SCHEMA_VERSION == "2.0.0"


# --- ExitDecision dataclass --------------------------------------------------


def test_exit_decision_is_frozen():
    decision = ExitDecision(
        reason=ExitReason.APPROVED, open_highs=[], open_mediums=[],
        open_questions=[], needs_soft_block=False,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        decision.reason = ExitReason.PLANNER_LOCKED


# --- Deferral / PlanEdit dataclasses ----------------------------------------


def test_deferral_to_dict():
    d = Deferral("f_r1_1", "medium", "deferred to v2.1", "v2.1")
    assert d.to_dict() == {
        "item_id": "f_r1_1",
        "severity": "medium",
        "reason": "deferred to v2.1",
        "target_version": "v2.1",
    }


def test_plan_edit_to_dict():
    e = PlanEdit("§5.3", "rewrote selection")
    assert e.to_dict() == {"section": "§5.3", "summary": "rewrote selection"}
