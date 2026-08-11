"""Tests for scripts/parse_review.py."""
from __future__ import annotations

import json

import pytest

from parse_review import (
    NO_FINDINGS_SENTINEL,
    REVIEW_SCHEMA,
    Finding,
    OpenQuestion,
    ReviewSchemaError,
    assign_open_question_ids,
    infer_severity,
    parse_claude_response,
    parse_codex_prose,
    parse_openai_response,
    validate_review_invariants,
)


# --- REVIEW_SCHEMA strict-safe-keyword check --------------------------------


_STRICT_SAFE_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "items",
}


def _walk_schema_keys(node):
    """Yield every dict key encountered at every nesting level of `node`."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_schema_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_schema_keys(item)


def test_review_schema_uses_only_strict_safe_keywords():
    """Round-6 finding 2: no annotation or conditional keywords in runtime schema."""
    forbidden = {"description", "comment", "$comment", "title", "examples", "allOf", "anyOf", "oneOf", "if", "then", "else", "const"}
    seen = set(_walk_schema_keys(REVIEW_SCHEMA))
    forbidden_present = seen & forbidden
    assert not forbidden_present, (
        f"REVIEW_SCHEMA contains forbidden keywords: {forbidden_present}"
    )


def test_review_schema_status_enum_has_type_string():
    """Round-6 review I2: defensive `type: string` paired with enum."""
    assert REVIEW_SCHEMA["properties"]["status"]["type"] == "string"
    severity_node = REVIEW_SCHEMA["properties"]["findings"]["items"]["properties"]["severity"]
    assert severity_node["type"] == "string"


# --- OpenAI structured-output parser -----------------------------------------


def test_parse_openai_response_no_findings(canned_openai_no_findings):
    result = parse_openai_response(canned_openai_no_findings, round_n=1, model="gpt-5.5")
    assert result.status == "NO_FINDINGS"
    assert result.findings == []
    assert result.open_questions == []
    assert result.transport == "openai"
    assert result.severity_histogram == {"high": 0, "medium": 0, "low": 0}


def test_parse_openai_response_findings_present(canned_openai_findings_present):
    result = parse_openai_response(
        canned_openai_findings_present, round_n=5, model="gpt-5.5"
    )
    assert result.status == "FINDINGS_PRESENT"
    assert len(result.findings) == 2
    assert result.findings[0].severity == "high"
    assert result.findings[0].category == "Pipeline"
    assert result.findings[1].severity == "medium"
    assert len(result.open_questions) == 1
    assert result.open_questions[0].id == "oq_r5_1"
    assert result.severity_histogram == {"high": 1, "medium": 1, "low": 0}


def test_parse_openai_response_records_usage():
    raw = json.dumps({"status": "NO_FINDINGS", "findings": [], "open_questions": []})
    result = parse_openai_response(
        raw,
        round_n=1,
        model="gpt-5.5",
        usage_input_tokens=1234,
        usage_output_tokens=567,
        cost_usd=0.025,
    )
    assert result.usage.tokens_input == 1234
    assert result.usage.tokens_output == 567
    assert result.usage.cost_usd == 0.025


def test_parse_openai_response_invalid_json_raises():
    with pytest.raises(ReviewSchemaError, match="not valid JSON"):
        parse_openai_response("{not json", round_n=1, model="gpt-5.5")


def test_parse_openai_response_missing_required_key_raises():
    bad = json.dumps({"status": "NO_FINDINGS", "findings": []})  # no open_questions
    with pytest.raises(ReviewSchemaError, match="open_questions"):
        parse_openai_response(bad, round_n=1, model="gpt-5.5")


def test_parse_openai_response_invalid_severity_raises():
    bad = json.dumps(
        {
            "status": "FINDINGS_PRESENT",
            "findings": [
                {
                    "severity": "critical",  # not in enum
                    "category": "X",
                    "where": "Y",
                    "what_can_go_wrong": "Z",
                    "concrete_fix": "W",
                }
            ],
            "open_questions": [],
        }
    )
    with pytest.raises(ReviewSchemaError, match="severity"):
        parse_openai_response(bad, round_n=1, model="gpt-5.5")


def test_parse_openai_response_non_string_field_raises():
    """I5: type-checks beyond just `severity`."""
    bad = json.dumps(
        {
            "status": "FINDINGS_PRESENT",
            "findings": [
                {
                    "severity": "high",
                    "category": None,  # not a string
                    "where": "Y",
                    "what_can_go_wrong": "Z",
                    "concrete_fix": "W",
                }
            ],
            "open_questions": [],
        }
    )
    with pytest.raises(ReviewSchemaError, match="must be a string"):
        parse_openai_response(bad, round_n=1, model="gpt-5.5")


# --- Claude CLI parser (defensive JSON extraction + shared validation) -------


def test_parse_claude_response_clean_json(canned_openai_findings_present):
    result = parse_claude_response(
        canned_openai_findings_present, round_n=2, model="claude-opus-5"
    )
    assert result.status == "FINDINGS_PRESENT"
    assert len(result.findings) == 2
    assert result.transport == "claude"
    assert result.model == "claude-opus-5"
    assert result.open_questions[0].id == "oq_r2_1"


def test_parse_claude_response_strips_code_fences(canned_openai_no_findings):
    fenced = f"```json\n{canned_openai_no_findings}\n```"
    result = parse_claude_response(fenced, round_n=1, model="claude-opus-5")
    assert result.status == "NO_FINDINGS"
    # Raw text is preserved verbatim for the fixes-md audit block.
    assert result.raw_response_text == fenced


def test_parse_claude_response_extracts_from_prose_wrapper(canned_openai_no_findings):
    wrapped = (
        "I opened every file the plan cites. Here is my verdict:\n\n"
        f"{canned_openai_no_findings}\n\n"
        "suppressed: 4 below-bar observations\n"
    )
    result = parse_claude_response(wrapped, round_n=1, model="claude-opus-5")
    assert result.status == "NO_FINDINGS"


def test_parse_claude_response_records_usage(canned_openai_no_findings):
    result = parse_claude_response(
        canned_openai_no_findings,
        round_n=1,
        model="claude-opus-5",
        usage_input_tokens=38209,
        usage_output_tokens=500,
        cost_usd=0.0384,
    )
    assert result.usage.tokens_input == 38209
    assert result.usage.tokens_output == 500
    assert result.usage.cost_usd == 0.0384


def test_parse_claude_response_no_json_raises():
    with pytest.raises(ReviewSchemaError):
        parse_claude_response("I could not comply.", round_n=1, model="claude-opus-5")


def test_parse_claude_response_schema_violation_raises():
    """Same validation body as the openai path → same ReviewSchemaError → D20 retry."""
    bad = json.dumps(
        {
            "status": "FINDINGS_PRESENT",
            "findings": [
                {
                    "severity": "blocker",  # not in enum
                    "category": "X",
                    "where": "Y",
                    "what_can_go_wrong": "Z",
                    "concrete_fix": "W",
                }
            ],
            "open_questions": [],
        }
    )
    with pytest.raises(ReviewSchemaError, match="severity"):
        parse_claude_response(bad, round_n=1, model="claude-opus-5")


def test_parse_claude_response_enforces_cross_field_invariants():
    bad = json.dumps(
        {"status": "NO_FINDINGS", "findings": [], "open_questions": ["but why?"]}
    )
    with pytest.raises(ReviewSchemaError, match="invariant"):
        parse_claude_response(bad, round_n=1, model="claude-opus-5")


def test_parse_claude_response_prefers_the_last_fenced_block(
    canned_openai_no_findings,
):
    """A repo-verifying reviewer fences its probe evidence BEFORE the verdict."""
    narrated = (
        "I grepped the repo and found this in reviewer.py:\n\n"
        "```python\n"
        'cmd = ["claude", "-p"]  # {not json}\n'
        "```\n\n"
        "Verdict:\n\n"
        f"```json\n{canned_openai_no_findings}\n```\n"
    )
    result = parse_claude_response(narrated, round_n=1, model="claude-opus-5")
    assert result.status == "NO_FINDINGS"


def test_parse_claude_response_ignores_trailing_stray_brace(canned_openai_no_findings):
    """The old rfind('}') span swallowed narration and produced invalid JSON."""
    trailing = (
        f"{canned_openai_no_findings}\n\n"
        "suppressed: 4 below-bar observations\n"
        "(one of them concerned the literal `}` in the disallowedTools string)\n"
    )
    result = parse_claude_response(trailing, round_n=1, model="claude-opus-5")
    assert result.status == "NO_FINDINGS"


def test_parse_claude_response_tolerates_braces_inside_string_values():
    """A `}` inside a finding's text must not terminate the object early."""
    payload = json.dumps(
        {
            "status": "FINDINGS_PRESENT",
            "findings": [
                {
                    "severity": "high",
                    "category": "Containment",
                    "where": "reviewer.py:120",
                    "what_can_go_wrong": 'The f-string "{tools}" }} expands wrong.',
                    "concrete_fix": "Escape the brace.",
                }
            ],
            "open_questions": [],
        }
    )
    result = parse_claude_response(
        f"Here is my verdict.\n\n{payload}\n\nDone.",
        round_n=1,
        model="claude-opus-5",
    )
    assert len(result.findings) == 1
    assert "}}" in result.findings[0].what_can_go_wrong


def test_parse_claude_response_unterminated_object_raises():
    """A truncated response is a schema error (→ D20 retry), not a crash."""
    with pytest.raises(ReviewSchemaError):
        parse_claude_response(
            '{"status": "NO_FINDINGS", "findings": [], "open_que',
            round_n=1,
            model="claude-opus-5",
        )


def test_parse_claude_response_falls_back_past_a_json_free_fence(
    canned_openai_no_findings,
):
    """Last-fence-first must skip fences with no object rather than give up."""
    narrated = (
        f"```json\n{canned_openai_no_findings}\n```\n\n"
        "```\nnpm run lint  # clean\n```\n"
    )
    result = parse_claude_response(narrated, round_n=1, model="claude-opus-5")
    assert result.status == "NO_FINDINGS"


def test_parse_claude_response_skips_an_unparseable_trailing_evidence_fence(
    canned_openai_findings_present,
):
    """A brace-bearing evidence fence AFTER the verdict must not shadow it.

    Last-fence-first alone isn't enough: the bash fence below yields a balanced
    `{…}` span that is not JSON. Accepting it (the old behaviour) lost the real
    verdict; the loop must fall back to the earlier fence that parses.
    """
    narrated = (
        "Verdict:\n\n"
        f"```json\n{canned_openai_findings_present}\n```\n\n"
        "Evidence for finding 1:\n\n"
        "```bash\n"
        "$ awk '{print $1}' cameras.json\n"
        'jq -r \'.cameras[] | {name: .name}\' cameras.json\n'
        "```\n"
    )
    result = parse_claude_response(narrated, round_n=2, model="claude-opus-5")
    assert result.status == "FINDINGS_PRESENT"
    assert len(result.findings) == 2


def test_parse_claude_response_whole_text_fallback_is_validated_too():
    """No fence parses AND the whole-text span is garbage → schema error, not a crash."""
    narrated = "```bash\nawk '{print $1}' f\n```\nand then {not: json} happened\n"
    with pytest.raises(ReviewSchemaError):
        parse_claude_response(narrated, round_n=1, model="claude-opus-5")


# --- Cross-field invariants --------------------------------------------------


def test_invariant_no_findings_with_findings_rejected():
    bad = {
        "status": "NO_FINDINGS",
        "findings": [{"severity": "low"}],
        "open_questions": [],
    }
    with pytest.raises(ReviewSchemaError, match="NO_FINDINGS with non-empty"):
        validate_review_invariants(bad)


def test_invariant_no_findings_with_open_questions_rejected():
    bad = {"status": "NO_FINDINGS", "findings": [], "open_questions": ["q?"]}
    with pytest.raises(ReviewSchemaError, match="NO_FINDINGS with non-empty"):
        validate_review_invariants(bad)


def test_invariant_findings_present_with_nothing_rejected():
    bad = {"status": "FINDINGS_PRESENT", "findings": [], "open_questions": []}
    with pytest.raises(ReviewSchemaError, match="empty findings AND empty"):
        validate_review_invariants(bad)


def test_invariant_findings_present_open_questions_only_accepted():
    """Round-3 finding 2: pure-open-question response is valid."""
    ok = {"status": "FINDINGS_PRESENT", "findings": [], "open_questions": ["q?"]}
    validate_review_invariants(ok)  # no raise


def test_invariant_findings_present_findings_only_accepted():
    ok = {
        "status": "FINDINGS_PRESENT",
        "findings": [{"severity": "high"}],
        "open_questions": [],
    }
    validate_review_invariants(ok)  # no raise


# --- Open-question ID assignment --------------------------------------------


def test_assign_open_question_ids_format():
    ids = assign_open_question_ids(["a?", "b?", "c?"], round_n=5)
    assert [oq.id for oq in ids] == ["oq_r5_1", "oq_r5_2", "oq_r5_3"]
    assert [oq.text for oq in ids] == ["a?", "b?", "c?"]


def test_assign_open_question_ids_round_stable():
    """Same inputs → same outputs (idempotent)."""
    a = assign_open_question_ids(["x?"], round_n=3)
    b = assign_open_question_ids(["x?"], round_n=3)
    assert [oq.id for oq in a] == [oq.id for oq in b]


def test_assign_open_question_ids_cross_round_unique():
    """Round-14 finding 2: re-running on round-N text never collides with round-M."""
    r1 = assign_open_question_ids(["q?"], round_n=1)
    r2 = assign_open_question_ids(["q?"], round_n=2)
    assert r1[0].id != r2[0].id
    assert r1[0].id == "oq_r1_1"
    assert r2[0].id == "oq_r2_1"


def test_assign_open_question_ids_empty():
    assert assign_open_question_ids([], round_n=1) == []


# --- Codex prose parser ------------------------------------------------------


def test_parse_codex_prose_no_findings(canned_codex_no_findings):
    result = parse_codex_prose(canned_codex_no_findings, round_n=1, model="gpt-5.5")
    assert result.status == "NO_FINDINGS"
    assert result.findings == []
    assert result.open_questions == []


def test_parse_codex_prose_preamble_then_no_findings(
    canned_codex_preamble_no_findings,
):
    """Code-review C3: preamble + sentinel still detects NO FINDINGS cleanly."""
    result = parse_codex_prose(
        canned_codex_preamble_no_findings, round_n=1, model="gpt-5.5"
    )
    assert result.status == "NO_FINDINGS"
    assert result.findings == []


def test_parse_codex_prose_no_findings_with_open_questions(
    canned_codex_no_findings_with_open_q,
):
    """Round-11 finding 2: NO FINDINGS + OPEN QUESTIONS → FINDINGS_PRESENT, no synthetics."""
    result = parse_codex_prose(
        canned_codex_no_findings_with_open_q, round_n=2, model="gpt-5.5"
    )
    assert result.status == "FINDINGS_PRESENT"
    assert result.findings == []  # no synthetic findings
    assert len(result.open_questions) == 1
    assert result.open_questions[0].id == "oq_r2_1"


def test_parse_codex_prose_findings_with_open_questions(canned_codex_findings):
    result = parse_codex_prose(canned_codex_findings, round_n=3, model="gpt-5.5")
    assert result.status == "FINDINGS_PRESENT"
    assert len(result.findings) == 2
    assert result.findings[0].severity == "high"  # "silently drops" → high
    assert result.findings[0].category == "Pipeline"
    assert result.findings[1].severity == "medium"  # "gap" → medium
    assert len(result.open_questions) == 1


def test_parse_codex_prose_extracts_concrete_fix():
    """I4: Fix:-clause extraction populates concrete_fix instead of placeholder."""
    prose = "1. [X] Something silently drops. Fix: add a guard."
    result = parse_codex_prose(prose, round_n=1, model="gpt-5.5")
    assert result.findings[0].concrete_fix == "add a guard."
    assert "silently drops" in result.findings[0].what_can_go_wrong
    assert "Fix:" not in result.findings[0].what_can_go_wrong


def test_parse_codex_prose_falls_back_to_empty_fix_when_no_clause():
    prose = "1. [X] Something is broken without a fix mentioned."
    result = parse_codex_prose(prose, round_n=1, model="gpt-5.5")
    assert result.findings[0].concrete_fix == ""


def test_parse_codex_prose_no_findings_sentinel_inside_finding_body_does_not_trip():
    """`NO FINDINGS` mentioned inside a numbered finding must not coerce to clean."""
    prose = "1. [X] The plan silently breaks NO FINDINGS support under load."
    result = parse_codex_prose(prose, round_n=1, model="gpt-5.5")
    assert result.status == "FINDINGS_PRESENT"
    assert len(result.findings) == 1


# --- Severity inference ------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("This silently drops X", "high"),
        ("Backwards-compat is broken", "high"),
        ("Security risk in module", "high"),
        ("Could leak the API key", "high"),
        ("Verification gap in the spec", "medium"),
        ("ambiguous wording", "medium"),
        ("Missing test for the fallback", "medium"),
        ("edge case unhandled", "medium"),
        ("Could be clearer", "low"),
        ("Just a typo", "low"),
    ],
)
def test_infer_severity_keywords(text, expected):
    assert infer_severity(text) == expected


def test_no_findings_sentinel_constant():
    """Public constant available for reviewers/builders to share without duplication."""
    assert NO_FINDINGS_SENTINEL == "NO FINDINGS"


# --- Severity histogram ------------------------------------------------------


def test_severity_histogram_property():
    raw = json.dumps(
        {
            "status": "FINDINGS_PRESENT",
            "findings": [
                {"severity": "high", "category": "A", "where": "B", "what_can_go_wrong": "C", "concrete_fix": "D"},
                {"severity": "high", "category": "A", "where": "B", "what_can_go_wrong": "C", "concrete_fix": "D"},
                {"severity": "medium", "category": "A", "where": "B", "what_can_go_wrong": "C", "concrete_fix": "D"},
                {"severity": "low", "category": "A", "where": "B", "what_can_go_wrong": "C", "concrete_fix": "D"},
            ],
            "open_questions": [],
        }
    )
    result = parse_openai_response(raw, round_n=1, model="gpt-5.5")
    assert result.severity_histogram == {"high": 2, "medium": 1, "low": 1}
