"""Tests for scripts/render_markdown.py — sidecar JSON → markdown rendering."""
from __future__ import annotations

import pytest

from render_markdown import (
    is_byte_stable,
    render_full_fixes_md,
    render_header,
    render_round,
)


# --- Byte-stability ---------------------------------------------------------


def test_render_round_is_byte_stable(make_sidecar_factory):
    """§5.7.5 drift detection depends on this — same JSON in, same markdown out."""
    sidecar = make_sidecar_factory(
        round_n=1,
        plan_content="# plan",
        baseline_plan_content="# baseline",
        findings=[
            {
                "id": "f_r1_1",
                "severity": "high",
                "category": "Pipeline",
                "where": "§2",
                "what_can_go_wrong": "X breaks Y",
                "concrete_fix": "Add Z",
            }
        ],
    )
    assert is_byte_stable(sidecar) is True


def test_render_round_pure_function(make_sidecar_factory):
    """Calling render_round 5 times yields 5 identical results."""
    sidecar = make_sidecar_factory(round_n=2, plan_content="# x")
    outputs = {render_round(sidecar) for _ in range(5)}
    assert len(outputs) == 1


# --- NO_FINDINGS round renders correctly ------------------------------------


def test_render_no_findings_round(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=3,
        plan_content="# plan",
        findings=[],
        open_questions=[],
        decisions=[],
        plan_edits=[],
    )
    md = render_round(sidecar)
    assert "## Round 3" in md
    assert "NO FINDINGS — clean review" in md
    # I1 fix: NOT "all findings rejected"
    assert "all findings rejected" not in md
    assert "no findings to edit" in md


def test_render_findings_round_with_severity_prefixes(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1,
        plan_content="# plan",
        baseline_plan_content="# baseline",
        findings=[
            {
                "id": "f_r1_1",
                "severity": "high",
                "category": "Pipeline",
                "where": "§2",
                "what_can_go_wrong": "X breaks Y",
                "concrete_fix": "Add Z",
            },
            {
                "id": "f_r1_2",
                "severity": "medium",
                "category": "Verify",
                "where": "§3",
                "what_can_go_wrong": "Test missing",
                "concrete_fix": "Add test_X",
            },
        ],
    )
    md = render_round(sidecar)
    assert "**[HIGH]** [Pipeline]" in md
    assert "**[MEDIUM]** [Verify]" in md
    assert "X breaks Y" in md
    assert "*Concrete fix:* Add Z" in md


def test_render_open_questions_with_ids(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=2,
        plan_content="# plan",
        open_questions=[
            {"id": "oq_r2_1", "text": "Should X be configurable?"},
            {"id": "oq_r2_2", "text": "What about Y?"},
        ],
    )
    md = render_round(sidecar)
    assert "OPEN QUESTIONS:" in md
    assert "(oq_r2_1) Should X be configurable?" in md
    assert "(oq_r2_2) What about Y?" in md


def test_render_no_open_questions_section_when_empty(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1,
        plan_content="# plan",
        baseline_plan_content="# baseline",
        findings=[
            {
                "id": "f_r1_1",
                "severity": "low",
                "category": "X",
                "where": "Y",
                "what_can_go_wrong": "Z",
                "concrete_fix": "W",
            }
        ],
        open_questions=[],
    )
    md = render_round(sidecar)
    assert "OPEN QUESTIONS:" not in md


# --- Planner decisions ------------------------------------------------------


def test_render_planner_decisions_with_labels(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1,
        plan_content="# plan",
        baseline_plan_content="# baseline",
        findings=[_make_finding(1, 1, "high")],
        decisions=[
            {
                "item_id": "f_r1_1",
                "decision": "accept",
                "rationale": "real bug",
                "stated_edit": "section X line 2",
            }
        ],
    )
    md = render_round(sidecar)
    assert "**Accept**" in md
    assert "real bug" in md
    assert "*Stated edit:* section X line 2" in md


def test_render_decisions_with_via_user_label(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1,
        plan_content="# plan",
        baseline_plan_content="# baseline",
        decisions=[
            {
                "item_id": "f_r1_1",
                "decision": "accept_via_user",
                "rationale": "user said yes",
            }
        ],
    )
    md = render_round(sidecar)
    assert "**Accept (via user)**" in md


# --- Plan edits applied -----------------------------------------------------


def test_render_plan_edits(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1,
        plan_content="# plan",
        baseline_plan_content="# baseline",
        plan_edits=[
            {"section": "§5.3", "summary": "rewrote selection algorithm"},
            {"section": "§5.7", "summary": "added schema"},
        ],
    )
    md = render_round(sidecar)
    assert "### Plan edits applied" in md
    assert "§5.3 — rewrote selection algorithm" in md
    assert "§5.7 — added schema" in md


def test_render_plan_edits_empty_with_findings_says_rejected(make_sidecar_factory):
    """Empty plan_edits + FINDINGS_PRESENT → 'all findings rejected'."""
    sidecar = make_sidecar_factory(
        round_n=1,
        plan_content="# plan",
        baseline_plan_content="# baseline",
        findings=[_make_finding(1, 1)],
        decisions=[{"item_id": "f_r1_1", "decision": "reject", "rationale": "out of scope"}],
        plan_edits=[],
    )
    md = render_round(sidecar)
    assert "all findings rejected" in md


# --- Round stats ------------------------------------------------------------


def test_render_round_stats_block(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=2,
        plan_content="# plan",
        cost_usd=0.0345,
        cumulative_cost_usd=0.1234,
        findings=[_make_finding(2, 1, "high"), _make_finding(2, 2, "medium")],
    )
    md = render_round(sidecar)
    assert "### Round stats" in md
    assert "OpenAI Responses API (gpt-5.5)" in md
    assert "$0.0345" in md
    assert "$0.1234" in md
    assert "high=1, medium=1, low=0" in md


# --- Reviewer raw response (audit fidelity) --------------------------------


def test_render_raw_response_preserved_verbatim(make_sidecar_factory):
    """Round-11 finding 4 / round-12 finding 1: raw text rendered as-is."""
    raw = '{"status":"FINDINGS_PRESENT","findings":[]}'
    sidecar = make_sidecar_factory(round_n=1, plan_content="# x", baseline_plan_content="# y")
    sidecar["raw_response_text"] = raw
    md = render_round(sidecar)
    assert "### Reviewer raw response" in md
    assert raw in md


def test_render_raw_response_in_inner_3_backtick_fence(make_sidecar_factory):
    """Inner fence is 3 backticks; outer fence (when embedded) must be 4."""
    sidecar = make_sidecar_factory(round_n=1, plan_content="# x", baseline_plan_content="# y")
    sidecar["raw_response_text"] = "some raw text"
    md = render_round(sidecar)
    # Inner fence around raw response
    assert "```text\n" in md
    assert "\n```\n" in md


# --- Deferrals --------------------------------------------------------------


def test_render_deferrals_when_present(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=3,
        plan_content="# x",
        deferrals_at_exit=[
            {
                "item_id": "f_r3_1",
                "severity": "medium",
                "reason": "deferred to v2.1",
                "target_version": "v2.1",
            },
            {
                "item_id": "oq_r3_1",
                "severity": "open_question",
                "reason": "out of scope for now",
                "target_version": None,
            },
        ],
    )
    md = render_round(sidecar)
    assert "### Deferrals at exit" in md
    assert "**[MEDIUM]** (f_r3_1) → v2.1: deferred to v2.1" in md
    assert "**[OPEN_QUESTION]** (oq_r3_1): out of scope for now" in md


def test_render_no_deferrals_section_when_null(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=3,
        plan_content="# x",
        deferrals_at_exit=None,
    )
    md = render_round(sidecar)
    assert "### Deferrals at exit" not in md


# --- Restart metadata -------------------------------------------------------


def test_render_restart_metadata_when_present(make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1,
        plan_content="# fresh",
        baseline_plan_content="# fresh",
        restart_metadata={
            "timestamp": "2026-05-03T00:00:00Z",
            "deleted_files": ["plans/fixs/test-v0-round-1.json", "plans/fixs/test-v0-round-2.json"],
            "user_decision": "user chose start over",
            "previous_run_summary": {"last_round": 5, "last_status": "ceiling_hit"},
        },
    )
    md = render_round(sidecar)
    assert "### Restart metadata" in md
    assert "user chose start over" in md
    assert "5 rounds" in md
    assert "ceiling_hit" in md
    assert "plans/fixs/test-v0-round-1.json" in md


# --- render_header ----------------------------------------------------------


def test_render_header_basic():
    header = render_header(
        slug="optical-lcoe",
        version="v0.0.5",
        started_at="2026-04-20T02:12:56Z",
        transport="openai",
        model="gpt-5.5",
    )
    assert "# Fixes log: optical-lcoe v0.0.5" in header
    assert "Plan: `plans/optical-lcoe-v0.0.5.md`" in header
    assert "Started: 2026-04-20T02:12:56Z" in header
    assert "OpenAI Responses API (gpt-5.5)" in header
    assert "Termination rules: severity-gated exit" in header


def test_render_header_codex_label():
    header = render_header(
        slug="x", version="v1", started_at="2026-05-03T00:00:00Z",
        transport="codex", model="gpt-5.5",
    )
    assert "Codex CLI (gpt-5.5)" in header


# --- render_full_fixes_md ---------------------------------------------------


def test_render_full_fixes_md_concatenates(make_sidecar_factory):
    sidecars = [
        make_sidecar_factory(
            round_n=1, plan_content="# plan", baseline_plan_content="# base",
            findings=[_make_finding(1, 1)],
        ),
        make_sidecar_factory(
            round_n=2, plan_content="# plan",
            findings=[_make_finding(2, 1, "medium")],
        ),
    ]
    header = render_header(
        slug="t", version="v0", started_at="2026-05-03T00:00:00Z",
        transport="openai", model="gpt-5.5",
    )
    full = render_full_fixes_md(header, sidecars)
    assert "# Fixes log: t v0" in full
    assert "## Round 1" in full
    assert "## Round 2" in full
    # Round 1 should appear before round 2
    assert full.index("## Round 1") < full.index("## Round 2")


# --- Helpers ----------------------------------------------------------------


def _make_finding(round_n, idx, severity="high"):
    return {
        "id": f"f_r{round_n}_{idx}",
        "severity": severity,
        "category": "X",
        "where": "Y",
        "what_can_go_wrong": "Z",
        "concrete_fix": "W",
    }
