"""Pure renderer: sidecar JSON → markdown fixes-md round section.

Byte-stable: same JSON in, same markdown out, every time. The §5.7.5 drift-
handling policy depends on this — we detect "markdown was hand-edited" by
re-rendering and comparing to what's on disk. If the renderer is non-
deterministic, drift detection becomes useless.

Three top-level entry points:

- `render_round(sidecar)` — render one round's section
- `render_full_fixes_md(header_block, sidecars)` — header + every round in
  order; used by the resume flow to regenerate fixes-md from scratch
- `render_header(slug, version, started_at, transport, model)` — the
  one-time header at the top of the fixes-md

See plans/v2-plan.md §5.7.6 for the template spec.
"""
from __future__ import annotations

import json
from typing import Any, Iterable


# Outer fence is 4 backticks so the inner ```text fence around the raw
# response renders as a real code block (code-review finding I6 from the
# Phase 1+2 review carried the lesson; we apply it consistently here).
_OUTER_FENCE = "````"
_INNER_FENCE = "```"


def render_header(
    *,
    slug: str,
    version: str,
    started_at: str,
    transport: str,
    model: str,
) -> str:
    """The one-time header at the top of the fixes-md (only first creation)."""
    return (
        f"# Fixes log: {slug} {version}\n"
        "\n"
        f"- Plan: `plans/{version}-{slug}.md`\n"
        f"- Started: {started_at}\n"
        f"- Reviewer: {_human_transport(transport)} ({model}) — "
        f"{reviewer_independence(transport)}\n"
        "- Planner: Claude\n"
        "- Termination rules: severity-gated exit | NO FINDINGS | "
        "ceiling hit | planner-locked | cost-capped (v2; see plans/v2-plan.md §5.4)\n"
    )


def render_round(sidecar: dict[str, Any]) -> str:
    """Render one round of the fixes-md from its sidecar."""
    round_n = sidecar["round"]
    transport = sidecar["transport"]
    started_at = sidecar["started_at"]

    parts: list[str] = []
    parts.append(f"## Round {round_n} — {started_at}")
    parts.append("")
    parts.append(_render_reviewer_findings(sidecar["reviewer_response"], transport))
    parts.append(_render_planner_decisions(sidecar["planner_decisions"]))

    if sidecar["plan_edits_applied"]:
        parts.append(_render_plan_edits(sidecar["plan_edits_applied"]))
    else:
        # Code-review I1: distinguish "clean review, nothing to edit" from
        # "all findings rejected". Both produce empty plan_edits_applied
        # but the explanatory copy must match the reality of the round.
        if sidecar["reviewer_response"]["status"] == "NO_FINDINGS":
            parts.append("### Plan edits applied\n\nNone — clean review (no findings to edit)\n")
        else:
            parts.append("### Plan edits applied\n\nNone — all findings rejected\n")

    if sidecar["deferrals_at_exit"]:
        parts.append(_render_deferrals(sidecar["deferrals_at_exit"]))

    parts.append(_render_round_stats(sidecar))
    parts.append(_render_raw_response(sidecar["raw_response_text"]))

    if sidecar["restart_metadata"] is not None:
        parts.append(_render_restart_metadata(sidecar["restart_metadata"]))

    return "\n".join(parts).rstrip() + "\n"


def render_full_fixes_md(
    header_block: str,
    sidecars: Iterable[dict[str, Any]],
) -> str:
    """Concatenate the header + every round's rendered section.

    Caller is responsible for sidecar order (typically sorted by `round`).
    """
    chunks: list[str] = [header_block.rstrip()]
    for sidecar in sidecars:
        chunks.append("")
        chunks.append(render_round(sidecar).rstrip())
    return "\n".join(chunks) + "\n"


# --- Section renderers -------------------------------------------------------


def _render_reviewer_findings(reviewer_response: dict[str, Any], transport: str) -> str:
    transport_label = _human_transport(transport)
    findings = reviewer_response["findings"]
    open_questions = reviewer_response["open_questions"]

    if reviewer_response["status"] == "NO_FINDINGS":
        body = (
            f"### Reviewer findings ({transport_label})\n"
            "\nNO FINDINGS — clean review (zero findings AND zero open questions per §5.2 invariant).\n"
        )
        return body

    lines: list[str] = [f"### Reviewer findings ({transport_label})", ""]
    for i, finding in enumerate(findings, start=1):
        severity = finding["severity"].upper()
        category = finding.get("category", "Uncategorized")
        where = finding.get("where", "")
        what = finding["what_can_go_wrong"]
        fix = finding.get("concrete_fix", "")

        where_suffix = f" ({where})" if where else ""
        lines.append(f"{i}. **[{severity}]** [{category}]{where_suffix} {what}")
        if fix:
            lines.append(f"   *Concrete fix:* {fix}")

    if open_questions:
        lines.append("")
        lines.append("OPEN QUESTIONS:")
        for oq in open_questions:
            lines.append(f"- ({oq['id']}) {oq['text']}")

    lines.append("")
    return "\n".join(lines)


def _render_planner_decisions(decisions: list[dict[str, Any]]) -> str:
    if not decisions:
        return "### Planner decisions (Claude)\n\nNone — reviewer returned NO_FINDINGS.\n"

    lines: list[str] = ["### Planner decisions (Claude)", ""]
    for i, decision in enumerate(decisions, start=1):
        verb = _decision_label(decision["decision"])
        rationale = decision["rationale"]
        lines.append(f"{i}. **{verb}** ({decision['item_id']}) — {rationale}")
        stated_edit = decision.get("stated_edit")
        if stated_edit:
            lines.append(f"   *Stated edit:* {stated_edit}")
    lines.append("")
    return "\n".join(lines)


def _render_plan_edits(edits: list[dict[str, Any]]) -> str:
    lines: list[str] = ["### Plan edits applied", ""]
    for edit in edits:
        lines.append(f"- {edit['section']} — {edit['summary']}")
    lines.append("")
    return "\n".join(lines)


def _render_deferrals(deferrals: list[dict[str, Any]]) -> str:
    """Render the soft-block deferrals_at_exit array (§5.4.1, §5.7.3aa)."""
    lines: list[str] = ["### Deferrals at exit", ""]
    for d in deferrals:
        sev = d["severity"]
        target = d.get("target_version")
        target_suffix = f" → {target}" if target else ""
        lines.append(
            f"- **[{sev.upper()}]** ({d['item_id']}){target_suffix}: {d['reason']}"
        )
    lines.append("")
    return "\n".join(lines)


def _render_round_stats(sidecar: dict[str, Any]) -> str:
    stats = sidecar["stats"]
    transport_label = _human_transport(sidecar["transport"])
    model = sidecar["model"]
    hist = stats["severity_histogram"]
    delta = stats["plan_size_delta"]
    delta_str = f"{delta:+,}" if delta else "+0"

    return (
        "### Round stats\n"
        "\n"
        f"- Reviewer: {transport_label} ({model}) — {independence_of(sidecar)}\n"
        f"- Tokens: {stats['tokens_input']:,} input / {stats['tokens_output']:,} output\n"
        f"- Cost: ${stats['cost_usd']:.4f} (cumulative: ${stats['cumulative_cost_usd']:.4f})\n"
        f"- Severity histogram: high={hist['high']}, medium={hist['medium']}, low={hist['low']}\n"
        f"- Duration: {stats['duration_seconds']:.1f}s\n"
        f"- Plan size: {stats['plan_size_chars']:,} chars (Δ {delta_str})\n"
    )


def _render_raw_response(raw_response_text: str) -> str:
    """Render the trailing `### Reviewer raw response` block.

    Wrapped in 3-backtick `text` fences. Caller's outer document fence
    must be 4 backticks (see _OUTER_FENCE) to keep these from terminating
    a containing code block. The renderer doesn't emit the outer fence —
    that's the caller's responsibility when embedding the rendered round
    inside other markdown (e.g. when copy-pasting into the SKILL.md
    template).
    """
    return (
        "### Reviewer raw response\n"
        "\n"
        f"{_INNER_FENCE}text\n"
        f"{raw_response_text.rstrip()}\n"
        f"{_INNER_FENCE}\n"
    )


def _render_restart_metadata(meta: dict[str, Any]) -> str:
    """Round-1 sidecar may carry restart_metadata when the round followed a destructive Start over."""
    lines: list[str] = ["### Restart metadata", ""]
    lines.append(f"- Timestamp: {meta['timestamp']}")
    lines.append(f"- User decision: {meta['user_decision']}")
    if meta["deleted_files"]:
        lines.append("- Deleted files (from prior run):")
        for path in meta["deleted_files"]:
            lines.append(f"  - {path}")
    prev = meta.get("previous_run_summary")
    if prev:
        lines.append(
            f"- Previous run: {prev['last_round']} rounds, status `{prev['last_status']}`"
        )
    lines.append("")
    return "\n".join(lines)


# --- Helpers -----------------------------------------------------------------


_DECISION_LABELS = {
    "accept": "Accept",
    "reject": "Reject",
    "uncertain": "Uncertain",
    "accept_via_user": "Accept (via user)",
    "reject_via_user": "Reject (via user)",
}


def _decision_label(decision: str) -> str:
    return _DECISION_LABELS.get(decision, decision)


_TRANSPORT_LABELS = {
    "openai": "OpenAI Responses API",
    "claude": "Claude Code CLI",
    "codex": "Codex CLI",
}


# The planner is always Claude. A Claude reviewer therefore has no
# cross-vendor independence — and this skill's whole premise is that a solo
# reviewer-planner tends to agree with itself. The claude transport still
# OUTRANKS codex in auto-detection, because it is the only one that can verify
# a plan against the repo and its severities are schema-validated rather than
# keyword-guessed; trading that away for a vendor label would be a worse
# review. The answer to the trade-off is disclosure, not reordering, so every
# round records which kind of independence it actually had.
SAME_VENDOR_TRANSPORTS = frozenset({"claude"})


def reviewer_independence(transport: str) -> str:
    """`"same-vendor"` when the reviewer shares a vendor with the planner."""
    return "same-vendor" if transport in SAME_VENDOR_TRANSPORTS else "cross-vendor"


def independence_of(sidecar: dict) -> str:
    """Read the recorded label, deriving it for sidecars written before it existed."""
    recorded = sidecar.get("reviewer_independence")
    if isinstance(recorded, str) and recorded:
        return recorded
    return reviewer_independence(sidecar.get("transport", ""))


def _human_transport(transport: str) -> str:
    """Display label for a transport; unknown values pass through verbatim."""
    return _TRANSPORT_LABELS.get(transport, transport)


# --- Round-trip self-check (used by drift detection) -------------------------


def is_byte_stable(sidecar: dict[str, Any]) -> bool:
    """Sanity check: rendering twice produces identical markdown.

    Caller can use this as a smoke test in CI. Not used at runtime — the
    renderer is structurally pure (no time/random calls), but having the
    invariant testable is cheap insurance.
    """
    first = render_round(sidecar)
    second = render_round(sidecar)
    return first == second


# --- CLI smoke entry ---------------------------------------------------------


def _main(argv: list[str]) -> int:
    """Render a single sidecar from a JSON file path. Smoke-test entry point."""
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        import sys

        print("usage: python render_markdown.py <sidecar.json>", file=sys.stderr)
        return 2

    from pathlib import Path

    sidecar = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    print(render_round(sidecar), end="")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
