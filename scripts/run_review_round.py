"""One reviewer round: build the prompt, invoke, retry, fall back on quota.

This used to live as Python embedded in SKILL.md prose, which cost us two
defects and blocked the fix for a third. Nothing imports a Markdown code block,
so a fallback whose argument list contained a *comment* where `--diff-file`
should be — leaving the advertised mid-loop quota fallback dead from round 2
onward — passed every green suite. The retry policy had nowhere testable to
live either.

The seam is deliberate: this module owns prompt construction, diff preparation,
invocation, retry and the transport switch. Sidecar persistence (SKILL.md step
6) and exit evaluation (step 7) are separate state-machine phases and stay
there.

Two invariants earn their keep:

- **Builder argv is assembled in exactly one place** (`_BuildContext.argv`).
  The original defect was a hand-retyped argument list in the fallback path,
  and the first draft of the plan for this module reproduced the same class of
  bug for a different flag (`--diff-recovered-from-git`). One construction
  site, used by both the initial build and the rebuild, makes that failure
  unrepresentable rather than merely discouraged.
- **Every attempt's cost is summed**, not just the winning one. A guard-
  truncated Claude run reports what it spent before the guard tripped, and a
  malformed success consumed tokens before it failed to parse. Counting only
  the final call can under-report a round by close to half — and the cost cap
  gates on that number.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from loop_state import compute_round_diff
from parse_review import ReviewResult, ReviewSchemaError
from reviewer import (
    QuotaExhaustedError,
    TransportError,
    TransportSelection,
    _is_claude_cli_available,
    invoke_reviewer,
)
import os

# Derived, never accepted as an argument: a caller-supplied skill directory is
# both a second authority over where code lives and an injection surface.
SKILL_DIR = Path(__file__).resolve().parent.parent
_BUILDER = SKILL_DIR / "scripts" / "build_reviewer_prompt_v2.py"

# Hard ceiling, enforced by a counter rather than recursion: at most two
# attempts on the originally selected transport, at most one fallback switch,
# at most two attempts on Claude.
MAX_ATTEMPTS_PER_TRANSPORT = 2


class RoundRunError(RuntimeError):
    """The round could not be completed. Carries what was spent getting there."""

    def __init__(self, message: str, *, attempts: list[AttemptRecord]) -> None:
        super().__init__(message)
        self.attempts = attempts

    @property
    def total_cost_usd(self) -> float:
        return sum(a.cost_usd or 0.0 for a in self.attempts)


@dataclass(frozen=True)
class AttemptRecord:
    """One call to a reviewer transport, successful or not."""

    transport: str
    outcome: str  # "success" | a TransportError kind | "schema_error"
    cost_usd: float | None = 0.0  # None == unknown, which is NOT free
    tokens_input: int = 0
    tokens_output: int = 0


@dataclass(frozen=True)
class RoundRunOutcome:
    result: ReviewResult
    transport: str  # the transport that actually produced `result`
    attempts: list[AttemptRecord] = field(default_factory=list)
    prompt_debug_path: Path | None = None

    @property
    def total_cost_usd(self) -> float:
        """Summed over every attempt — see the module docstring.

        Unknown-cost attempts contribute 0 here; `cost_complete` says whether
        that happened, so a caller can surface incomplete accounting rather
        than trusting a total that quietly omits a call.
        """
        return sum(a.cost_usd or 0.0 for a in self.attempts)

    @property
    def cost_complete(self) -> bool:
        """False when any attempt could not be priced."""
        return all(a.cost_usd is not None for a in self.attempts)

    @property
    def tokens_input(self) -> int:
        return sum(a.tokens_input for a in self.attempts)

    @property
    def tokens_output(self) -> int:
        return sum(a.tokens_output for a in self.attempts)


@dataclass(frozen=True)
class _BuildContext:
    """Everything the builder needs, captured once.

    `diff_recovered_from_git` travels with `diff_path` on purpose:
    `compute_round_diff` returns the pair precisely so the prompt can warn that
    the diff is cumulative-against-HEAD rather than snapshot-accurate. Carrying
    the bytes without the flag keeps the prompt syntactically valid while
    silently changing what it claims — the exact failure this module exists to
    make unrepresentable.
    """

    repo_root: Path
    plan_file: Path
    slug: str
    version: str
    round_n: int
    diff_path: Path | None
    diff_recovered_from_git: bool
    consistency_only: bool
    cumulative_cost_usd: float

    def argv(self, transport_name: str) -> list[str]:
        """The ONE place builder argv is assembled."""
        argv = [
            sys.executable,
            str(_BUILDER),
            "--plan-file", str(self.plan_file),
            "--slug", self.slug,
            "--version", self.version,
            "--round", str(self.round_n),
            "--cumulative-cost-usd", str(self.cumulative_cost_usd),
            "--transport", transport_name,
        ]
        if self.diff_path is not None:
            argv += ["--diff-file", str(self.diff_path)]
        if self.diff_recovered_from_git:
            argv.append("--diff-recovered-from-git")
        if self.consistency_only:
            argv.append("--consistency-only")
        return argv

    def build(self, transport_name: str) -> str:
        proc = subprocess.run(
            self.argv(transport_name),
            cwd=self.repo_root,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RoundRunError(
                f"prompt builder exited {proc.returncode} for transport "
                f"{transport_name!r}: {proc.stderr.strip() or '(no stderr)'}",
                attempts=[],
            )
        return proc.stdout


def _attempt_usage(exc: BaseException) -> tuple[float | None, int, int]:
    """Spend reported by a failed attempt.

    `TransportError` declares these fields; `ReviewSchemaError` has them
    attached at the raise site in `reviewer.py`. Read defensively so a future
    error type that reports nothing degrades to zero rather than crashing the
    accounting.
    """
    raw = getattr(exc, "cost_usd", None)
    return (
        float(raw) if isinstance(raw, (int, float)) else None,
        int(getattr(exc, "tokens_input", 0) or 0),
        int(getattr(exc, "tokens_output", 0) or 0),
    )


def _is_retryable(exc: BaseException) -> bool:
    """D20's retry-once covers malformed output as well as transport blips.

    `ReviewSchemaError` is a separate class from `TransportError`, and both
    SKILL.md and the README promise it a retry — that response was already paid
    for. Retrying only transport failures would leave a documented half of the
    policy dead.
    """
    if isinstance(exc, QuotaExhaustedError):
        return False  # a transport switch, not a retry
    if isinstance(exc, ReviewSchemaError):
        return True
    return isinstance(exc, TransportError) and exc.is_transient


def _allows_quota_fallback(selection: TransportSelection) -> bool:
    """Only an AUTO-DETECTED openai selection may silently switch transports.

    An operator who wrote `ADVERSARIAL_TRANSPORT=openai` asked for openai; the
    honest answer to quota exhaustion there is the error, not a different
    reviewer.

    Reads `selection.source`, never `selection.reason`: the latter is a
    human-readable display string, so string-matching it meant a reworded
    message — or a legitimate hand-built `TransportSelection("openai", "forced
    by environment")` — silently re-enabled the fallback. `source` defaults to
    "explicit", so an unknown provenance fails closed.
    """
    return (
        selection.name == "openai"
        and selection.source != "explicit"
        and _is_claude_cli_available(dict(os.environ))
    )


def run_review_round(
    *,
    repo_root: Path,
    slug: str,
    version: str,
    round_n: int,
    selection: TransportSelection,
    consistency_only: bool = False,
    cumulative_cost_usd: float = 0.0,
    keep_prompt: bool = False,
) -> RoundRunOutcome:
    """Build, invoke, retry and (on quota) switch transports for one round.

    `repo_root` is required and authoritative: the plan file, sidecar dir,
    snapshot dir and both subprocess working directories resolve beneath it, so
    a round cannot build from one repository, recover snapshots from another,
    and invoke the reviewer in a third.
    """
    repo_root = Path(repo_root).resolve()
    # `compute_round_diff` resolves `.scratch/` and `plans/fixs/` from the
    # PROCESS cwd, and the builder defaults `--sidecars-dir` to a cwd-relative
    # `plans/fixs`. Neither takes a root argument. Rather than let those quietly
    # disagree with `repo_root` — diffing one repository's snapshots against
    # another's plan — require them to match and say so when they do not.
    if Path.cwd().resolve() != repo_root:
        raise RoundRunError(
            f"run_review_round requires cwd == repo_root. repo_root={repo_root}, "
            f"cwd={Path.cwd().resolve()}. compute_round_diff and the prompt "
            "builder both resolve .scratch/ and plans/fixs/ from the process cwd, "
            "so a mismatch would silently mix two repositories.",
            attempts=[],
        )
    plan_file = repo_root / "plans" / f"{version}-{slug}.md"
    if not plan_file.is_file():
        raise RoundRunError(f"plan file not found: {plan_file}", attempts=[])

    workdir = tempfile.TemporaryDirectory(prefix=f"apr-{version}-{slug}-r{round_n}-")
    try:
        diff_path: Path | None = None
        recovered = False
        if round_n > 1:
            # Computed here, not accepted: this function owns the round
            # context, so it derives the diff rather than trusting a caller to
            # have produced one for the same round from the same repo.
            diff_text, recovered = compute_round_diff(
                plan_file, round_n=round_n, slug=slug, version=version
            )
            diff_path = Path(workdir.name) / f"round-{round_n}-diff.patch"
            diff_path.write_text(diff_text, encoding="utf-8")

        ctx = _BuildContext(
            repo_root=repo_root,
            plan_file=plan_file,
            slug=slug,
            version=version,
            round_n=round_n,
            diff_path=diff_path,
            diff_recovered_from_git=recovered,
            consistency_only=consistency_only,
            cumulative_cost_usd=cumulative_cost_usd,
        )

        attempts: list[AttemptRecord] = []
        prompt_path = Path(workdir.name) / f"round-{round_n}-prompt.txt"

        result, transport = _run_with_policy(
            ctx, selection, attempts=attempts, prompt_path=prompt_path
        )
        return RoundRunOutcome(
            result=result,
            transport=transport,
            attempts=attempts,
            prompt_debug_path=_retain_prompt(prompt_path, slug, version, round_n)
            if keep_prompt
            else None,
        )
    finally:
        # Always. `TemporaryDirectory` owns a finalizer that deletes the tree
        # when the object is released, so merely skipping this call left
        # `keep_prompt=True` handing back a path to something already gone —
        # the outcome holds a Path, not the owner. Retained prompts are copied
        # out to a directory nothing auto-cleans instead.
        workdir.cleanup()


def _retain_prompt(prompt_path: Path, slug: str, version: str, round_n: int) -> Path:
    """Copy the prompt somewhere no finalizer will reclaim.

    `mkdtemp` deliberately, not `TemporaryDirectory`: the caller asked to keep
    this for debugging, so ownership passes to them and nothing auto-deletes it.
    """
    keep_dir = Path(tempfile.mkdtemp(prefix=f"apr-keep-{version}-{slug}-r{round_n}-"))
    kept = keep_dir / prompt_path.name
    kept.write_text(prompt_path.read_text(encoding="utf-8"), encoding="utf-8")
    return kept


def _run_with_policy(
    ctx: _BuildContext,
    selection: TransportSelection,
    *,
    attempts: list[AttemptRecord],
    prompt_path: Path,
) -> tuple[ReviewResult, str]:
    """Attempt policy: retry-once per transport, at most one quota switch."""
    transport = selection.name
    active = selection
    switched = False

    prompt = ctx.build(transport)
    prompt_path.write_text(prompt, encoding="utf-8")

    while True:
        tries = sum(1 for a in attempts if a.transport == transport)
        if tries >= MAX_ATTEMPTS_PER_TRANSPORT:
            raise RoundRunError(
                f"{transport} exhausted its {MAX_ATTEMPTS_PER_TRANSPORT}-attempt "
                "ceiling for this round",
                attempts=attempts,
            )
        try:
            result = invoke_reviewer(
                prompt,
                round_n=ctx.round_n,
                transport=active,
                repo_root=str(ctx.repo_root),
            )
        except QuotaExhaustedError as exc:
            cost, tok_in, tok_out = _attempt_usage(exc)
            attempts.append(AttemptRecord(transport, "quota", cost, tok_in, tok_out))
            if switched or not _allows_quota_fallback(active):
                raise RoundRunError(
                    f"{transport} quota exhausted and no fallback permitted: {exc}",
                    attempts=list(attempts),
                ) from exc
            switched = True
            transport = "claude"
            active = TransportSelection("claude", "openai quota exhausted")
            # Rebuild through the SAME construction site — the openai prompt
            # carries no repo-verification / finding-discipline / output-format
            # calibration, so reusing it would ship the wrong instructions.
            try:
                prompt = ctx.build(transport)
            except RoundRunError as build_exc:
                # `build` cannot see the ledger, so it raises with an empty
                # one; re-raise carrying what the quota attempt already cost.
                raise RoundRunError(
                    str(build_exc), attempts=list(attempts)
                ) from build_exc
            prompt_path.write_text(prompt, encoding="utf-8")
            continue
        except (TransportError, ReviewSchemaError) as exc:
            cost, tok_in, tok_out = _attempt_usage(exc)
            outcome = getattr(exc, "kind", "schema_error")
            attempts.append(AttemptRecord(transport, outcome, cost, tok_in, tok_out))
            if not _is_retryable(exc):
                raise RoundRunError(
                    f"{transport} failed with a non-retryable error: {exc}",
                    attempts=list(attempts),
                ) from exc
            if sum(1 for a in attempts if a.transport == transport) >= (
                MAX_ATTEMPTS_PER_TRANSPORT
            ):
                raise RoundRunError(
                    f"{transport} failed twice; second failure: {exc}",
                    attempts=list(attempts),
                ) from exc
            continue

        attempts.append(
            AttemptRecord(
                transport,
                "success",
                result.usage.cost_usd,
                result.usage.tokens_input,
                result.usage.tokens_output,
            )
        )
        return result, transport
