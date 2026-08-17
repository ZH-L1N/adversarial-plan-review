"""Loop state machine for the v2 adversarial plan review (Phase 4).

Single module owning every piece of cross-round state:

- **Snapshot management** (§5.3.1): namespaced `.scratch/` snapshots with
  hash validation, sidecar-based recovery, git fallback as last resort.
- **Sidecar atomic writes** (§5.7.3): write `.json.tmp` then rename, then
  render the markdown fixes-md from the just-written JSON.
- **Exit gates** (§5.4): Approved / Resolved / Resolved-with-deferrals /
  Planner-locked / Ceiling hit / Cost-capped, in priority order.
- **Plan-bloat detection** (§5.5): trigger when plan grew >20% over the
  last 3 rounds with no new high findings.
- **Resume support** (§5.9): walk prior sidecars, validate schema + hash,
  regenerate fixes-md.

The actual `AskUserQuestion` interactions live in SKILL.md — this module
exposes the state machine as pure functions and dataclasses so the skill
side can drive it with whatever UI primitives are available (Claude Code
AskUserQuestion, stdin prompts in CI smoke tests, etc.).

Schema validation is best-effort: if the `jsonschema` package is installed
we use it; otherwise we fall back to a structural sanity check that covers
the load-bearing rules but skips the `if/then` / `allOf` conditionals.
Documented as a known degradation; the test suite (Phase 4 verification)
runs against `jsonschema` to lock the full contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from parse_review import Finding, OpenQuestion, ReviewResult
import render_markdown


SCHEMA_VERSION = "2.0.0"

# Hard ceiling on review rounds. Single source of truth: SKILL.md, README, and
# .env.example all describe this value rather than restating a literal, so the
# documented default cannot drift from the code the way v1's "10" did.
DEFAULT_MAX_ROUNDS = 5


# --- Enums and dataclasses ---------------------------------------------------


class ExitReason(str, Enum):
    APPROVED = "approved"
    RESOLVED = "resolved"
    RESOLVED_WITH_DEFERRALS = "resolved_with_deferrals"
    PLANNER_LOCKED = "planner_locked"
    CEILING_HIT = "ceiling_hit"
    COST_CAPPED = "cost_capped"
    # Sentinel: no exit fires this round; the loop continues to N+1.
    # `evaluate_exit` returns this when no other reason applies, so callers
    # can disambiguate "approved/resolved + no soft-block needed" from "no
    # exit yet" without re-inspecting open_* lists. (Code-review C2.)
    NO_EXIT = "no_exit"


@dataclass(frozen=True)
class PlannerDecision:
    """One planner verdict on a finding or open question."""

    item_id: str
    decision: str  # one of "accept", "reject", "uncertain", "accept_via_user", "reject_via_user"
    rationale: str
    stated_edit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "decision": self.decision,
            "rationale": self.rationale,
            "stated_edit": self.stated_edit,
        }


@dataclass(frozen=True)
class PlanEdit:
    section: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return {"section": self.section, "summary": self.summary}


@dataclass(frozen=True)
class Deferral:
    """Persisted form of a soft-block deferral. See §5.7.3aa."""

    item_id: str
    severity: str  # "high" | "medium" | "low" | "open_question"
    reason: str
    target_version: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "severity": self.severity,
            "reason": self.reason,
            "target_version": self.target_version,
        }


@dataclass
class RoundState:
    """Inputs and outputs for one round, before sidecar serialization.

    The state machine populates the inputs (`reviewer_response`, `decisions`,
    `plan_edits`, `started_at`, `completed_at`) over the course of the round,
    then `build_sidecar()` serializes everything into the JSON sidecar shape.
    """

    round_n: int
    slug: str
    version: str
    transport: str
    model: str
    started_at: str
    completed_at: str = ""
    reviewer_response: ReviewResult | None = None
    decisions: list[PlannerDecision] = field(default_factory=list)
    plan_edits: list[PlanEdit] = field(default_factory=list)
    deferrals_at_exit: list[Deferral] | None = None
    restart_metadata: dict[str, Any] | None = None
    plan_content_at_end: str = ""
    baseline_plan_content: str | None = None
    cumulative_cost_usd: float = 0.0
    duration_seconds: float = 0.0
    plan_size_delta: int = 0


# --- Snapshot machinery (§5.3.1) ---------------------------------------------


def _snapshot_dir() -> Path:
    d = Path(".scratch")
    d.mkdir(exist_ok=True)
    return d


def _snapshot_path(slug: str, version: str, snapshot_index: int) -> Path:
    return _snapshot_dir() / f"{version}-{slug}-plan-snapshot-r{snapshot_index}.md"


def _sidecar_path(slug: str, version: str, round_n: int) -> Path:
    return Path("plans/fixs") / f"{version}-{slug}-round-{round_n}.json"


def take_initial_snapshot(plan_path: Path, *, slug: str, version: str) -> None:
    """Write the BASELINE snapshot (r1) before round 1 begins.

    Snapshot index semantic: r{N} = plan state at START of round N. So r1 is
    the pre-loop baseline; round 2 reads r1 and diffs against current plan
    (which IS r2 at that point). See v2-plan §5.3.1 round-8 finding 1.
    """
    target = _snapshot_path(slug, version, 1)
    target.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")


def compute_round_diff(
    plan_path: Path,
    *,
    round_n: int,
    slug: str,
    version: str,
) -> tuple[str, bool]:
    """Diff between r{N-1} snapshot and current plan.

    Returns `(diff_text, recovered_from_git)`. `recovered_from_git` is True
    only when both the snapshot file and the sidecar `plan_content` recovery
    failed — the prompt builder uses the flag to add the cumulative-against-
    HEAD warning banner (§5.3.2).

    Priority chain (per §5.3.1 round-7 finding 2):
        1. Namespaced snapshot present AND hash-matches sidecar → use it
        2. Recover from sidecar's plan_content (or baseline_plan_content for r1)
        3. Last resort: git fallback against the most recent plan in HEAD
    """
    if round_n < 2:
        raise ValueError(f"compute_round_diff requires round_n >= 2 (got {round_n})")

    prior_snapshot_path = _snapshot_path(slug, version, round_n - 1)
    current_snapshot_path = _snapshot_path(slug, version, round_n)

    prior_snapshot_valid = False
    snapshot_existed = prior_snapshot_path.exists()
    if snapshot_existed:
        expected_hash = _read_sidecar_plan_hash(round_n - 1, slug, version)
        actual_hash = hashlib.sha256(prior_snapshot_path.read_bytes()).hexdigest()
        prior_snapshot_valid = bool(expected_hash) and actual_hash == expected_hash
        if not prior_snapshot_valid and expected_hash:
            # Code-review I7: tampered/stale snapshot → warn the operator
            # before silently routing to recovery. Logged to stderr so a
            # CI run can pick it up; doesn't fail the round.
            import sys

            print(
                f"WARNING: snapshot {prior_snapshot_path.name} hash mismatch "
                f"(expected {expected_hash[:8]}…, got {actual_hash[:8]}…). "
                "Routing to sidecar recovery; snapshot may have been hand-edited.",
                file=sys.stderr,
            )

    if not prior_snapshot_valid:
        recovered = _recover_snapshot_from_sidecar(round_n - 1, slug, version)
        if recovered is not None:
            prior_snapshot_path.write_text(recovered, encoding="utf-8")
            prior_snapshot_valid = True
        else:
            return _recover_diff_from_git(plan_path), True

    diff = _git_diff_no_index(prior_snapshot_path, plan_path)
    current_snapshot_path.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
    return diff, False


def _git_diff_no_index(left: Path, right: Path) -> str:
    """`git diff --no-index` — exit code 1 means files differ (the common case)."""
    result = subprocess.run(
        ["git", "diff", "--no-index", "--no-color", str(left), str(right)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"git diff --no-index failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _recover_diff_from_git(plan_path: Path) -> str:
    """Last-resort: cumulative diff between the plan in HEAD and current working tree.

    Documented in §5.3.2 — this is degraded compared to round-by-round
    snapshot diffs, and the prompt builder adds a banner so the reviewer
    knows to apply verify-then-attack against the cumulative delta.
    """
    result = subprocess.run(
        ["git", "diff", "--no-color", "HEAD", "--", str(plan_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode not in (0, 1):
        # If git itself fails (no repo, no HEAD, etc.), return an empty diff
        # rather than crash — the reviewer prompt will still have the full
        # plan + the banner explaining the degraded state.
        return ""
    return result.stdout


def _recover_snapshot_from_sidecar(
    snapshot_index: int, slug: str, version: str
) -> str | None:
    """Recover snapshot at index N from sidecar `plan_content` (or baseline for N=1).

    For snapshot_index=1: read round-1 sidecar's `baseline_plan_content` +
    `baseline_plan_content_sha256`. For snapshot_index>=2: read sidecar
    (snapshot_index - 1)'s `plan_content` + `plan_content_sha256`. Hash
    mismatch returns None and routes to git fallback.
    """
    if snapshot_index == 1:
        sidecar = _sidecar_path(slug, version, 1)
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        plan_content = data.get("baseline_plan_content")
        expected_hash = data.get("baseline_plan_content_sha256")
    elif snapshot_index >= 2:
        source_round = snapshot_index - 1
        sidecar = _sidecar_path(slug, version, source_round)
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        plan_content = data.get("plan_content")
        expected_hash = data.get("plan_content_sha256")
    else:
        return None

    if not plan_content or not expected_hash:
        return None
    actual_hash = hashlib.sha256(plan_content.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        return None
    return plan_content


def _read_sidecar_plan_hash(snapshot_index: int, slug: str, version: str) -> str | None:
    """Hash of the plan content represented by snapshot index N."""
    if snapshot_index == 1:
        sidecar = _sidecar_path(slug, version, 1)
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data.get("baseline_plan_content_sha256")
    if snapshot_index >= 2:
        sidecar = _sidecar_path(slug, version, snapshot_index - 1)
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data.get("plan_content_sha256")
    return None


def cleanup_snapshots(slug: str, version: str) -> int:
    """Remove all snapshots for this loop on exit. Returns count deleted."""
    snapshot_dir = Path(".scratch")
    if not snapshot_dir.is_dir():
        return 0
    pattern = f"{version}-{slug}-plan-snapshot-r*.md"
    paths = list(snapshot_dir.glob(pattern))
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass
    return len(paths)


# --- Sidecar persistence (§5.7.3) --------------------------------------------


def build_sidecar(state: RoundState, *, raw_response_text: str) -> dict[str, Any]:
    """Materialize the JSON sidecar from a populated RoundState."""
    if state.reviewer_response is None:
        raise ValueError("RoundState.reviewer_response must be set before build_sidecar")

    plan_bytes = state.plan_content_at_end.encode("utf-8")
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()

    if state.round_n == 1:
        if state.baseline_plan_content is None:
            raise ValueError(
                "Round-1 sidecar requires baseline_plan_content captured at "
                "_take_initial_snapshot time (round-10 finding 1)."
            )
        baseline_bytes = state.baseline_plan_content.encode("utf-8")
        baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
        baseline_text: str | None = state.baseline_plan_content
        baseline_sha_field: str | None = baseline_sha
    else:
        baseline_text = None
        baseline_sha_field = None

    findings_serialized = []
    for i, finding in enumerate(state.reviewer_response.findings, start=1):
        findings_serialized.append(
            {
                "id": f"f_r{state.round_n}_{i}",
                "severity": finding.severity,
                "category": finding.category,
                "where": finding.where,
                "what_can_go_wrong": finding.what_can_go_wrong,
                "concrete_fix": finding.concrete_fix,
            }
        )

    open_questions_serialized = [oq.to_dict() for oq in state.reviewer_response.open_questions]
    histogram = state.reviewer_response.severity_histogram

    return {
        "schema_version": SCHEMA_VERSION,
        "round": state.round_n,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "transport": state.transport,
        "model": state.model,
        "raw_response_text": raw_response_text,
        "plan_content_sha256": plan_sha,
        "plan_content": state.plan_content_at_end,
        "baseline_plan_content_sha256": baseline_sha_field,
        "baseline_plan_content": baseline_text,
        "restart_metadata": state.restart_metadata,
        "deferrals_at_exit": (
            [d.to_dict() for d in state.deferrals_at_exit]
            if state.deferrals_at_exit is not None
            else None
        ),
        "reviewer_response": {
            "status": state.reviewer_response.status,
            "findings": findings_serialized,
            "open_questions": open_questions_serialized,
        },
        "planner_decisions": [d.to_dict() for d in state.decisions],
        "plan_edits_applied": [e.to_dict() for e in state.plan_edits],
        "stats": {
            "tokens_input": state.reviewer_response.usage.tokens_input,
            "tokens_output": state.reviewer_response.usage.tokens_output,
            "cost_usd": state.reviewer_response.usage.cost_usd,
            "cumulative_cost_usd": state.cumulative_cost_usd,
            "duration_seconds": state.duration_seconds,
            "plan_size_chars": len(state.plan_content_at_end),
            "plan_size_delta": state.plan_size_delta,
            "severity_histogram": histogram,
        },
    }


def write_sidecar_atomic(sidecar: dict[str, Any], *, slug: str, version: str) -> Path:
    """Atomic write: serialize, write `.tmp`, then rename to final path."""
    target = _sidecar_path(slug, version, sidecar["round"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    payload = json.dumps(sidecar, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def regenerate_fixes_md(
    *,
    slug: str,
    version: str,
    output_path: Path | None = None,
) -> Path:
    """Re-render the fixes-md from all available sidecars in order.

    The §5.7.5 drift policy: hand-edits to fixes-md are silently overwritten
    on the next render. The JSON sidecar is the source of truth; the markdown
    is a derived view.
    """
    sidecars = load_sidecars(slug=slug, version=version)
    if not sidecars:
        raise ValueError("No sidecars to render — nothing to do.")
    first = sidecars[0]
    header = render_markdown.render_header(
        slug=slug,
        version=version,
        started_at=first["started_at"],
        transport=first["transport"],
        model=first["model"],
    )
    body = render_markdown.render_full_fixes_md(header, sidecars)
    target = output_path or Path("plans/fixs") / f"{version}-{slug}-fixes.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


# --- Sidecar loading and validation ------------------------------------------


def load_sidecars(*, slug: str, version: str) -> list[dict[str, Any]]:
    """Load round 1..N sidecars in numeric order.

    Refuses to return a non-contiguous range. If sidecars 1, 2, 3, 5 exist
    (gap at 4), raises `ResumeIntegrityError` so the caller can surface to
    the user (§5.9 step "If a sidecar is missing in the middle, refuse to
    resume").
    """
    fixs_dir = Path("plans/fixs")
    if not fixs_dir.is_dir():
        return []
    pattern = f"{version}-{slug}-round-*.json"
    matches: dict[int, Path] = {}
    for path in fixs_dir.glob(pattern):
        n = _round_number_from_path(path, slug, version)
        if n is not None:
            matches[n] = path
    if not matches:
        return []
    expected_range = range(1, max(matches.keys()) + 1)
    missing = [n for n in expected_range if n not in matches]
    if missing:
        raise ResumeIntegrityError(
            f"Non-contiguous sidecar range: missing round(s) {missing} "
            f"in {fixs_dir}/{version}-{slug}-round-*.json"
        )
    out: list[dict[str, Any]] = []
    for n in expected_range:
        out.append(json.loads(matches[n].read_text(encoding="utf-8")))
    return out


def _round_number_from_path(path: Path, slug: str, version: str) -> int | None:
    prefix = f"{version}-{slug}-round-"
    suffix = ".json"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    try:
        return int(name[len(prefix) : -len(suffix)])
    except ValueError:
        return None


class ResumeIntegrityError(RuntimeError):
    """Sidecar audit trail is corrupt; resume cannot proceed safely."""


def validate_sidecar(sidecar: dict[str, Any]) -> None:
    """Validate sidecar against `sidecar_schema.json`.

    Uses `jsonschema` if installed; otherwise falls back to a structural
    sanity check that covers the load-bearing rules but skips the if/then
    conditionals. Raises `SidecarSchemaError` on violation.
    """
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        _structural_sanity_check(sidecar)
        return

    schema_path = Path(__file__).resolve().parent / "sidecar_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(sidecar, schema)
    except jsonschema.ValidationError as exc:
        raise SidecarSchemaError(str(exc)) from exc

    # JSON Schema can't express "this hash matches that content" — verify
    # SHA-256 round-trip post-schema. Both plan_content and (round-1 only)
    # baseline_plan_content must hash to their declared *_sha256 values.
    actual_sha = hashlib.sha256(sidecar["plan_content"].encode("utf-8")).hexdigest()
    if actual_sha != sidecar["plan_content_sha256"]:
        raise SidecarSchemaError(
            f"plan_content_sha256 mismatch: declared {sidecar['plan_content_sha256'][:8]}…, "
            f"computed {actual_sha[:8]}…"
        )
    if sidecar["round"] == 1 and sidecar.get("baseline_plan_content"):
        actual_baseline = hashlib.sha256(
            sidecar["baseline_plan_content"].encode("utf-8")
        ).hexdigest()
        if actual_baseline != sidecar["baseline_plan_content_sha256"]:
            raise SidecarSchemaError(
                f"baseline_plan_content_sha256 mismatch: "
                f"declared {sidecar['baseline_plan_content_sha256'][:8]}…, "
                f"computed {actual_baseline[:8]}…"
            )


class SidecarSchemaError(RuntimeError):
    """Sidecar JSON failed schema validation."""


def _structural_sanity_check(sidecar: dict[str, Any]) -> None:
    """Lightweight schema check used when `jsonschema` isn't installed.

    Covers the structural fields (required keys, top-level types) but does
    not enforce the `allOf`/`if/then` conditionals (round-1 baseline rule,
    medium-target rule). Documented degradation; full enforcement requires
    `pip install jsonschema`.
    """
    required_top_level = (
        "schema_version",
        "round",
        "transport",
        "model",
        "raw_response_text",
        "plan_content_sha256",
        "plan_content",
        "reviewer_response",
        "planner_decisions",
        "plan_edits_applied",
        "stats",
    )
    for key in required_top_level:
        if key not in sidecar:
            raise SidecarSchemaError(f"sidecar missing required key '{key}'")

    if sidecar["schema_version"] != SCHEMA_VERSION:
        raise SidecarSchemaError(
            f"unsupported schema_version '{sidecar['schema_version']}' "
            f"(expected '{SCHEMA_VERSION}')"
        )

    sha = sidecar["plan_content_sha256"]
    if not isinstance(sha, str) or len(sha) != 64:
        raise SidecarSchemaError("plan_content_sha256 must be a 64-char hex string")

    actual_sha = hashlib.sha256(sidecar["plan_content"].encode("utf-8")).hexdigest()
    if actual_sha != sha:
        raise SidecarSchemaError(
            "plan_content_sha256 does not match SHA-256 of plan_content"
        )

    if sidecar["round"] == 1:
        if sidecar.get("baseline_plan_content") is None:
            raise SidecarSchemaError(
                "round-1 sidecar must carry non-null baseline_plan_content"
            )
        baseline_sha = sidecar.get("baseline_plan_content_sha256")
        if not baseline_sha:
            raise SidecarSchemaError(
                "round-1 sidecar must carry baseline_plan_content_sha256"
            )
        actual_baseline = hashlib.sha256(
            sidecar["baseline_plan_content"].encode("utf-8")
        ).hexdigest()
        if actual_baseline != baseline_sha:
            raise SidecarSchemaError(
                "baseline_plan_content_sha256 does not match SHA of baseline_plan_content"
            )


# --- Exit gates (§5.4) -------------------------------------------------------


@dataclass(frozen=True)
class ExitDecision:
    reason: ExitReason
    open_highs: list[str]  # finding IDs
    open_mediums: list[str]
    open_questions: list[str]
    needs_soft_block: bool  # True if exit requires §5.4.1 soft-block UX


def evaluate_exit(
    state: RoundState,
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    cumulative_cost_usd: float,
    cost_cap_usd: float,
) -> ExitDecision:
    """Compute the loop's exit verdict for this round.

    Priority order matches §5.4. Returns `needs_soft_block=True` when the
    loop would exit (ceiling, planner-locked, cost-capped) but unresolved
    items remain — caller (SKILL.md) drives the AskUserQuestion flow.
    """
    if state.reviewer_response is None:
        raise ValueError("evaluate_exit requires reviewer_response set")

    open_highs, open_mediums, open_questions = _open_items(state)

    # 1. Approved: reviewer returned NO_FINDINGS (schema guarantees no open
    #    questions either, so no follow-up needed).
    if state.reviewer_response.status == "NO_FINDINGS":
        return ExitDecision(
            reason=ExitReason.APPROVED,
            open_highs=[],
            open_mediums=[],
            open_questions=[],
            needs_soft_block=False,
        )

    # 2. Planner-locked: every finding this round was rejected. Code-review
    #    C1 — must check this BEFORE the RESOLVED branch, because rejected
    #    findings are "decided" and would otherwise make `_open_items`
    #    return empty, masking planner-lock as a clean Resolved exit.
    if state.decisions and _all_rejected(state.decisions):
        return ExitDecision(
            reason=ExitReason.PLANNER_LOCKED,
            open_highs=open_highs,
            open_mediums=open_mediums,
            open_questions=open_questions,
            needs_soft_block=bool(open_highs or open_mediums or open_questions),
        )

    # 3. Resolved: zero unresolved highs + zero open questions + every
    #    medium has an Accept/Reject decision (no Uncertain remaining) +
    #    no accepted findings this round. The accept guard is load-bearing:
    #    "every finding decided" includes accepts, but accepts produce plan
    #    edits that no reviewer has seen yet. Exiting here would skip the
    #    validation round that confirms the edits actually closed the
    #    finding (or introduced new problems). The only safe same-round
    #    clean exits are APPROVED (NO_FINDINGS this round) and
    #    PLANNER_LOCKED (all rejected, no edits made). When accepts are
    #    present we fall through to NO_EXIT and let round N+1's reviewer
    #    deliver the verdict on the edited plan.
    if not open_highs and not open_mediums and not open_questions:
        has_accepts = any(
            d.decision in ("accept", "accept_via_user") for d in state.decisions
        )
        if not has_accepts:
            return ExitDecision(
                reason=ExitReason.RESOLVED,
                open_highs=[],
                open_mediums=[],
                open_questions=[],
                needs_soft_block=False,
            )

    has_open = bool(open_highs or open_mediums or open_questions)

    # 4. Cost-cap exit (forces user input via soft-block when items remain)
    if cumulative_cost_usd >= cost_cap_usd:
        return ExitDecision(
            reason=ExitReason.COST_CAPPED,
            open_highs=open_highs,
            open_mediums=open_mediums,
            open_questions=open_questions,
            needs_soft_block=has_open,
        )

    # 5. Ceiling
    if state.round_n >= max_rounds:
        return ExitDecision(
            reason=ExitReason.CEILING_HIT,
            open_highs=open_highs,
            open_mediums=open_mediums,
            open_questions=open_questions,
            needs_soft_block=has_open,
        )

    # 6. No exit yet — caller continues to N+1. Code-review C2: explicit
    #    NO_EXIT sentinel so the caller doesn't have to re-derive "did
    #    anything actually exit?" from open_* lists.
    return ExitDecision(
        reason=ExitReason.NO_EXIT,
        open_highs=open_highs,
        open_mediums=open_mediums,
        open_questions=open_questions,
        needs_soft_block=False,
    )


def escalate_to_resolved_with_deferrals(
    decision: ExitDecision,
    deferrals: list[Deferral],
) -> ExitDecision:
    """Promote a soft-blocked exit to RESOLVED_WITH_DEFERRALS after the user
    completes the §5.4.1 deferral flow.

    Code-review C3: the original exit reason (CEILING_HIT, PLANNER_LOCKED,
    COST_CAPPED) describes WHY the loop was about to exit. After the user
    explicitly defers all open items with reasons + targets, the AUDIT
    semantic upgrades to "we exited cleanly with declared deferrals" — the
    end-report should reflect that, not the underlying ceiling/lock/cap.

    Caller passes the original `ExitDecision` from `evaluate_exit()` plus
    the list of `Deferral` objects collected by SKILL.md's soft-block flow.
    The new decision keeps the open_* lists (so the end-report can still
    enumerate what was deferred) but flips reason → RESOLVED_WITH_DEFERRALS
    and needs_soft_block → False.
    """
    if not deferrals:
        # No deferrals collected; original decision stands.
        return decision
    return ExitDecision(
        reason=ExitReason.RESOLVED_WITH_DEFERRALS,
        open_highs=decision.open_highs,
        open_mediums=decision.open_mediums,
        open_questions=decision.open_questions,
        needs_soft_block=False,
    )


def _open_items(state: RoundState) -> tuple[list[str], list[str], list[str]]:
    """Return finding IDs that the planner did NOT decide this round, by severity.

    "Did not decide" = no PlannerDecision with a non-uncertain decision for
    that item_id. Open questions follow the same rule.
    """
    if state.reviewer_response is None:
        return [], [], []

    decided_ids = {
        d.item_id
        for d in state.decisions
        if d.decision in ("accept", "reject", "accept_via_user", "reject_via_user")
    }

    findings_by_id: dict[str, Finding] = {}
    for i, finding in enumerate(state.reviewer_response.findings, start=1):
        findings_by_id[f"f_r{state.round_n}_{i}"] = finding

    open_highs = [
        fid for fid, f in findings_by_id.items()
        if f.severity == "high" and fid not in decided_ids
    ]
    open_mediums = [
        fid for fid, f in findings_by_id.items()
        if f.severity == "medium" and fid not in decided_ids
    ]
    open_questions = [
        oq.id for oq in state.reviewer_response.open_questions if oq.id not in decided_ids
    ]
    return open_highs, open_mediums, open_questions


def _all_rejected(decisions: list[PlannerDecision]) -> bool:
    if not decisions:
        return False
    return all(d.decision in ("reject", "reject_via_user") for d in decisions)


# --- Plan-bloat detection (§5.5) ---------------------------------------------


@dataclass(frozen=True)
class BloatVerdict:
    triggered: bool
    growth_fraction: float
    new_high_findings: int
    window: int


def evaluate_bloat(
    *,
    sidecars: list[dict[str, Any]],
    current_plan_size_chars: int,
    threshold: float,
    window: int,
) -> BloatVerdict:
    """Trigger if plan grew >threshold over the last `window` rounds and zero new highs.

    Caller (SKILL.md) drives the AskUserQuestion when `triggered=True`. The
    "no new highs" check looks at the most recent `window` rounds combined
    rather than the current round alone — single-round zero-high is normal
    once the loop has settled, but plan-bloat is about the *trend*.
    """
    if len(sidecars) < window:
        return BloatVerdict(triggered=False, growth_fraction=0.0, new_high_findings=0, window=window)

    baseline_size = sidecars[-window]["stats"]["plan_size_chars"]
    if baseline_size <= 0:
        return BloatVerdict(triggered=False, growth_fraction=0.0, new_high_findings=0, window=window)
    growth = (current_plan_size_chars - baseline_size) / baseline_size

    new_high = 0
    for sidecar in sidecars[-window:]:
        new_high += sidecar["stats"]["severity_histogram"]["high"]

    triggered = growth > threshold and new_high == 0
    return BloatVerdict(
        triggered=triggered,
        growth_fraction=growth,
        new_high_findings=new_high,
        window=window,
    )


# --- Resume support (§5.9) ---------------------------------------------------


@dataclass(frozen=True)
class ResumeStatus:
    has_prior_run: bool
    last_completed_round: int  # 0 if no prior run
    cumulative_cost_usd: float
    sidecar_count: int


def detect_resume(*, slug: str, version: str) -> ResumeStatus:
    """Walk prior sidecars; report what's available for resume.

    Reads ONLY from JSON sidecars (the authoritative artifact). The fixes-md
    is regenerated from sidecars on resume — never used as a state source.
    Validates each sidecar; raises `ResumeIntegrityError` on any failure so
    the caller can surface to the user.
    """
    sidecars = load_sidecars(slug=slug, version=version)
    if not sidecars:
        return ResumeStatus(False, 0, 0.0, 0)

    for sidecar in sidecars:
        validate_sidecar(sidecar)

    last = sidecars[-1]
    cumulative = float(last["stats"].get("cumulative_cost_usd", 0.0))
    return ResumeStatus(
        has_prior_run=True,
        last_completed_round=last["round"],
        cumulative_cost_usd=cumulative,
        sidecar_count=len(sidecars),
    )


def restore_snapshots_from_sidecars(*, slug: str, version: str) -> int:
    """Materialize `.scratch/` snapshots from sidecars on resume.

    For each prior round N: write r{N+1} (= state at end of round N = state
    at start of round N+1) by reading sidecar-N's plan_content. For round 1
    specifically, also write r1 from baseline_plan_content. Returns the
    number of snapshots restored.
    """
    sidecars = load_sidecars(slug=slug, version=version)
    if not sidecars:
        return 0

    count = 0
    # r1 = baseline (only from round-1 sidecar)
    first = sidecars[0]
    if first["round"] == 1 and first.get("baseline_plan_content"):
        path = _snapshot_path(slug, version, 1)
        path.write_text(first["baseline_plan_content"], encoding="utf-8")
        count += 1

    # rN+1 = end-of-round-N for each prior round
    for sidecar in sidecars:
        target_index = sidecar["round"] + 1
        path = _snapshot_path(slug, version, target_index)
        path.write_text(sidecar["plan_content"], encoding="utf-8")
        count += 1
    return count


# --- Destructive Start over (§5.0) ------------------------------------------


@dataclass(frozen=True)
class StartOverPlan:
    """What `start_over` will delete. Caller surfaces this to the user before deletion."""

    sidecars: list[Path]
    fixes_md: Path | None
    snapshots: list[Path]


def plan_start_over(*, slug: str, version: str) -> StartOverPlan:
    """Enumerate the files a Start over destructive op would delete."""
    fixs_dir = Path("plans/fixs")
    sidecars: list[Path] = []
    if fixs_dir.is_dir():
        sidecars = sorted(
            fixs_dir.glob(f"{version}-{slug}-round-*.json"),
            key=lambda p: _round_number_from_path(p, slug, version) or 0,
        )
    fixes_md = fixs_dir / f"{version}-{slug}-fixes.md"
    fixes_md_present: Path | None = fixes_md if fixes_md.exists() else None

    snapshot_dir = Path(".scratch")
    snapshots: list[Path] = []
    if snapshot_dir.is_dir():
        snapshots = sorted(snapshot_dir.glob(f"{version}-{slug}-plan-snapshot-r*.md"))

    return StartOverPlan(
        sidecars=sidecars,
        fixes_md=fixes_md_present,
        snapshots=snapshots,
    )


def execute_start_over(
    plan: StartOverPlan,
    *,
    user_decision: str,
    previous_run_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delete the files in `plan` and return the `restart_metadata` payload.

    The caller (SKILL.md) is responsible for capturing user_decision text and
    optionally a summary of the previous run before calling. NEVER deletes
    `plans/<version>-<slug>.md` — the plan itself is user-authored and out
    of scope per §5.0.
    """
    deleted: list[str] = []
    for path in plan.sidecars:
        try:
            path.unlink()
            deleted.append(str(path))
        except OSError:
            pass
    if plan.fixes_md and plan.fixes_md.exists():
        try:
            plan.fixes_md.unlink()
            deleted.append(str(plan.fixes_md))
        except OSError:
            pass
    for path in plan.snapshots:
        try:
            path.unlink()
        except OSError:
            pass

    metadata: dict[str, Any] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "deleted_files": deleted,
        "user_decision": user_decision,
    }
    if previous_run_summary is not None:
        metadata["previous_run_summary"] = previous_run_summary
    return metadata
