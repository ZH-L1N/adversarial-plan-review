"""Shared fixtures for the adversarial-plan-review test suite.

Tests run with the repo root as cwd. `scripts/` is added to `sys.path` so
modules can be imported by their bare name (matching how SKILL.md's inline
Python snippets do the import).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# --- Helpers -----------------------------------------------------------------


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Synthetic plan ---------------------------------------------------------


SYNTHETIC_PLAN_V0 = """\
# Test plan v0

## §1 Goal

Stress-test the v2 review loop.

## §2 Design

The skill must do something useful.
"""


SYNTHETIC_PLAN_V1 = """\
# Test plan v0

## §1 Goal

Stress-test the v2 review loop with two specific failure modes covered.

## §2 Design

The skill must do something useful, including handling the silent-drop edge case.
"""


@pytest.fixture
def synthetic_plan_v0() -> str:
    return SYNTHETIC_PLAN_V0


@pytest.fixture
def synthetic_plan_v1() -> str:
    """Post-round-1 edits applied — used to compute round-2 diffs."""
    return SYNTHETIC_PLAN_V1


# --- Canned reviewer responses ----------------------------------------------


CANNED_OPENAI_NO_FINDINGS = json.dumps(
    {"status": "NO_FINDINGS", "findings": [], "open_questions": []}
)


CANNED_OPENAI_FINDINGS_PRESENT = json.dumps(
    {
        "status": "FINDINGS_PRESENT",
        "findings": [
            {
                "severity": "high",
                "category": "Pipeline",
                "where": "§2 Design",
                "what_can_go_wrong": "The plan silently drops X under condition Y.",
                "concrete_fix": "Add a guard before drop and surface a warning.",
            },
            {
                "severity": "medium",
                "category": "Verification",
                "where": "§2 Design",
                "what_can_go_wrong": "No test covers the fallback path.",
                "concrete_fix": "Add test_fallback_path() asserting expected behavior.",
            },
        ],
        "open_questions": [
            "Should the warning be a structured object or a string?",
        ],
    }
)


CANNED_CODEX_PROSE_FINDINGS = """\
1. [Pipeline] The plan silently drops X under condition Y. Fix: Add a guard before drop.
2. [Verification] No test covers the fallback path; this is a gap. Fix: add test_fallback_path().

OPEN QUESTIONS:
- Should the warning be structured or a string?
"""


CANNED_CODEX_PROSE_NO_FINDINGS = "NO FINDINGS\n"


CANNED_CODEX_PROSE_PREAMBLE_NO_FINDINGS = """\
Reviewing the plan now...
Looking carefully at every section.

NO FINDINGS
"""


CANNED_CODEX_PROSE_NO_FINDINGS_WITH_OPEN_Q = """\
NO FINDINGS

OPEN QUESTIONS:
- One thing remains unclear about the cost cap behavior.
"""


@pytest.fixture
def canned_openai_no_findings() -> str:
    return CANNED_OPENAI_NO_FINDINGS


@pytest.fixture
def canned_openai_findings_present() -> str:
    return CANNED_OPENAI_FINDINGS_PRESENT


@pytest.fixture
def canned_codex_findings() -> str:
    return CANNED_CODEX_PROSE_FINDINGS


@pytest.fixture
def canned_codex_no_findings() -> str:
    return CANNED_CODEX_PROSE_NO_FINDINGS


@pytest.fixture
def canned_codex_preamble_no_findings() -> str:
    return CANNED_CODEX_PROSE_PREAMBLE_NO_FINDINGS


@pytest.fixture
def canned_codex_no_findings_with_open_q() -> str:
    return CANNED_CODEX_PROSE_NO_FINDINGS_WITH_OPEN_Q


# --- Sample sidecars --------------------------------------------------------


def make_sidecar(
    *,
    round_n: int,
    plan_content: str,
    baseline_plan_content: str | None = None,
    transport: str = "openai",
    model: str = "gpt-5.5",
    findings: list[dict] | None = None,
    open_questions: list[dict] | None = None,
    decisions: list[dict] | None = None,
    plan_edits: list[dict] | None = None,
    cumulative_cost_usd: float = 0.0,
    cost_usd: float = 0.0,
    deferrals_at_exit: list[dict] | None = None,
    restart_metadata: dict | None = None,
) -> dict:
    """Construct a schema-valid sidecar dict for tests."""
    plan_sha = sha256_hex(plan_content)
    baseline_sha = sha256_hex(baseline_plan_content) if baseline_plan_content else None
    is_clean = not findings and not open_questions
    histogram = {
        "high": sum(1 for f in (findings or []) if f["severity"] == "high"),
        "medium": sum(1 for f in (findings or []) if f["severity"] == "medium"),
        "low": sum(1 for f in (findings or []) if f["severity"] == "low"),
    }
    return {
        "schema_version": "2.0.0",
        "round": round_n,
        "started_at": f"2026-05-03T00:0{round_n}:00Z",
        "completed_at": f"2026-05-03T00:0{round_n}:30Z",
        "transport": transport,
        "model": model,
        "raw_response_text": "{}" if transport == "openai" else "NO FINDINGS",
        "plan_content_sha256": plan_sha,
        "plan_content": plan_content,
        "baseline_plan_content_sha256": baseline_sha,
        "baseline_plan_content": baseline_plan_content,
        "restart_metadata": restart_metadata,
        "deferrals_at_exit": deferrals_at_exit,
        "reviewer_response": {
            "status": "NO_FINDINGS" if is_clean else "FINDINGS_PRESENT",
            "findings": findings or [],
            "open_questions": open_questions or [],
        },
        "planner_decisions": decisions or [],
        "plan_edits_applied": plan_edits or [],
        "stats": {
            "tokens_input": 1000,
            "tokens_output": 500,
            "cost_usd": cost_usd,
            "cumulative_cost_usd": cumulative_cost_usd,
            "duration_seconds": 30.0,
            "plan_size_chars": len(plan_content),
            "plan_size_delta": 0,
            "severity_histogram": histogram,
        },
    }


@pytest.fixture
def make_sidecar_factory():
    return make_sidecar


# --- Working directory isolation --------------------------------------------


@pytest.fixture
def isolated_repo(tmp_path, monkeypatch):
    """Run the test in a temp directory mimicking the repo layout.

    Creates `plans/`, `plans/fixs/`, `.scratch/` so module-level path
    constants like `Path("plans/fixs")` resolve under tmp_path.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "fixs").mkdir()
    (tmp_path / ".scratch").mkdir()
    return tmp_path


# --- Hermetic env -----------------------------------------------------------


@pytest.fixture
def empty_env():
    """An env dict with PATH cleared — used to test hermetic transport detection."""
    return {"PATH": "", "PATHEXT": ""}
