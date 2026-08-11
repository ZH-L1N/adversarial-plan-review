#!/usr/bin/env python3
"""Diff-aware reviewer prompt builder for v2 (Phase 3).

Round 1 emits a full-plan prompt (matching the v1 builder shape so the
reviewer's adversarial role/operating-stance/etc. are unchanged). Round
N>1 emits a richer prompt with:

- Prior-rounds summary (severity histogram + cumulative cost)
- Last 3 rounds verbatim, older rounds 1-line summarized
- Accepted findings to verify (with planner's stated edits)
- Rejected findings for context (so the reviewer doesn't re-raise them)
- Plan diff (snapshot-based; see `loop_state.compute_round_diff`)
- Full plan text for cross-reference
- Two-pass verify-then-attack instructions

The v1 builder (`build_reviewer_prompt.py`) is kept for the Codex-only
fallback debugging path per v2-plan §7 D16; this v2 builder is the active
default for Phase 3+ runs.

See plans/v2-plan.md §5.3 for the full design rationale.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from parse_review import REVIEW_SCHEMA


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROLE = """<role>
You are an adversarial plan reviewer. Your job is to break confidence in this
implementation plan, not to validate it. You are a discriminator in a GAN-style
loop — the planner is a different agent that will accept or reject your findings.
</role>

<operating_stance>
Default to skepticism. Assume the plan can fail in subtle, high-cost, or
user-visible ways until the evidence says otherwise. Do not give credit for good
intent, partial coverage, or likely follow-up work. Happy-path-only coverage is
a real weakness.
</operating_stance>

<attack_surface>
Prioritize expensive, dangerous, or hard-to-detect failure modes:
- unstated assumptions about existing code, config, or dependencies
- backwards-compat claims without evidence from the actual current state
- platform/environment risks (Windows vs Linux, RPi, tools like jq)
- safety limits, error handling, degraded-dependency behavior
- test strategy gaps — missing edge cases, no failure-mode tests
- verification/rollout steps that cannot actually detect silent failure
- encoding/i18n hazards (mojibake in output files)
- scope creep or ambiguity in what "done" means
</attack_surface>

<finding_bar>
Report only material findings. No style nits, no speculative concerns without
grounding in the plan text. Each finding must answer:
1. What can go wrong?
2. Why is the plan vulnerable here?
3. What is the likely impact?
4. What concrete change would reduce the risk?

Tag every finding with severity: "high" (silent failures, data loss, backwards-
compat breaks, security risks, contract violations), "medium" (verification
gaps, ambiguity, missing edge-case coverage), or "low" (clarity / wording).
</finding_bar>

<calibration>
Prefer one strong finding over several weak ones. If the plan is genuinely
sound, return status="NO_FINDINGS" with empty findings AND empty open_questions.
</calibration>
"""


# --- Claude-transport calibration blocks -------------------------------------
#
# Appended after ROLE (i.e. after `<finding_bar>`) for `transport="claude"` ONLY
# — the openai/codex prompts stay byte-identical, pinned by a regression test.
# The third claude-only block, `<output_format>`, is built further down from
# `REVIEW_SCHEMA`.
#
# Why they exist: post-hoc analysis of 68 review rounds found the Claude
# reviewer's edge is repo access (all 5 of its round-1 HIGHs required opening
# repo files — a defect class a text-only transport structurally cannot see),
# and its weakness is volume (12 findings in round 1 vs GPT's historical 3–6,
# with 3–5 lows/round where the churn lived). These two blocks keep the
# capability and import the discipline.

CLAUDE_REPO_VERIFICATION_ROUND_ONE = """<repo_verification>
You run inside the plan's repo with Read/Grep/Glob/Bash. Verify the plan against
reality: open every file it cites, check named fixtures/helpers/config keys
exist, lint-probe embedded code against the repo's actual linter config, verify
the library versions its claims depend on. Repo claims require
personally-verified `file:line` (or probe-output) evidence. Clean up scratch
files; never modify tracked files; never run git write commands.
</repo_verification>"""


CLAUDE_REPO_VERIFICATION_LATER_ROUNDS = """<repo_verification>
Verify prior-round resolutions in the plan diff, then hunt only for new
implementation-breaking defects — no full re-sweep.
</repo_verification>"""


CLAUDE_FINDING_DISCIPLINE = """<finding_discipline>
At most 8 findings, ranked by impact; at most 3 `low`; count anything below the
bar in a final `suppressed: N below-bar observations` line rather than reporting
it. HIGH/MEDIUM are the working currency; `low` only when it still changes
implementation outcome.
</finding_discipline>"""


# --- Claude JSON output contract ---------------------------------------------
#
# The openai path gets its contract enforced server-side (strict structured
# outputs built from `REVIEW_SCHEMA`); the claude CLI has NOTHING, so the prompt
# must state it or `parse_claude_response` is left guessing at free-form prose.
#
# The block is DERIVED from `REVIEW_SCHEMA` at import time — field names, types,
# enums and the `additionalProperties: false` semantics all come from the
# validator itself, so the prompt cannot drift from what the parser accepts.


def _schema_type_label(prop_schema: dict[str, Any]) -> str:
    """Human-readable type for one property, including a closed enum."""
    type_name = str(prop_schema.get("type", "any"))
    enum = prop_schema.get("enum")
    if enum:
        return f"{type_name} — one of {' | '.join(json.dumps(v) for v in enum)}"
    if type_name == "array":
        items = prop_schema.get("items") or {}
        item_type = str(items.get("type", "any"))
        plural = "objects" if item_type == "object" else item_type
        return f"array of {plural} (may be empty)"
    return type_name


def _object_contract_lines(schema: dict[str, Any], *, subject: str) -> list[str]:
    """Render one object schema as prompt bullets: key names, types, closedness."""
    properties: dict[str, Any] = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    closed_suffix = (
        " and NO other keys (additionalProperties: false)"
        if schema.get("additionalProperties") is False
        else ""
    )
    lines = [f"{subject} — these {len(properties)} keys{closed_suffix}:"]
    for name, prop in properties.items():
        optional_suffix = "" if name in required else " (optional)"
        lines.append(f'- "{name}": {_schema_type_label(prop)}{optional_suffix}')
    return lines


def _render_claude_output_format() -> str:
    lines = [
        "<output_format>",
        "Return EXACTLY ONE JSON object and nothing else — no preamble, no",
        "summary, no praise, and no markdown code fences around it. Emit every",
        "key listed below, empty arrays included, unless it is tagged (optional).",
        "",
    ]
    lines.extend(_object_contract_lines(REVIEW_SCHEMA, subject="Top-level object"))
    for name, prop in (REVIEW_SCHEMA.get("properties") or {}).items():
        items = prop.get("items")
        if (
            prop.get("type") == "array"
            and isinstance(items, dict)
            and items.get("type") == "object"
        ):
            lines.append("")
            lines.extend(
                _object_contract_lines(items, subject=f'Each object in "{name}"')
            )
    lines.extend(
        [
            "",
            'Cross-field invariants: status "NO_FINDINGS" requires findings AND',
            'open_questions to BOTH be empty; status "FINDINGS_PRESENT" requires',
            "at least one finding OR one open question.",
            "",
            "The ONLY text permitted outside the object is the single trailing",
            "`suppressed: N below-bar observations` line from <finding_discipline>.",
            "</output_format>",
        ]
    )
    return "\n".join(lines)


CLAUDE_OUTPUT_FORMAT = _render_claude_output_format()


ROUND_ONE_INSTRUCTION = "Review this plan."


LATER_ROUND_INSTRUCTIONS = """Two-pass review:
1. VERIFY each accepted finding from prior round was actually addressed by the
   diff. Check the planner's stated edit matches what's in the diff.
   Disagreement between stated intent and actual edit is itself a finding.
   Re-raise as a finding if the fix is incomplete or introduces new risk.
2. ADVERSARIAL pass on the diff specifically. Look for new contracts, fields,
   or function signatures introduced this round. Find risks the planner
   missed in their own edits.

Do NOT re-raise findings already rejected in prior rounds unless new evidence
emerges from the diff. Do NOT scrub the unchanged sections — that's plan-bloat
checking, not adversarial review.
"""


CONSISTENCY_ONLY_INSTRUCTIONS = """You are now in CONSISTENCY-ONLY MODE. Your task this round is narrow:
1. Find stale cross-references introduced by prior edits (e.g., section A
   references field X that was renamed to Y in section B).
2. Find duplicated specs (two sections both claim authority over the same
   contract with conflicting wording).
3. Find dangling references (mentions of files / sections that no longer exist).

Do NOT raise new architectural concerns. Do NOT re-evaluate design decisions.
If you find no consistency issues, return status="NO_FINDINGS" to exit the loop.
"""


# Maximum number of prior rounds rendered verbatim. Older rounds collapse to a
# 1-line summary (round-N: K findings, h=X m=Y l=Z). See v2-plan §5.3 + D19.
RECENT_ROUNDS_VERBATIM = 3


# --- Public entry point ------------------------------------------------------


def build_prompt(
    *,
    plan_text: str,
    round_n: int,
    sidecars: list[dict[str, Any]],
    plan_diff: str,
    plan_diff_is_recovered_from_git: bool = False,
    consistency_only_mode: bool = False,
    cumulative_cost_usd: float = 0.0,
    transport: str = "openai",
) -> str:
    """Build the reviewer prompt for round N.

    Args:
        plan_text: Current plan markdown.
        round_n: 1-indexed round number.
        sidecars: All prior-round sidecars (round 1..N-1) in order.
        plan_diff: Unified diff between r{N-1} snapshot and current plan.
                   Empty/ignored on round 1.
        plan_diff_is_recovered_from_git: True when the diff is the degraded
            cumulative-against-HEAD form from `_recover_diff_from_git`. The
            reviewer is told to expect cumulative semantics.
        consistency_only_mode: True when the user picked consistency-only
            from the plan-bloat warning (§5.5.1). Replaces the verify-then-
            attack instructions with the narrow scrub-only spec.
        cumulative_cost_usd: Running total for the prior-rounds-summary block.
        transport: Active reviewer transport. `"claude"` appends the
            repo-verification + finding-discipline calibration blocks and the
            `REVIEW_SCHEMA`-derived output contract; every other value
            (including the `"openai"` default and `"codex"`) produces the
            byte-identical pre-claude prompt.
    """
    sections: list[str] = [ROLE]
    sections.extend(_claude_calibration_blocks(transport=transport, round_n=round_n))

    if round_n == 1:
        sections.append(_full_plan_block(plan_text))
        sections.append(ROUND_ONE_INSTRUCTION)
        return "\n\n".join(sections) + "\n"

    sections.append(
        _prior_rounds_summary_block(sidecars, cumulative_cost_usd=cumulative_cost_usd)
    )
    sections.append(_prior_decisions_block(sidecars))
    sections.append(_accepted_findings_to_verify_block(sidecars))
    sections.append(_rejected_findings_for_context_block(sidecars))
    sections.append(_plan_diff_block(plan_diff, recovered_from_git=plan_diff_is_recovered_from_git))
    sections.append(_full_plan_block(plan_text))

    if consistency_only_mode:
        sections.append(_instructions_block(CONSISTENCY_ONLY_INSTRUCTIONS))
    else:
        sections.append(_instructions_block(LATER_ROUND_INSTRUCTIONS))

    return "\n\n".join(sections) + "\n"


# --- Prompt section builders -------------------------------------------------


def _claude_calibration_blocks(*, transport: str, round_n: int) -> list[str]:
    """The claude-only calibration sections, or `[]` for every other transport."""
    if transport != "claude":
        return []
    repo_verification = (
        CLAUDE_REPO_VERIFICATION_ROUND_ONE
        if round_n == 1
        else CLAUDE_REPO_VERIFICATION_LATER_ROUNDS
    )
    return [repo_verification, CLAUDE_FINDING_DISCIPLINE, CLAUDE_OUTPUT_FORMAT]


def _full_plan_block(plan_text: str) -> str:
    # Tag is `<full_plan>` to match the §5.3 contract description and let the
    # reviewer's prompt easily distinguish it from plan_diff content.
    return f"<full_plan>\n{plan_text}\n</full_plan>"


def _instructions_block(text: str) -> str:
    return f"<instructions>\n{text}</instructions>"


def _prior_rounds_summary_block(
    sidecars: list[dict[str, Any]],
    *,
    cumulative_cost_usd: float,
) -> str:
    if not sidecars:
        return "<prior_rounds_summary>\n(no prior rounds)\n</prior_rounds_summary>"

    last = sidecars[-1]
    findings_count = len(last["reviewer_response"]["findings"])
    decisions = last["planner_decisions"]
    accepted = sum(1 for d in decisions if d["decision"].startswith("accept"))
    rejected = sum(1 for d in decisions if d["decision"].startswith("reject"))
    deferred = sum(1 for d in decisions if d["decision"] == "uncertain")
    hist = last["stats"]["severity_histogram"]
    body = (
        f"Round {last['round']} raised {findings_count} findings. "
        f"Planner accepted {accepted}, rejected {rejected}, "
        f"deferred {deferred} to user.\n"
        f"Severity histogram: high={hist['high']}, medium={hist['medium']}, "
        f"low={hist['low']}\n"
        f"Cumulative cost so far: ${cumulative_cost_usd:.4f}"
    )
    return f"<prior_rounds_summary>\n{body}\n</prior_rounds_summary>"


def _prior_decisions_block(sidecars: list[dict[str, Any]]) -> str:
    """Render last `RECENT_ROUNDS_VERBATIM` rounds verbatim, older as 1-liners."""
    if not sidecars:
        return "<prior_decisions>\n(none)\n</prior_decisions>"

    parts: list[str] = ["<prior_decisions>"]
    older = sidecars[:-RECENT_ROUNDS_VERBATIM] if len(sidecars) > RECENT_ROUNDS_VERBATIM else []
    recent = sidecars[-RECENT_ROUNDS_VERBATIM:] if older else sidecars

    for s in older:
        parts.append(_one_line_round_summary(s))
    for s in recent:
        parts.append(_verbatim_round_block(s))
    parts.append("</prior_decisions>")
    return "\n".join(parts)


def _one_line_round_summary(sidecar: dict[str, Any]) -> str:
    n = sidecar["round"]
    findings = sidecar["reviewer_response"]["findings"]
    h = sum(1 for f in findings if f["severity"] == "high")
    m = sum(1 for f in findings if f["severity"] == "medium")
    low = sum(1 for f in findings if f["severity"] == "low")
    decisions = sidecar["planner_decisions"]
    accepted = sum(1 for d in decisions if d["decision"].startswith("accept"))
    rejected = sum(1 for d in decisions if d["decision"].startswith("reject"))
    return (
        f'<round n="{n}" '
        f'summary="{len(findings)} findings (h={h},m={m},l={low}); '
        f'accepted={accepted}, rejected={rejected}"/>'
    )


def _verbatim_round_block(sidecar: dict[str, Any]) -> str:
    n = sidecar["round"]
    review = sidecar["reviewer_response"]
    findings_lines: list[str] = []
    for i, f in enumerate(review["findings"], start=1):
        findings_lines.append(
            f"  {i}. [{f['severity']}] [{f['category']}] {f['what_can_go_wrong']}"
        )
        if f.get("concrete_fix"):
            findings_lines.append(f"     Concrete fix: {f['concrete_fix']}")

    decisions_lines: list[str] = []
    for i, d in enumerate(sidecar["planner_decisions"], start=1):
        verb = d["decision"].replace("_", " ").title()
        decisions_lines.append(f"  {i}. {verb} ({d['item_id']}): {d['rationale']}")

    edits_lines = [
        f"  - {e['section']}: {e['summary']}" for e in sidecar["plan_edits_applied"]
    ] or ["  (none)"]

    body = (
        f"  <findings>\n"
        + ("\n".join(findings_lines) or "  (none)")
        + "\n  </findings>\n"
        f"  <decisions>\n"
        + ("\n".join(decisions_lines) or "  (none)")
        + "\n  </decisions>\n"
        f"  <plan_edits>\n"
        + "\n".join(edits_lines)
        + "\n  </plan_edits>"
    )
    return f'<round n="{n}">\n{body}\n</round>'


def _accepted_findings_to_verify_block(sidecars: list[dict[str, Any]]) -> str:
    """Findings the planner accepted in N-1 — reviewer must verify edits landed."""
    if not sidecars:
        return ""
    last = sidecars[-1]
    accepted = [
        d for d in last["planner_decisions"] if d["decision"].startswith("accept")
    ]
    if not accepted:
        return (
            "<accepted_findings_to_verify>\n"
            "(none — prior round had no accepted findings)\n"
            "</accepted_findings_to_verify>"
        )

    findings_by_id = {f["id"]: f for f in last["reviewer_response"]["findings"]}
    lines: list[str] = ["<accepted_findings_to_verify>"]
    lines.append(
        "For each accepted finding from the prior round, verify it was addressed "
        "by the diff. The planner's stated edit is shown for cross-reference; "
        "disagreement between stated intent and actual edit is itself a finding."
    )
    for d in accepted:
        finding = findings_by_id.get(d["item_id"])
        if finding:
            sev = finding["severity"]
            cat = finding["category"]
            verbatim = finding["what_can_go_wrong"]
            lines.append(
                f"- Finding {d['item_id']} ({sev}, [{cat}]): {verbatim}"
            )
            if d.get("stated_edit"):
                lines.append(f"  Planner's stated edit: {d['stated_edit']}")
        else:
            # Decision references a finding ID we don't have (open question, etc.)
            lines.append(f"- {d['item_id']}: {d['rationale']}")
            if d.get("stated_edit"):
                lines.append(f"  Planner's stated edit: {d['stated_edit']}")
    lines.append("</accepted_findings_to_verify>")
    return "\n".join(lines)


def _rejected_findings_for_context_block(sidecars: list[dict[str, Any]]) -> str:
    """Findings the planner rejected — reviewer should not re-raise unless new evidence.

    Includes rejections from ALL prior rounds (not just the last), so a finding
    rejected three rounds ago doesn't get re-raised because the reviewer no
    longer sees it in the verbatim block. This is a known plan-bloat concern;
    if the rejection-context list grows large we may compress it later.
    """
    rejections: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
    for sidecar in sidecars:
        findings_by_id = {f["id"]: f for f in sidecar["reviewer_response"]["findings"]}
        for d in sidecar["planner_decisions"]:
            if d["decision"].startswith("reject"):
                rejections.append(
                    (sidecar["round"], d, findings_by_id.get(d["item_id"]))
                )

    if not rejections:
        return ""

    lines: list[str] = ["<rejected_findings_for_context>"]
    lines.append(
        "The planner rejected these findings; do NOT re-raise unless new evidence:"
    )
    for round_n, decision, finding in rejections:
        if finding:
            sev = finding["severity"]
            cat = finding["category"]
            verbatim = finding["what_can_go_wrong"]
            lines.append(
                f"- Round {round_n}, finding {decision['item_id']} ({sev}, [{cat}]): {verbatim}"
            )
        else:
            lines.append(f"- Round {round_n}, item {decision['item_id']}")
        lines.append(f"  Rejection reason: {decision['rationale']}")
    lines.append("</rejected_findings_for_context>")
    return "\n".join(lines)


def _plan_diff_block(plan_diff: str, *, recovered_from_git: bool) -> str:
    if recovered_from_git:
        banner = (
            "WARNING: this diff is recovered from git, not snapshot-accurate. "
            "It is the cumulative diff against the last committed plan version, "
            "NOT the round-N-1→N delta. Apply the verify-then-attack pass against "
            "the cumulative diff rather than expecting per-round precision.\n"
        )
        return f"<plan_diff>\n{banner}\n{plan_diff}\n</plan_diff>"
    return f"<plan_diff>\n{plan_diff}\n</plan_diff>"


# --- CLI entry point ---------------------------------------------------------


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-file", required=True, type=Path)
    parser.add_argument("--round", required=True, type=int, dest="round_n")
    parser.add_argument(
        "--sidecars-dir",
        type=Path,
        default=Path("plans/fixs"),
        help="Directory containing per-round JSON sidecars",
    )
    parser.add_argument("--slug", required=True, type=str)
    parser.add_argument("--version", required=True, type=str)
    parser.add_argument(
        "--diff-file",
        type=Path,
        default=None,
        help="Unified diff for round N. Required when --round > 1.",
    )
    parser.add_argument(
        "--diff-recovered-from-git",
        action="store_true",
        help="Diff is from _recover_diff_from_git, not snapshot-accurate",
    )
    parser.add_argument(
        "--consistency-only",
        action="store_true",
        help="Use the §5.5.1 consistency-only-mode instructions",
    )
    parser.add_argument(
        "--cumulative-cost-usd",
        type=float,
        default=0.0,
        help="Running cumulative cost for the prior-rounds-summary block",
    )
    parser.add_argument(
        "--transport",
        choices=["openai", "codex", "claude"],
        default="openai",
        help=(
            "Active reviewer transport. 'claude' appends the repo-verification "
            "+ finding-discipline calibration blocks and the JSON output "
            "contract; openai/codex are identical."
        ),
    )
    args = parser.parse_args(argv)

    if not args.plan_file.exists():
        print(f"ERROR: plan file not found: {args.plan_file}", file=sys.stderr)
        return 2

    plan_text = args.plan_file.read_text(encoding="utf-8")

    sidecars: list[dict[str, Any]] = []
    if args.round_n > 1:
        sidecars = _load_prior_sidecars(args.sidecars_dir, args.slug, args.version, args.round_n)
        if args.diff_file is None:
            print(
                "ERROR: --diff-file is required when --round > 1; pass an empty file if no diff exists",
                file=sys.stderr,
            )
            return 2

    plan_diff = ""
    if args.diff_file and args.diff_file.exists():
        plan_diff = args.diff_file.read_text(encoding="utf-8")

    prompt = build_prompt(
        plan_text=plan_text,
        round_n=args.round_n,
        sidecars=sidecars,
        plan_diff=plan_diff,
        plan_diff_is_recovered_from_git=args.diff_recovered_from_git,
        consistency_only_mode=args.consistency_only,
        cumulative_cost_usd=args.cumulative_cost_usd,
        transport=args.transport,
    )
    sys.stdout.write(prompt)
    return 0


def _load_prior_sidecars(
    sidecars_dir: Path, slug: str, version: str, current_round: int
) -> list[dict[str, Any]]:
    """Load round 1..current_round-1 sidecars in numeric order."""
    out: list[dict[str, Any]] = []
    for n in range(1, current_round):
        path = sidecars_dir / f"{version}-{slug}-round-{n}.json"
        if not path.exists():
            print(
                f"ERROR: missing sidecar {path}; cannot build round {current_round} prompt",
                file=sys.stderr,
            )
            raise SystemExit(2)
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
