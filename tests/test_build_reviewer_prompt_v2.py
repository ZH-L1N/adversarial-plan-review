"""Tests for scripts/build_reviewer_prompt_v2.py — diff-aware prompt builder."""
from __future__ import annotations

import pytest

from build_reviewer_prompt_v2 import (
    CONSISTENCY_ONLY_INSTRUCTIONS,
    LATER_ROUND_INSTRUCTIONS,
    RECENT_ROUNDS_VERBATIM,
    ROLE,
    ROUND_ONE_INSTRUCTION,
    build_prompt,
)


# --- Round 1 -----------------------------------------------------------------


def test_round_1_minimal_shape():
    """Round 1 = role + full plan + 'Review this plan.' instruction. Nothing else."""
    prompt = build_prompt(
        plan_text="# my plan\n\nbody.",
        round_n=1,
        sidecars=[],
        plan_diff="",
    )
    assert "<role>" in prompt
    assert "adversarial plan reviewer" in prompt
    assert "<full_plan>" in prompt
    assert "# my plan" in prompt
    assert ROUND_ONE_INSTRUCTION in prompt
    # Round 1 must NOT include diff / verify / consistency-only blocks
    assert "<plan_diff>" not in prompt
    assert "<accepted_findings_to_verify>" not in prompt
    assert "<rejected_findings_for_context>" not in prompt
    assert "CONSISTENCY-ONLY MODE" not in prompt


def test_round_1_uses_full_plan_tag():
    """I4: tag is `<full_plan>`, not `<plan>`."""
    prompt = build_prompt(plan_text="# x", round_n=1, sidecars=[], plan_diff="")
    assert "<full_plan>" in prompt
    assert "</full_plan>" in prompt


# --- Round N>1 required blocks ----------------------------------------------


def _sidecar_with_findings(round_n, findings, decisions=None, plan_size=1000):
    return {
        "round": round_n,
        "started_at": f"2026-05-03T00:0{round_n}:00Z",
        "transport": "openai",
        "model": "gpt-5.5",
        "reviewer_response": {
            "status": "FINDINGS_PRESENT",
            "findings": findings,
            "open_questions": [],
        },
        "planner_decisions": decisions or [],
        "plan_edits_applied": [],
        "stats": {
            "tokens_input": 1000,
            "tokens_output": 500,
            "cost_usd": 0.05,
            "cumulative_cost_usd": 0.1 * round_n,
            "duration_seconds": 10.0,
            "plan_size_chars": plan_size,
            "plan_size_delta": 0,
            "severity_histogram": {
                "high": sum(1 for f in findings if f["severity"] == "high"),
                "medium": sum(1 for f in findings if f["severity"] == "medium"),
                "low": sum(1 for f in findings if f["severity"] == "low"),
            },
        },
    }


def _finding(round_n, idx, severity="high", category="X", what="something bad", fix="fix it"):
    return {
        "id": f"f_r{round_n}_{idx}",
        "severity": severity,
        "category": category,
        "where": "§X",
        "what_can_go_wrong": what,
        "concrete_fix": fix,
    }


def test_round_2_has_all_required_blocks():
    sidecar1 = _sidecar_with_findings(
        1,
        [_finding(1, 1)],
        decisions=[
            {"item_id": "f_r1_1", "decision": "accept", "rationale": "clear", "stated_edit": "edit X"}
        ],
    )
    prompt = build_prompt(
        plan_text="# new plan",
        round_n=2,
        sidecars=[sidecar1],
        plan_diff="@@ ... @@",
        cumulative_cost_usd=0.1,
    )
    assert "<prior_rounds_summary>" in prompt
    assert "<prior_decisions>" in prompt
    assert "<accepted_findings_to_verify>" in prompt
    assert "<plan_diff>" in prompt
    assert "<full_plan>" in prompt
    assert "<instructions>" in prompt


def test_round_2_includes_diff():
    sidecar1 = _sidecar_with_findings(1, [])
    prompt = build_prompt(
        plan_text="# x",
        round_n=2,
        sidecars=[sidecar1],
        plan_diff="@@ -1 +1 @@\n-old\n+new",
    )
    assert "@@ -1 +1 @@" in prompt
    assert "+new" in prompt


def test_round_2_two_pass_instructions():
    sidecar1 = _sidecar_with_findings(1, [])
    prompt = build_prompt(plan_text="# x", round_n=2, sidecars=[sidecar1], plan_diff="")
    assert "VERIFY each accepted finding" in prompt
    assert "ADVERSARIAL pass on the diff" in prompt


def test_round_2_recovered_from_git_adds_banner():
    sidecar1 = _sidecar_with_findings(1, [])
    prompt = build_prompt(
        plan_text="# x",
        round_n=2,
        sidecars=[sidecar1],
        plan_diff="@@ ... @@",
        plan_diff_is_recovered_from_git=True,
    )
    assert "WARNING" in prompt
    assert "cumulative" in prompt.lower()


def test_round_2_consistency_only_swaps_instructions():
    sidecar1 = _sidecar_with_findings(1, [])
    prompt = build_prompt(
        plan_text="# x",
        round_n=2,
        sidecars=[sidecar1],
        plan_diff="@@ ... @@",
        consistency_only_mode=True,
    )
    assert "CONSISTENCY-ONLY MODE" in prompt
    assert "Do NOT raise new architectural concerns" in prompt
    assert "VERIFY each accepted finding" not in prompt  # full instructions replaced


# --- Prior-decisions truncation (D19) ---------------------------------------


def test_recent_rounds_verbatim_constant():
    """D19: keep last 3 rounds verbatim, older summarized."""
    assert RECENT_ROUNDS_VERBATIM == 3


def test_round_5_keeps_last_3_rounds_verbatim():
    """Rounds 2/3/4 verbatim, round 1 summarized."""
    sidecars = [
        _sidecar_with_findings(n, [_finding(n, 1, what=f"round-{n} finding")])
        for n in range(1, 5)
    ]
    prompt = build_prompt(
        plan_text="# x", round_n=5, sidecars=sidecars, plan_diff=""
    )
    # Round 1 should be 1-line summary (no <findings> body)
    assert 'summary="1 findings (h=1,m=0,l=0); accepted=0, rejected=0"' in prompt
    # Rounds 2-4 should be verbatim with full findings text
    assert "round-2 finding" in prompt
    assert "round-3 finding" in prompt
    assert "round-4 finding" in prompt
    # Round 1 finding text should NOT be in the prompt (summarized away)
    assert "round-1 finding" not in prompt


def test_round_4_all_3_prior_verbatim():
    """At round 4, exactly 3 priors → all verbatim, no summaries."""
    sidecars = [
        _sidecar_with_findings(n, [_finding(n, 1, what=f"round-{n} body")])
        for n in range(1, 4)
    ]
    prompt = build_prompt(
        plan_text="# x", round_n=4, sidecars=sidecars, plan_diff=""
    )
    assert "round-1 body" in prompt
    assert "round-2 body" in prompt
    assert "round-3 body" in prompt
    # No `summary=` 1-liners
    assert "summary=" not in prompt


# --- accepted_findings_to_verify --------------------------------------------


def test_accepted_findings_block_uses_last_round():
    sidecar1 = _sidecar_with_findings(
        1,
        [_finding(1, 1, what="silently drops X")],
        decisions=[
            {"item_id": "f_r1_1", "decision": "accept", "rationale": "good catch", "stated_edit": "added guard"}
        ],
    )
    prompt = build_prompt(plan_text="# x", round_n=2, sidecars=[sidecar1], plan_diff="")
    assert "silently drops X" in prompt
    assert "added guard" in prompt
    # Pulled from accepted_findings block, not from the full prior_decisions
    section = prompt.split("<accepted_findings_to_verify>")[1].split("</accepted_findings_to_verify>")[0]
    assert "f_r1_1" in section
    assert "added guard" in section


def test_accepted_findings_block_says_none_when_all_rejected():
    sidecar1 = _sidecar_with_findings(
        1,
        [_finding(1, 1)],
        decisions=[
            {"item_id": "f_r1_1", "decision": "reject", "rationale": "out of scope"}
        ],
    )
    prompt = build_prompt(plan_text="# x", round_n=2, sidecars=[sidecar1], plan_diff="")
    section = prompt.split("<accepted_findings_to_verify>")[1].split("</accepted_findings_to_verify>")[0]
    assert "none" in section.lower()


# --- rejected_findings_for_context spans all priors -------------------------


def test_rejected_findings_block_spans_all_prior_rounds():
    """A finding rejected 5 rounds ago should still appear so reviewer doesn't re-raise."""
    sidecars = [
        _sidecar_with_findings(
            1,
            [_finding(1, 1, what="ancient rejected concern")],
            decisions=[
                {"item_id": "f_r1_1", "decision": "reject", "rationale": "out of scope"}
            ],
        ),
        _sidecar_with_findings(2, [], decisions=[]),
        _sidecar_with_findings(3, [], decisions=[]),
        _sidecar_with_findings(4, [], decisions=[]),
    ]
    prompt = build_prompt(plan_text="# x", round_n=5, sidecars=sidecars, plan_diff="")
    section = prompt.split("<rejected_findings_for_context>")[1].split(
        "</rejected_findings_for_context>"
    )[0]
    assert "ancient rejected concern" in section
    assert "out of scope" in section
    assert "Round 1" in section


def test_rejected_findings_block_omitted_when_no_rejections():
    sidecar1 = _sidecar_with_findings(
        1,
        [_finding(1, 1)],
        decisions=[
            {"item_id": "f_r1_1", "decision": "accept", "rationale": "x", "stated_edit": "y"}
        ],
    )
    prompt = build_prompt(plan_text="# x", round_n=2, sidecars=[sidecar1], plan_diff="")
    assert "<rejected_findings_for_context>" not in prompt


# --- prior_rounds_summary ---------------------------------------------------


def test_summary_block_reports_severity_histogram():
    sidecar1 = _sidecar_with_findings(
        1,
        [_finding(1, 1, severity="high"), _finding(1, 2, severity="medium")],
        decisions=[
            {"item_id": "f_r1_1", "decision": "accept", "rationale": "x"},
            {"item_id": "f_r1_2", "decision": "accept", "rationale": "y"},
        ],
    )
    prompt = build_prompt(plan_text="# x", round_n=2, sidecars=[sidecar1], plan_diff="")
    section = prompt.split("<prior_rounds_summary>")[1].split("</prior_rounds_summary>")[0]
    assert "Round 1" in section
    assert "high=1" in section
    assert "medium=1" in section
    assert "low=0" in section
    assert "accepted 2" in section
    assert "$0.0" in section  # cumulative cost rendered


def test_summary_block_handles_no_priors():
    """Edge case: round_n=2 but sidecars=[] (shouldn't happen in practice)."""
    prompt = build_prompt(plan_text="# x", round_n=2, sidecars=[], plan_diff="")
    assert "(no prior rounds)" in prompt


# --- Role + operating stance always present --------------------------------


def test_role_block_present_in_round_1():
    prompt = build_prompt(plan_text="# x", round_n=1, sidecars=[], plan_diff="")
    assert ROLE.strip() in prompt


def test_role_block_present_in_round_n():
    prompt = build_prompt(
        plan_text="# x", round_n=2, sidecars=[_sidecar_with_findings(1, [])], plan_diff=""
    )
    assert ROLE.strip() in prompt


def test_role_block_mentions_severity_tagging():
    """High/medium/low must appear in the finding_bar so the reviewer tags them."""
    assert "high" in ROLE
    assert "medium" in ROLE
    assert "low" in ROLE


# --- Instructions content ---------------------------------------------------


def test_later_round_instructions_constant_has_two_pass():
    assert "Two-pass review" in LATER_ROUND_INSTRUCTIONS
    assert "VERIFY" in LATER_ROUND_INSTRUCTIONS
    assert "ADVERSARIAL pass" in LATER_ROUND_INSTRUCTIONS


def test_consistency_only_instructions_constant_narrows_scope():
    assert "CONSISTENCY-ONLY MODE" in CONSISTENCY_ONLY_INSTRUCTIONS
    assert "Do NOT raise new architectural concerns" in CONSISTENCY_ONLY_INSTRUCTIONS
    assert "stale cross-references" in CONSISTENCY_ONLY_INSTRUCTIONS
