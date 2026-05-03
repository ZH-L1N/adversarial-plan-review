"""Tests for scripts/sidecar_schema.json — JSON Schema contract."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sidecar_schema.json"


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(schema, sidecar):
    jsonschema.validate(sidecar, schema)


# --- Known-good sidecars validate -------------------------------------------


def test_valid_round_1_with_baseline(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
    )
    _validate(schema, sidecar)  # no raise


def test_valid_round_n_without_baseline(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=5, plan_content="# x", baseline_plan_content=None,
    )
    _validate(schema, sidecar)


def test_valid_round_with_findings_and_open_questions(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=2,
        plan_content="# x",
        findings=[
            {
                "id": "f_r2_1",
                "severity": "high",
                "category": "Pipeline",
                "where": "§2",
                "what_can_go_wrong": "X",
                "concrete_fix": "Y",
            }
        ],
        open_questions=[{"id": "oq_r2_1", "text": "Should X?"}],
    )
    _validate(schema, sidecar)


# --- Round-1 baseline rule (§5.7.3b) ----------------------------------------


def test_round_1_with_null_baseline_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content=None,
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_round_n_with_baseline_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x", baseline_plan_content="# leaked",
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_round_1_baseline_sha256_required(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(round_n=1, plan_content="# x", baseline_plan_content="# y")
    sidecar["baseline_plan_content_sha256"] = None
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


# --- deferrals_at_exit medium-target rule (§5.7.3aa) ------------------------


def test_deferral_medium_with_null_target_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x",
        deferrals_at_exit=[
            {
                "item_id": "f_r2_1",
                "severity": "medium",
                "reason": "deferred",
                "target_version": None,  # forbidden for medium
            }
        ],
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_deferral_medium_with_target_accepted(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x",
        deferrals_at_exit=[
            {
                "item_id": "f_r2_1",
                "severity": "medium",
                "reason": "deferred to v2.1",
                "target_version": "v2.1",
            }
        ],
    )
    _validate(schema, sidecar)


def test_deferral_high_with_null_target_accepted(schema, make_sidecar_factory):
    """High doesn't require target_version (only medium does)."""
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x",
        deferrals_at_exit=[
            {
                "item_id": "f_r2_1",
                "severity": "high",
                "reason": "accepted at exit",
                "target_version": None,
            }
        ],
    )
    _validate(schema, sidecar)


def test_deferral_open_question_severity_accepted(schema, make_sidecar_factory):
    """severity='open_question' is a valid pseudo-severity in deferrals."""
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x",
        deferrals_at_exit=[
            {
                "item_id": "oq_r2_1",
                "severity": "open_question",
                "reason": "out of scope",
                "target_version": None,
            }
        ],
    )
    _validate(schema, sidecar)


def test_deferral_accept_all_risk_sentinel(schema, make_sidecar_factory):
    """Round-14 finding 1: accept_all_risk uses 'accepted-at-exit' sentinel for all items."""
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x",
        deferrals_at_exit=[
            {
                "item_id": "f_r2_1",
                "severity": "medium",
                "reason": "accepted at exit",
                "target_version": "accepted-at-exit",
            }
        ],
    )
    _validate(schema, sidecar)


def test_deferral_empty_reason_rejected(schema, make_sidecar_factory):
    """reason must be non-empty (minLength: 1)."""
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x",
        deferrals_at_exit=[
            {
                "item_id": "f_r2_1",
                "severity": "high",
                "reason": "",
                "target_version": None,
            }
        ],
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


# --- Open-question ID format ------------------------------------------------


def test_open_question_id_must_match_oq_pattern(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=2, plan_content="# x",
        open_questions=[{"id": "bad_id", "text": "Q?"}],
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_open_question_id_oq_r5_1_accepted(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=5, plan_content="# x",
        open_questions=[{"id": "oq_r5_1", "text": "Q?"}],
    )
    _validate(schema, sidecar)


def test_open_question_bare_string_rejected(schema, make_sidecar_factory):
    """Round-15 finding 1: sidecar must store object form, not bare strings."""
    sidecar = make_sidecar_factory(round_n=2, plan_content="# x")
    sidecar["reviewer_response"]["open_questions"] = ["bare string"]
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


# --- Required top-level fields ---------------------------------------------


def test_missing_schema_version_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
    )
    del sidecar["schema_version"]
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_invalid_schema_version_format_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(round_n=1, plan_content="# x", baseline_plan_content="# y")
    sidecar["schema_version"] = "v2"  # missing semver dots
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_invalid_transport_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
    )
    sidecar["transport"] = "anthropic"
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_invalid_severity_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
        findings=[
            {
                "id": "f_r1_1",
                "severity": "critical",  # not in enum
                "category": "X", "where": "Y", "what_can_go_wrong": "Z",
                "concrete_fix": "W",
            }
        ],
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_sha256_format_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# x", baseline_plan_content="# y",
    )
    sidecar["plan_content_sha256"] = "not-hex"
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


def test_negative_tokens_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(round_n=1, plan_content="# x", baseline_plan_content="# y")
    sidecar["stats"]["tokens_input"] = -1
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)


# --- restart_metadata schema -----------------------------------------------


def test_restart_metadata_populated_form(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# fresh", baseline_plan_content="# fresh",
        restart_metadata={
            "timestamp": "2026-05-03T00:00:00Z",
            "deleted_files": ["plans/fixs/old-round-1.json"],
            "user_decision": "user chose start over",
        },
    )
    _validate(schema, sidecar)


def test_restart_metadata_with_previous_run_summary(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# fresh", baseline_plan_content="# fresh",
        restart_metadata={
            "timestamp": "2026-05-03T00:00:00Z",
            "deleted_files": [],
            "user_decision": "u",
            "previous_run_summary": {"last_round": 5, "last_status": "ceiling_hit"},
        },
    )
    _validate(schema, sidecar)


def test_restart_metadata_missing_required_rejected(schema, make_sidecar_factory):
    sidecar = make_sidecar_factory(
        round_n=1, plan_content="# fresh", baseline_plan_content="# fresh",
        restart_metadata={"timestamp": "2026-05-03T00:00:00Z"},  # missing deleted_files, user_decision
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, sidecar)
