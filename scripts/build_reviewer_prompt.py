#!/usr/bin/env python3
"""Build the adversarial plan-review prompt for Codex CLI.

Reads the current plan markdown and the append-only fixes log, then emits the
full reviewer prompt to stdout. The parent skill pipes this into
`codex-companion.mjs task`.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

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
</finding_bar>

<calibration>
Prefer one strong finding over several weak ones. If the plan is genuinely sound,
return the exact token "NO FINDINGS" and nothing else.
</calibration>

<output_format>
Either:
  - the exact token "NO FINDINGS" (and nothing else), OR
  - a numbered list of material findings, optionally followed by an
    "OPEN QUESTIONS:" block of things you are genuinely unsure about and want
    the human to weigh in on before the planner proceeds.

Format:

1. [Category] <finding body>
2. [Category] ...

OPEN QUESTIONS:
- <question or uncertainty, one per line>

Use OPEN QUESTIONS only for material uncertainty — do not fabricate questions
to look thorough. No preamble, no summary, no praise.
</output_format>
"""

ROUND_ONE_INSTRUCTION = "Review this plan."

LATER_ROUND_INSTRUCTION = (
    "This is the updated plan after the planner addressed prior findings. "
    "Check (1) were accepted findings properly addressed, (2) are there new "
    "critical issues introduced by the edits. Apply the same bar."
)


ROUND_HEADER_RE = re.compile(r"^## Round (\d+)\b.*$", re.MULTILINE)
SUBSECTION_RE = re.compile(
    r"### Reviewer findings \(Codex\)\s*\n(?P<findings>.*?)"
    r"### Planner decisions \(Claude\)\s*\n(?P<decisions>.*?)"
    r"### Plan edits applied\s*\n(?P<edits>.*?)(?=\Z|^## )",
    re.DOTALL | re.MULTILINE,
)


def parse_prior_rounds(fixes_text: str, current_round: int) -> list[dict]:
    """Extract completed rounds (< current_round) from the fixes md."""
    rounds = []
    # Split by round headers; keep associated numbers.
    splits = list(ROUND_HEADER_RE.finditer(fixes_text))
    for i, m in enumerate(splits):
        n = int(m.group(1))
        if n >= current_round:
            continue
        start = m.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(fixes_text)
        body = fixes_text[start:end]
        sub = SUBSECTION_RE.search(body)
        if not sub:
            continue
        rounds.append(
            {
                "n": n,
                "findings": sub.group("findings").strip(),
                "decisions": sub.group("decisions").strip(),
                "edits": sub.group("edits").strip(),
            }
        )
    rounds.sort(key=lambda r: r["n"])
    return rounds


def render_prior_rounds(rounds: list[dict]) -> str:
    if not rounds:
        return ""
    parts = ["<prior_rounds>"]
    for r in rounds:
        parts.append(f'<prior_round n="{r["n"]}">')
        parts.append("<reviewer_findings>")
        parts.append(r["findings"])
        parts.append("</reviewer_findings>")
        parts.append("<planner_decisions>")
        parts.append(r["decisions"])
        parts.append("</planner_decisions>")
        parts.append("<plan_edits_applied>")
        parts.append(r["edits"])
        parts.append("</plan_edits_applied>")
        parts.append("</prior_round>")
    parts.append("</prior_rounds>")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-file", required=True, type=Path)
    parser.add_argument("--fixes-file", required=True, type=Path)
    parser.add_argument("--round", required=True, type=int)
    args = parser.parse_args()

    if not args.plan_file.exists():
        print(f"ERROR: plan file not found: {args.plan_file}", file=sys.stderr)
        return 2

    plan_content = args.plan_file.read_text(encoding="utf-8")

    prior_rounds: list[dict] = []
    if args.fixes_file.exists():
        fixes_text = args.fixes_file.read_text(encoding="utf-8")
        prior_rounds = parse_prior_rounds(fixes_text, args.round)

    instruction = ROUND_ONE_INSTRUCTION if args.round == 1 else LATER_ROUND_INSTRUCTION
    prior_block = render_prior_rounds(prior_rounds)

    sections = [ROLE]
    if prior_block:
        sections.append(prior_block)
    sections.append(f"<plan>\n{plan_content}\n</plan>")
    sections.append(instruction)

    sys.stdout.write("\n\n".join(sections))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
