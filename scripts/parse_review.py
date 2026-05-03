"""Parse adversarial reviewer output into a structured ReviewResult.

Two transport paths land here, both producing the same dataclass shape:

- OpenAI Responses API path: structured JSON, schema-validated server-side.
  We re-validate locally against REVIEW_SCHEMA + run cross-field invariants
  in `validate_review_invariants()`.
- Codex CLI prose path: free-form numbered findings + optional OPEN QUESTIONS
  block. Severity inferred via keyword heuristic (see `infer_severity()`).

See plans/v2-plan.md §5.2 for the full design rationale and schema spec.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# --- Runtime schema for OpenAI strict structured outputs ---------------------
#
# Limited to the conservative subset OpenAI strict mode supports:
#   object, properties, required, additionalProperties, type, enum, items,
#   array.
#
# Annotation keywords (description, comment, $comment, title, examples) are
# intentionally absent — they may be rejected by the strict-mode validator at
# request time and would block reviewer invocation. Field semantics live in
# the v2-plan §5.2 markdown, not here.
#
# Cross-field invariants (NO_FINDINGS implies empty findings AND empty
# open_questions; FINDINGS_PRESENT requires at least one finding OR open
# question) are NOT in this schema — they are enforced post-parse in
# `validate_review_invariants()` because conditional keywords (allOf, if/then,
# const) are not guaranteed in the strict subset.
#
# See v2-plan §5.2 round-5 finding 2 and round-6 finding 2 for the history.

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "findings", "open_questions"],
    "properties": {
        # `type: "string"` paired with enum is defensive — code-review
        # finding I2 noted that OpenAI strict mode is known to reject enum
        # nodes that lack an accompanying type. The Phase 4 contract test
        # is the eventual canonical check, but adding the type here costs
        # nothing and removes the runtime risk in the meantime.
        "status": {"type": "string", "enum": ["NO_FINDINGS", "FINDINGS_PRESENT"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "severity",
                    "category",
                    "where",
                    "what_can_go_wrong",
                    "concrete_fix",
                ],
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {"type": "string"},
                    "where": {"type": "string"},
                    "what_can_go_wrong": {"type": "string"},
                    "concrete_fix": {"type": "string"},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


# --- Dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    severity: str  # "high" | "medium" | "low"
    category: str
    where: str
    what_can_go_wrong: str
    concrete_fix: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "category": self.category,
            "where": self.where,
            "what_can_go_wrong": self.what_can_go_wrong,
            "concrete_fix": self.concrete_fix,
        }


@dataclass(frozen=True)
class OpenQuestion:
    """An open question with a stable round-scoped ID.

    ID format: oq_r{round}_{1-based-index}. See v2-plan §5.2 round-14 finding 2
    for the rationale.
    """

    id: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text}


@dataclass
class ReviewUsage:
    """Token + cost accounting for a single reviewer invocation."""

    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0


@dataclass
class ReviewResult:
    """Parsed reviewer output, transport-agnostic."""

    status: str  # "NO_FINDINGS" | "FINDINGS_PRESENT"
    findings: list[Finding] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    raw_response_text: str = ""
    transport: str = ""  # "openai" | "codex"
    model: str = ""
    usage: ReviewUsage = field(default_factory=ReviewUsage)

    @property
    def severity_histogram(self) -> dict[str, int]:
        h = {"high": 0, "medium": 0, "low": 0}
        for f in self.findings:
            h[f.severity] += 1
        return h


# --- Errors ------------------------------------------------------------------


class ReviewSchemaError(ValueError):
    """Reviewer output failed schema or cross-field invariant validation.

    Triggers retry-once-then-fail at the caller layer (see v2-plan D20).
    """


# --- OpenAI structured-output parser -----------------------------------------


def parse_openai_response(
    raw_response_text: str,
    *,
    round_n: int,
    model: str,
    usage_input_tokens: int = 0,
    usage_output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> ReviewResult:
    """Parse the JSON string produced by the OpenAI Responses API path."""
    try:
        parsed = json.loads(raw_response_text)
    except json.JSONDecodeError as exc:
        raise ReviewSchemaError(f"OpenAI response is not valid JSON: {exc}") from exc

    _validate_review_schema(parsed)
    validate_review_invariants(parsed)

    findings = [
        Finding(
            severity=f["severity"],
            category=f["category"],
            where=f["where"],
            what_can_go_wrong=f["what_can_go_wrong"],
            concrete_fix=f["concrete_fix"],
        )
        for f in parsed["findings"]
    ]
    open_questions = assign_open_question_ids(parsed["open_questions"], round_n)

    return ReviewResult(
        status=parsed["status"],
        findings=findings,
        open_questions=open_questions,
        raw_response_text=raw_response_text,
        transport="openai",
        model=model,
        usage=ReviewUsage(
            tokens_input=usage_input_tokens,
            tokens_output=usage_output_tokens,
            cost_usd=cost_usd,
        ),
    )


def _validate_review_schema(parsed: Any) -> None:
    """Lightweight schema check covering the structural rules of REVIEW_SCHEMA.

    We don't pull in jsonschema as a dep — the schema is small and stable.
    Strict-mode failures from the OpenAI server are the primary defence; this
    is just a belt-and-braces check that catches malformed responses (e.g. a
    dropped field) before they trip downstream code.
    """
    if not isinstance(parsed, dict):
        raise ReviewSchemaError(f"reviewer response must be an object, got {type(parsed).__name__}")

    for key in ("status", "findings", "open_questions"):
        if key not in parsed:
            raise ReviewSchemaError(f"reviewer response missing required key '{key}'")

    if parsed["status"] not in ("NO_FINDINGS", "FINDINGS_PRESENT"):
        raise ReviewSchemaError(f"invalid status '{parsed['status']}'")

    if not isinstance(parsed["findings"], list):
        raise ReviewSchemaError("'findings' must be an array")
    for i, finding in enumerate(parsed["findings"]):
        if not isinstance(finding, dict):
            raise ReviewSchemaError(f"findings[{i}] must be an object")
        required_string_fields = (
            "severity",
            "category",
            "where",
            "what_can_go_wrong",
            "concrete_fix",
        )
        for key in required_string_fields:
            if key not in finding:
                raise ReviewSchemaError(f"findings[{i}] missing required key '{key}'")
            # Code-review finding I5: enforce string type on all five required
            # fields. A drifting server returning {"category": null, ...} would
            # otherwise produce an opaque AttributeError downstream.
            if not isinstance(finding[key], str):
                raise ReviewSchemaError(
                    f"findings[{i}].{key} must be a string, got {type(finding[key]).__name__}"
                )
        if finding["severity"] not in ("high", "medium", "low"):
            raise ReviewSchemaError(
                f"findings[{i}].severity must be high|medium|low, got '{finding['severity']}'"
            )

    if not isinstance(parsed["open_questions"], list):
        raise ReviewSchemaError("'open_questions' must be an array")
    for i, oq in enumerate(parsed["open_questions"]):
        if not isinstance(oq, str):
            raise ReviewSchemaError(f"open_questions[{i}] must be a string")


def validate_review_invariants(parsed: dict[str, Any]) -> None:
    """Enforce cross-field invariants from §5.2 that strict JSON Schema can't express.

    - NO_FINDINGS must imply empty findings AND empty open_questions
      (closes D15/D17 ambiguity from v2-plan round-1 finding 3).
    - FINDINGS_PRESENT must carry at least one finding OR one open question
      (allows pure-open-question response per round-3 finding 2).
    """
    status = parsed["status"]
    findings = parsed["findings"]
    open_qs = parsed["open_questions"]

    if status == "NO_FINDINGS":
        if findings or open_qs:
            raise ReviewSchemaError(
                f"NO_FINDINGS with non-empty findings ({len(findings)}) or "
                f"open_questions ({len(open_qs)}) — schema invariant violated"
            )
    elif status == "FINDINGS_PRESENT":
        if not findings and not open_qs:
            raise ReviewSchemaError(
                "FINDINGS_PRESENT with empty findings AND empty open_questions"
            )


def assign_open_question_ids(open_questions: list[str], round_n: int) -> list[OpenQuestion]:
    """Attach stable IDs to the bare-string open questions from the wire schema.

    Format: oq_r{round}_{1-based-index}. Round-stable (re-running the parser on
    the same raw text yields identical IDs) and cross-round-stable (each round
    produces a fresh prefix, so collisions are impossible). See v2-plan §5.2
    round-14 finding 2.
    """
    return [
        OpenQuestion(id=f"oq_r{round_n}_{i + 1}", text=text)
        for i, text in enumerate(open_questions)
    ]


# --- Codex CLI prose parser --------------------------------------------------


# Sentinels used by the v1-compat Codex prose format.
NO_FINDINGS_SENTINEL = "NO FINDINGS"
_OPEN_QUESTIONS_HEADER = re.compile(r"^OPEN QUESTIONS:\s*$", re.MULTILINE)
_NUMBERED_FINDING_PREFIX = re.compile(r"^(\d+)\.\s+", re.MULTILINE)
_CATEGORY_PREFIX = re.compile(r"^\[([^\]]+)\]\s*(.*)$", re.DOTALL)


def _has_no_findings_sentinel(section: str) -> bool:
    """True if any non-empty line of `section` is exactly the sentinel.

    Tolerates short preambles ("Reviewing the plan now…") that real Codex
    outputs sometimes emit before the verdict — see code-review finding C3.
    """
    for line in section.splitlines():
        if line.strip() == NO_FINDINGS_SENTINEL:
            return True
    return False


def parse_codex_prose(
    raw_response_text: str,
    *,
    round_n: int,
    model: str,
    usage_input_tokens: int = 0,
    usage_output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> ReviewResult:
    """Parse a Codex CLI free-form review.

    Severity is inferred via keyword heuristic — see `infer_severity()`. This
    is best-effort; OpenAI path is recommended for severity-critical work.

    Coercion rule (§5.2 reviewer-prompt corollary): if the prose contains the
    `NO FINDINGS` sentinel AND a co-located `OPEN QUESTIONS:` block, we emit
    `FINDINGS_PRESENT` with empty findings and the parsed open questions. The
    schema invariant in §5.2 explicitly accepts that shape (round-3 finding 2).
    Synthesizing fake low-severity findings for that case was the v1 behavior
    and was removed in round-11 finding 2 because it polluted the histogram.
    """
    text = raw_response_text.strip()

    open_q_match = _OPEN_QUESTIONS_HEADER.search(text)
    findings_section = text[: open_q_match.start()] if open_q_match else text
    open_q_section = text[open_q_match.end():] if open_q_match else ""

    # Detect `NO FINDINGS` sentinel anywhere in the findings section
    # (code-review finding C3): real Codex outputs sometimes include a short
    # preamble before the verdict, so a "first 3 lines" check kills the loop
    # on benign clean-review responses. Match the literal sentinel as a
    # whole non-empty line, and ALSO require there are no numbered findings
    # in the section — a stray "NO FINDINGS" inside a finding body should
    # not flip is_clean.
    is_clean = (
        _has_no_findings_sentinel(findings_section)
        and not _NUMBERED_FINDING_PREFIX.search(findings_section)
    )

    findings = [] if is_clean else _parse_numbered_findings(findings_section)
    open_questions_text = _parse_open_questions(open_q_section)

    if is_clean and not open_questions_text:
        status = "NO_FINDINGS"
    else:
        status = "FINDINGS_PRESENT"

    open_questions = assign_open_question_ids(open_questions_text, round_n)

    parsed_for_invariant_check = {
        "status": status,
        "findings": [f.to_dict() for f in findings],
        "open_questions": open_questions_text,
    }
    validate_review_invariants(parsed_for_invariant_check)

    return ReviewResult(
        status=status,
        findings=findings,
        open_questions=open_questions,
        raw_response_text=raw_response_text,
        transport="codex",
        model=model,
        usage=ReviewUsage(
            tokens_input=usage_input_tokens,
            tokens_output=usage_output_tokens,
            cost_usd=cost_usd,
        ),
    )


def _parse_numbered_findings(section: str) -> list[Finding]:
    """Split prose into numbered findings; infer severity per finding.

    Code-review finding I4: don't render placeholder strings into
    `where`/`concrete_fix`. Best-effort extraction of an inline `Fix:` /
    `Concrete fix:` clause per finding; the raw response is always preserved
    in the `### Reviewer raw response` block of fixes-md so nothing is lost
    if extraction misses.
    """
    findings: list[Finding] = []
    parts = _NUMBERED_FINDING_PREFIX.split(section)
    if len(parts) < 3:
        return findings

    for i in range(1, len(parts), 2):
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if not body:
            continue

        category, content = _split_category(body)
        severity = infer_severity(body)
        what_can_go_wrong, concrete_fix = _split_off_fix_clause(content)

        findings.append(
            Finding(
                severity=severity,
                category=category or "Uncategorized",
                where="",
                what_can_go_wrong=what_can_go_wrong,
                concrete_fix=concrete_fix,
            )
        )
    return findings


def _split_category(body: str) -> tuple[str, str]:
    """Pull the leading `[Category]` tag if present.

    Note: `_CATEGORY_PREFIX` uses re.DOTALL so the `(.*)` group greedily
    captures the entire multi-line finding body. That's intentional —
    `_split_off_fix_clause()` then teases out a `Fix:` line if present.
    """
    match = _CATEGORY_PREFIX.match(body)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", body


_FIX_CLAUSE = re.compile(
    r"\b(?:concrete fix|fix|recommendation|suggested fix)\s*:\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)


def _split_off_fix_clause(content: str) -> tuple[str, str]:
    """Best-effort extraction of an inline `Fix:` / `Concrete fix:` clause.

    Returns `(what_can_go_wrong, concrete_fix)`. If no fix clause is found,
    `concrete_fix` is the empty string — the renderer will omit the
    `*Concrete fix:*` line entirely rather than print a placeholder.
    """
    match = _FIX_CLAUSE.search(content)
    if not match:
        return content.strip(), ""
    fix_text = match.group(1).strip()
    description = content[: match.start()].rstrip(" \t\r\n.,")
    if not description:
        # Whole body was a single "Fix: ..." line; treat as fix only,
        # leaving what_can_go_wrong as the original sentence verbatim.
        return content.strip(), fix_text
    return description, fix_text


def _parse_open_questions(section: str) -> list[str]:
    """Extract one open question per non-empty line, stripping bullet markers."""
    questions: list[str] = []
    for line in section.splitlines():
        cleaned = line.strip().lstrip("-*•").strip()
        if cleaned:
            questions.append(cleaned)
    return questions


def infer_severity(finding_text: str) -> str:
    """Best-effort severity inference for the Codex prose path.

    See v2-plan D23 for the keyword lists. High signals (silent failures, data
    loss, backwards-compat, security) take precedence over medium signals
    (verification gaps, ambiguity); anything else is low.
    """
    lower = finding_text.lower()
    high_kw = (
        "silent",
        "silently",
        "data loss",
        "breaks compat",
        "backwards-compat",
        "security",
        "leak",
        "expose",
    )
    medium_kw = (
        "gap",
        "ambiguous",
        "unclear",
        "missing test",
        "verification",
        "edge case",
        "may not",
    )
    if any(kw in lower for kw in high_kw):
        return "high"
    if any(kw in lower for kw in medium_kw):
        return "medium"
    return "low"
