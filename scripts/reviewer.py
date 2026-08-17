"""Transport abstraction for the adversarial plan reviewer.

Three paths converge on the same `ReviewResult` shape (defined in
`parse_review.py`):

- **OpenAI Responses API** (`gpt-5.6-sol` by default) — JSON schema enforcement
  via the strict structured-output mode. The recommended path.
- **Claude Code CLI** (`claude -p --output-format json`) — repo-verifying
  fallback: the reviewer runs inside the plan's repo with a contained tool
  floor, so it can open the files the plan cites. No server-side schema
  enforcement; `parse_claude_response` validates and the D20 retry covers
  malformed output.
- **Codex CLI** (`codex exec` / `codex-companion.mjs task`) — v1-compatible
  prose path. Severity is inferred via keyword heuristic; no schema
  enforcement.

Transport selection follows v2-plan §5.1 + the claude-transport plan:
  1. `ADVERSARIAL_TRANSPORT=openai|codex|claude` env var, if set
  2. Otherwise auto-detect: `OPENAI_API_KEY` → openai, Claude CLI on PATH →
     claude, Codex CLI on PATH → codex
  3. Otherwise raise so the first-run UX (`first_run.py`) can take over

Phase 1+2 scope: this module replaces the v1 ad-hoc Codex invocation in
SKILL.md but keeps v1 loop logic intact. Severity-gated exit, diff-aware
prompts, and resume support land in Phase 3+4.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from _dotenv import load_local_env
from parse_review import (
    REVIEW_SCHEMA,
    ReviewResult,
    ReviewSchemaError,
    parse_claude_response,
    parse_codex_prose,
    parse_openai_response,
)
from cost_tracker import estimate_cost_usd

# Populate os.environ from .env (shell values win) so OPENAI_API_KEY and
# friends are visible whether the user exported them or just wrote them to
# the skill's .env file.
load_local_env()

DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_MAX_OUTPUT_TOKENS = 8000

# CLI alias — the resolved model id comes back in the envelope's
# `modelUsage[*].canonicalModel` and is what we record / rate-key on.
DEFAULT_CLAUDE_MODEL = "opus"

# CLI alias → canonical model id, for the FALLBACK path only (an envelope with
# no `modelUsage`). Without it the bare alias reached `ReviewResult.model` /
# the sidecar AND, being neither a `gpt*` nor a `claude-*` id, slipped past
# cost_tracker's override gate so an operator's `OPENAI_*_USD_PER_1M` rates got
# applied to a claude round. Covers the documented aliases; anything else passes
# through unchanged rather than being guessed at.
CLAUDE_MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "fable": "claude-fable-5",
}

# Containment floor for the claude subprocess (claude-transport plan R1-H1/M4).
# `--allowedTools` ALONE DOES NOT CONTAIN: probed on claude 2.1.227, a `-p`
# child launched on a machine whose user settings carry
# `permissions.defaultMode: bypassPermissions` and granted only Read/Grep/Glob
# still executed Write and created a file. `--setting-sources ""` is what turns
# the grant into a real denial (and drops inherited hooks + the reviewed repo's
# ambient CLAUDE.md priors, ~37k cache tokens on a one-word probe).
#
# `--tools` is the SET the reviewer may use at all; `--allowedTools` is the
# subset pre-granted without asking. They are deliberately different. Bash is
# in the set but NOT pre-granted, because a bare or wildcarded Bash grant is a
# write primitive: the deny floor below can only blacklist prefixes, so
# `printf x > plans/plan.md`, `sed -i`, `python -c`, and `tee` all sail past it
# and the reviewer can rewrite the very plan it is reviewing. Scoping the grant
# instead (`Bash(git log *)`) is not a fix either — Anthropic's permission docs
# warn that Bash argument patterns are fragile, and a trailing wildcard accepts
# arbitrary further flags.
#
# Leaving Bash ungranted is not the same as removing it. Claude Code itself
# recognizes read-only command forms and permits them in every mode while
# escalating anything write-capable, so the reviewer keeps `git log`,
# `git show`, `git status`, `git diff`, `git ls-files`, `stat` and friends —
# which is what "repo-verifying" actually needs — while escalation under
# `--setting-sources ""` is a real denial rather than an inherited approval.
# What is intentionally given up: arbitrary process execution, i.e. running the
# reviewed repo's tests or linters (they create caches and generated files),
# `rg --pre`, and arbitrary repo scripts. Those need a genuine read-only
# filesystem sandbox before they can honestly live in this transport.
DEFAULT_CLAUDE_TOOLS = "Read,Grep,Glob,Bash"
DEFAULT_CLAUDE_ALLOWED_TOOLS = "Read,Grep,Glob"
CLAUDE_DISALLOWED_TOOLS = (
    "Write,Edit,MultiEdit,NotebookEdit,"
    "Bash(git commit*),Bash(git push*),Bash(git reset*),"
    "Bash(git checkout*),Bash(git restore*),Bash(git stash*),"
    "Bash(rm -r*),Bash(sudo*)"
)
DEFAULT_CLAUDE_TIMEOUT_S = 1200
DEFAULT_CLAUDE_MAX_TURNS = 120  # 2x the max turn count observed in the R1 probes
DEFAULT_CLAUDE_MAX_BUDGET_USD = 5.0

# Substrings that mark a claude error envelope as worth one D20 retry.
_CLAUDE_TRANSIENT_MARKERS = (
    "overload",
    "rate limit",
    "rate_limit",
    "timeout",
    "timed out",
    "temporarily",
    "connection",
    "429",
    "502",
    "503",
)

# Substrings that mark an OpenAI failure as "the account is out of money /
# quota", i.e. no amount of retrying helps and the orchestration should switch
# transports rather than retry (claude-transport plan R1-H3).
_OPENAI_QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing_hard_limit_reached",
    "billing hard limit",
    "credit balance is too low",
    "out of credits",
    "quota exhausted",
)


# --- Errors ------------------------------------------------------------------


TRANSPORT_ERROR_KINDS = (
    "wall_timeout",  # our own --timeout tripped; a retry just doubles the wait
    "max_turns",     # our own --max-turns guard; a second stochastic run may fit
    "max_budget",    # our own --max-budget-usd guard; likewise
    "api",           # rate limit, overload, network blip, malformed success
    "permanent",     # auth failure, bad request, unknown model
    "quota",         # account out of credit -> transport switch, never a retry
)
# Only these get D20's retry-once. `quota` is excluded because the answer is a
# transport switch, and `wall_timeout` because the retry inherits the same
# timeout: one round would burn 2 x ADVERSARIAL_CLAUDE_TIMEOUT_S before failing.
_RETRYABLE_KINDS = frozenset({"max_turns", "max_budget", "api"})


class TransportError(RuntimeError):
    """Reviewer transport could not complete the request.

    Distinct from `ReviewSchemaError` — this means the network/CLI call itself
    failed, not that the response was malformed. Callers may retry or escalate
    based on which error type they see.

    `kind` carries the retry contract. It replaced a bare `is_transient`
    boolean, which could not express the policy the caller actually needs: a
    1200-second wall-clock timeout and a rate limit were both "transient", yet
    retrying the rate limit is the entire point while retrying the timeout
    spends 40 minutes on one round before failing anyway.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "permanent",
        cost_usd: float | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
    ) -> None:
        super().__init__(message)
        if kind not in TRANSPORT_ERROR_KINDS:
            raise ValueError(
                f"unknown TransportError kind {kind!r}; expected one of "
                f"{TRANSPORT_ERROR_KINDS}"
            )
        self.kind = kind
        # A failed attempt can still have cost money — a `max_turns` /
        # `max_budget` envelope reports `total_cost_usd` for the work done
        # before the guard tripped. The caller retries, and counting only the
        # winning call would under-report the round, which is what the cost cap
        # gates on.
        #
        # `None` means UNKNOWN, and is deliberately not 0.0: collapsing the two
        # would let an unpriceable attempt silently lower the cap's denominator
        # while looking like a free one. A caller can then surface incomplete
        # accounting, or block conservatively, instead of trusting a total that
        # quietly omits a call.
        self.cost_usd = cost_usd
        self.tokens_input = tokens_input
        self.tokens_output = tokens_output

    @property
    def is_transient(self) -> bool:
        """Derived, read-only view of `kind` for callers that only need a yes/no.

        Not settable on purpose: an independently assignable boolean is exactly
        what let `wall_timeout` and `api` share a retry policy.
        """
        return self.kind in _RETRYABLE_KINDS


class QuotaExhaustedError(TransportError):
    """The selected transport's account has no quota / credit left.

    A distinct type because the response is neither "retry" nor "give up": the
    orchestration (SKILL.md) catches it and, when the transport was
    auto-detected and the Claude CLI is available, **rebuilds the prompt for the
    claude calibration** and re-invokes with an explicit claude selection
    (claude-transport plan R1-H3). `invoke_reviewer` deliberately does NOT fall
    back on its own — the prompt it was handed was built for the openai
    calibration, and silently reusing it would ship the wrong instructions.
    """

    def __init__(self, message: str) -> None:
        # cost 0.0, not None: this is raised from the account-out-of-quota
        # markers, i.e. the request was REJECTED rather than served, so nothing
        # was billed. Reporting it as unknown would flag every ordinary quota
        # fallback as incomplete accounting and make that signal useless.
        super().__init__(message, kind="quota", cost_usd=0.0)


class TransportUnavailableError(TransportError):
    """No reviewer transport is configured.

    The first-run UX in `first_run.py` should have already prompted the user
    before any reviewer call reaches this module. If we still see this, the
    pre-flight ordering (§5.0a step 2) was bypassed — fail loud.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="permanent")


# --- Transport detection -----------------------------------------------------


@dataclass(frozen=True)
class TransportSelection:
    name: str  # "openai" | "claude" | "codex"
    reason: str  # Human-readable explanation, surfaced in logs. DISPLAY ONLY.
    # Structured provenance. Policy (notably the quota fallback, which may only
    # switch away from an AUTO-DETECTED openai) reads this, never `reason` —
    # inferring explicitness by string-matching a human-readable sentence meant
    # a reworded message, or a legitimate hand-built selection, would silently
    # change behaviour. Defaults to "explicit" so an unknown provenance fails
    # closed: worst case the operator sees the error instead of being quietly
    # moved to another reviewer.
    source: str = "explicit"


def detect_transport(*, env: dict[str, str] | None = None) -> TransportSelection:
    """Pick the active reviewer transport.

    Priority per v2-plan §5.1 + the claude-transport plan:
      1. `ADVERSARIAL_TRANSPORT` explicit override
      2. `OPENAI_API_KEY` set → openai
      3. Claude CLI on PATH → claude (outranks the legacy Codex path)
      4. Codex CLI on PATH → codex
      5. raise `TransportUnavailableError`

    `anthropic` is deliberately NOT an alias for `claude` — it rejects like any
    other unknown value so a typo can never silently pick a transport.
    """
    env = dict(os.environ if env is None else env)
    explicit = (env.get("ADVERSARIAL_TRANSPORT") or "").strip().lower()
    if explicit == "openai":
        return TransportSelection("openai", "ADVERSARIAL_TRANSPORT=openai", source="explicit")
    if explicit == "codex":
        return TransportSelection("codex", "ADVERSARIAL_TRANSPORT=codex", source="explicit")
    if explicit == "claude":
        return TransportSelection("claude", "ADVERSARIAL_TRANSPORT=claude", source="explicit")
    if explicit:
        raise TransportError(
            f"ADVERSARIAL_TRANSPORT must be 'openai', 'codex' or 'claude', got '{explicit}'"
        )

    if env.get("OPENAI_API_KEY"):
        return TransportSelection("openai", "OPENAI_API_KEY is set", source="openai_key")
    if _is_claude_cli_available(env):
        return TransportSelection("claude", "Claude CLI on PATH", source="claude_path")
    if _is_codex_cli_available(env):
        return TransportSelection("codex", "Codex CLI on PATH", source="codex_path")

    raise TransportUnavailableError(
        "No reviewer transport configured. "
        "Set OPENAI_API_KEY (recommended), install the Claude Code CLI, or "
        "install the Codex CLI; "
        "see the first-run UX (§5.6 of plans/v2-plan.md) for details."
    )


def _is_claude_cli_available(env: dict[str, str]) -> bool:
    """Hermetic claude-availability check using the injected env's PATH.

    Same env-injection contract as `_is_codex_cli_available` (no
    `shutil.which`, which would always read the live `os.environ["PATH"]`).
    There is no plugin-wrapper fallback: the Claude Code CLI is either on PATH
    or it isn't.
    """
    return _executable_on_env_path("claude", env)


def _is_codex_cli_available(env: dict[str, str]) -> bool:
    """Hermetic codex-availability check using the injected env's PATH.

    Code-review finding I1: previously used `shutil.which("codex")` which
    always reads the live `os.environ["PATH"]`, defeating the env-injection
    contract that `detect_transport(env=...)` advertises. Now walks
    `env["PATH"]` (and PATHEXT on Windows) manually.
    """
    if _executable_on_env_path("codex", env):
        return True

    plugin_root = env.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        wrapper = os.path.join(plugin_root, "scripts", "codex-companion.mjs")
        if os.path.isfile(wrapper):
            return True
    return False


def _executable_on_env_path(name: str, env: dict[str, str]) -> bool:
    """True if `name` (PATHEXT-aware) is a file on the INJECTED env's PATH."""
    path_env = env.get("PATH", "")
    pathext = env.get("PATHEXT", "")
    # PATHEXT is a Windows construct and is ALWAYS ';'-separated — splitting on
    # os.pathsep broke the hermetic env-injection contract on POSIX hosts (':'),
    # where an injected Windows-style PATHEXT became one unsplittable token.
    extensions = (
        [""] + [ext.lower() for ext in pathext.split(";") if ext]
        if pathext
        else [""]
    )
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        for ext in extensions:
            if os.path.isfile(os.path.join(directory, f"{name}{ext}")):
                return True
    return False


# --- Public entry point ------------------------------------------------------


def invoke_reviewer(
    prompt: str,
    *,
    round_n: int,
    model: str | None = None,
    transport: TransportSelection | None = None,
    repo_root: str | None = None,
) -> ReviewResult:
    """Run the reviewer against `prompt`, return parsed `ReviewResult`.

    `round_n` is required because the post-parse `assign_open_question_ids()`
    helper needs it to mint the `oq_r{round}_{idx}` identifiers (§5.2).

    `repo_root` is the plan's repo — the claude transport runs there so its
    repo-verification pass can open the files the plan cites. Defaults to the
    process cwd; the openai and codex transports ignore it.

    Raises `QuotaExhaustedError` (a `TransportError`) when the openai account is
    out of quota. That is deliberately NOT handled here: the caller owns the
    transport switch because the switch requires rebuilding the prompt for the
    claude calibration (claude-transport plan R1-H3).
    """
    if transport is None:
        transport = detect_transport()

    if transport.name == "openai":
        return _invoke_openai(prompt, round_n=round_n, model=model)
    if transport.name == "claude":
        return _invoke_claude(
            prompt, round_n=round_n, model=model, repo_root=repo_root or os.getcwd()
        )
    if transport.name == "codex":
        return _invoke_codex(prompt, round_n=round_n, model=model)
    raise TransportError(f"unknown transport '{transport.name}'")


# --- OpenAI Responses API path ----------------------------------------------


def _invoke_openai(prompt: str, *, round_n: int, model: str | None) -> ReviewResult:
    """Send `prompt` to the OpenAI Responses API with strict JSON schema."""
    try:
        import openai  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TransportError(
            "openai package not installed. Run `pip install openai>=1.0` "
            "or set ADVERSARIAL_TRANSPORT=codex to use the legacy fallback."
        ) from exc

    if not os.environ.get("OPENAI_API_KEY"):
        raise TransportError(
            "OPENAI_API_KEY not set but openai transport selected. "
            "Configure the key (e.g. via `.env`) or set ADVERSARIAL_TRANSPORT=codex."
        )

    chosen_model = model or os.environ.get("OPENAI_REVIEWER_MODEL", DEFAULT_OPENAI_MODEL)
    max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS)))

    client = openai.OpenAI()
    try:
        response = client.responses.create(
            model=chosen_model,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "review",
                    "schema": REVIEW_SCHEMA,
                    "strict": True,
                }
            },
            max_output_tokens=max_tokens,
        )
    except openai.APIError as exc:
        # Quota exhaustion is neither transient nor a dead end: it is the
        # signal to switch transports (claude-transport plan R1-H3). Classify
        # it FIRST — a quota 429 is a `RateLimitError`, which the transient
        # check below would otherwise mark retryable, burning D20's retry on a
        # call that cannot succeed.
        if _is_openai_quota_error(exc):
            raise QuotaExhaustedError(
                f"OpenAI quota/credit exhausted ({type(exc).__name__}): {exc}"
            ) from exc
        # Code-review finding I3: classify the error so the caller's retry
        # policy (D20) knows whether to retry or escalate. Connection errors,
        # rate limits, and 5xx are transient; auth/permission/bad-request
        # errors are permanent.
        transient_classes = (
            getattr(openai, "APIConnectionError", ()),
            getattr(openai, "APITimeoutError", ()),
            getattr(openai, "RateLimitError", ()),
            getattr(openai, "InternalServerError", ()),
        )
        is_transient = isinstance(exc, transient_classes) if transient_classes else False
        raise TransportError(
            f"OpenAI Responses API call failed ({type(exc).__name__}): {exc}",
            kind="api" if is_transient else "permanent",
        ) from exc

    raw_text = _extract_openai_output_text(response)
    tokens_input, tokens_output = _extract_openai_usage(response)
    cost_usd = estimate_cost_usd(chosen_model, tokens_input, tokens_output)

    try:
        return parse_openai_response(
            raw_text,
            round_n=round_n,
            model=chosen_model,
            usage_input_tokens=tokens_input,
            usage_output_tokens=tokens_output,
            cost_usd=cost_usd,
        )
    except ReviewSchemaError as exc:
        # Schema violations are surfaced to the caller for the retry-once policy
        # (D20). The response was already billed before it failed to parse, so
        # attach what it cost — same contract as the claude path. Without this
        # an OpenAI malformed-success retry is accounted as free.
        exc.cost_usd = cost_usd
        exc.tokens_input = tokens_input
        exc.tokens_output = tokens_output
        raise


def _is_openai_quota_error(exc: Exception) -> bool:
    """True when an OpenAI error means "no quota / credit left", not "retry".

    Covers the `RateLimitError` + `insufficient_quota` pair and the
    exhausted-credits auth/permission variants (a dead key and a drained
    prepaid balance surface as different classes across SDK versions, so we
    match on the machine-readable `code`/`type` first and the message text as a
    fallback rather than on the exception class).
    """
    haystack: list[str] = [str(exc)]
    for attr in ("code", "type"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            haystack.append(value)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            for key in ("code", "type", "message"):
                value = error.get(key)
                if isinstance(value, str):
                    haystack.append(value)
    lowered = " ".join(haystack).lower()
    return any(marker in lowered for marker in _OPENAI_QUOTA_MARKERS)


def _extract_openai_output_text(response: object) -> str:
    """Read the raw model output text in a way that's tolerant to SDK churn."""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text:
        return output_text
    raise TransportError(
        "OpenAI response missing output_text; cannot parse reviewer JSON"
    )


def _extract_openai_usage(response: object) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from a Responses API result."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    tokens_input = getattr(usage, "input_tokens", 0) or 0
    tokens_output = getattr(usage, "output_tokens", 0) or 0
    return int(tokens_input), int(tokens_output)


# --- Claude Code CLI path ----------------------------------------------------


def _env_int(name: str, default: int) -> int:
    """Read an integer env knob, or raise a `TransportError` naming the garbage.

    A bare `int()` on a mistyped `.env` value raised a raw `ValueError` that told
    the operator nothing about which knob was wrong.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise TransportError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    """Read a float env knob, or raise a `TransportError` naming the garbage."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise TransportError(f"{name} must be a number, got {raw!r}") from exc


def _invoke_claude(
    prompt: str,
    *,
    round_n: int,
    model: str | None,
    repo_root: str | None = None,
) -> ReviewResult:
    """Run `claude -p` as a contained, settings-isolated reviewer subprocess.

    The discriminator role travels in the prompt itself (the builder emits
    `<role>`), so settings isolation costs nothing behaviourally while removing
    the reviewed repo's hooks, CLAUDE.md priors, and — critically — its
    `permissions.defaultMode` from the child.
    """
    chosen_model = model or os.environ.get("CLAUDE_REVIEWER_MODEL", DEFAULT_CLAUDE_MODEL)
    # Everything read from the environment here is attacker-supplied for our
    # purposes: `load_local_env()` sources `<cwd>/.env`, lets it beat the
    # skill's own file, and cwd is the repository under review. So the env var
    # states a REQUEST, and both argv values below are intersections with the
    # built-in constants — subsets, never supersets.
    #
    # The SET is intersected too, not only the grant. "In `--tools` but absent
    # from `--allowedTools`" is NOT equivalent to "absent": the containment
    # probe showed Claude Code executes ungranted commands it classifies as
    # read-only — that is exactly how `git log` still works — so an unexpected
    # tool smuggled into the set could run without ever appearing in the
    # grant. There is deliberately no env var for the pre-grant at all: an
    # overridable grant would let a reviewed repo hand a shell to the reviewer
    # reading it, i.e. the audited subject choosing the auditor's permissions.
    requested = {
        t.strip()
        for t in os.environ.get(
            "ADVERSARIAL_CLAUDE_TOOLS", DEFAULT_CLAUDE_TOOLS
        ).split(",")
        if t.strip()
    }
    # Constant order, not request order, so argv stays deterministic.
    tools = ",".join(t for t in DEFAULT_CLAUDE_TOOLS.split(",") if t in requested)
    allowed_tools = ",".join(
        t for t in DEFAULT_CLAUDE_ALLOWED_TOOLS.split(",") if t in tools.split(",")
    )
    if not tools:
        # Fail closed and legibly: an empty set would otherwise reach the CLI
        # as a bare empty argument and surface as an opaque parse error.
        raise TransportError(
            "ADVERSARIAL_CLAUDE_TOOLS requested no tool from the supported set "
            f"({DEFAULT_CLAUDE_TOOLS}) — the reviewer would have no way to read "
            "the plan."
        )
    timeout_s = _env_int("ADVERSARIAL_CLAUDE_TIMEOUT_S", DEFAULT_CLAUDE_TIMEOUT_S)
    max_turns = _env_int("ADVERSARIAL_CLAUDE_MAX_TURNS", DEFAULT_CLAUDE_MAX_TURNS)
    # Normalised through float() so a bad value fails here — as a named
    # TransportError — rather than inside the CLI's own flag parser.
    max_budget = str(
        _env_float("ADVERSARIAL_CLAUDE_MAX_BUDGET_USD", DEFAULT_CLAUDE_MAX_BUDGET_USD)
    )

    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        chosen_model,
        # Containment + independence (R1-H1, R1-M4): flags, not prose. A prose
        # instruction to "never modify tracked files" is advisory; these are
        # enforced by the CLI's own permission layer.
        "--setting-sources",
        "",  # no user/project settings -> no inherited bypassPermissions,
        # hooks, or ambient CLAUDE.md priors
        "--strict-mcp-config",  # no MCP servers
        "--tools",
        tools,  # restrict the tool SET (Bash included)
        "--allowedTools",
        allowed_tools,  # pre-grant only the read tools; Bash must escalate,
        # and under --setting-sources "" escalation is a real denial
        "--disallowedTools",
        CLAUDE_DISALLOWED_TOOLS,
        # NOTE: `--max-turns` is accepted and enforced on claude 2.1.227 but is
        # ABSENT from `claude --help` — a silent-removal risk. A test pins it in
        # the argv builder; the live smoke catches an actual removal.
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        max_budget,  # the documented budget guard
    ]

    try:
        # Prompt over stdin: reviewer prompts routinely exceed Windows' ~32KB
        # argv limit. encoding/errors mirror the codex path (§ non-cp1252
        # codepoints in prompts).
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=repo_root or os.getcwd(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TransportError(
            f"Claude CLI timed out after {timeout_s}s "
            "(ADVERSARIAL_CLAUDE_TIMEOUT_S). NOT retried: a retry inherits the "
            "same timeout, so one round would cost twice the wait before failing.",
            kind="wall_timeout",
        ) from exc
    except subprocess.CalledProcessError as exc:
        # A non-zero exit can still carry the JSON envelope on stdout (a
        # truncated run is reported as `is_error` AND may exit non-zero).
        # Classify from the envelope when there is one, so a max-turns/budget
        # truncation stays transient instead of degrading to an opaque
        # exit-code error that the retry policy can't act on.
        envelope = _envelope_or_none(exc.stdout)
        if envelope is not None:
            _raise_on_claude_error_envelope(
                envelope,
                max_turns=max_turns,
                max_budget=max_budget,
                chosen_model=chosen_model,
            )
        stderr_excerpt = (exc.stderr or "")[-2000:]
        raise TransportError(
            f"Claude CLI exited {exc.returncode}. stderr tail:\n{stderr_excerpt}"
        ) from exc
    except FileNotFoundError as exc:
        raise TransportError(
            "Claude CLI executable not found (claude). "
            "Install Claude Code or set ADVERSARIAL_TRANSPORT=openai."
        ) from exc

    envelope = _parse_claude_envelope(result.stdout)
    # is_error / subtype are checked BEFORE `result` is touched: `result` is
    # null on every error envelope (R1-H4).
    _raise_on_claude_error_envelope(
        envelope, max_turns=max_turns, max_budget=max_budget, chosen_model=chosen_model
    )

    resolved_model = _resolve_claude_model_id(envelope, fallback=chosen_model)
    tokens_input, tokens_output = _extract_claude_usage(envelope)
    cost_usd = _claude_cost_usd(
        envelope,
        resolved_model=resolved_model,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
    )

    raw_text = envelope.get("result")
    if not isinstance(raw_text, str) or not raw_text.strip():
        # Usage is extracted above so this carries what the empty round cost.
        raise TransportError(
            "Claude CLI success envelope carried no `result` text; "
            "cannot parse reviewer JSON",
            kind="api",
            cost_usd=cost_usd,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )

    try:
        return parse_claude_response(
            raw_text,
            round_n=round_n,
            model=resolved_model,
            usage_input_tokens=tokens_input,
            usage_output_tokens=tokens_output,
            cost_usd=cost_usd,
        )
    except ReviewSchemaError as exc:
        # This response was paid for before it failed to parse, and D20 grants
        # it one retry — so the caller needs the spend attached or the round
        # under-reports by a whole call. Same contract as TransportError's
        # fields; the runner reads both with getattr.
        exc.cost_usd = cost_usd
        exc.tokens_input = tokens_input
        exc.tokens_output = tokens_output
        raise


def _parse_claude_envelope(stdout: str) -> dict:
    """Decode the `claude -p --output-format json` result envelope."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TransportError(
            "Claude CLI stdout is not a JSON result envelope — expected "
            "`--output-format json`. First 500 chars:\n"
            f"{stdout[:500]}"
        ) from exc
    if not isinstance(envelope, dict):
        raise TransportError(
            "Claude CLI `--output-format json` returned "
            f"{type(envelope).__name__}, expected an object"
        )
    return envelope


def _envelope_or_none(stdout: object) -> dict | None:
    """Decode `stdout` as a result envelope, or None when it isn't one."""
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return envelope if isinstance(envelope, dict) else None


def _raise_on_claude_error_envelope(
    envelope: dict, *, max_turns: int, max_budget: str, chosen_model: str
) -> None:
    """Raise a classified `TransportError` for any non-success envelope."""
    subtype = str(envelope.get("subtype") or "")
    is_error = bool(envelope.get("is_error"))
    if not is_error and subtype in ("", "success"):
        return

    # Usage BEFORE raising. A guard-truncated run did real work and the
    # envelope reports what it cost; raising bare would hide that from the
    # caller's per-round total, which is what the cost cap gates on. Only the
    # envelope's own figures are used here — the rate-table estimate needs a
    # resolved model id that an error envelope may not carry, and guessing is
    # worse than reporting nothing.
    spent, tok_in, tok_out = _reported_usage(envelope, chosen_model=chosen_model)

    # Truncation by OUR OWN guard: the run didn't fail, it ran out of the
    # allowance we set. Retryable so D20's retry-once applies (the retry
    # inherits the same caps, so two truncations surface to the operator).
    if subtype == "error_max_turns":
        raise TransportError(
            f"Claude review truncated by our own guard: --max-turns {max_turns} "
            "exhausted (raise ADVERSARIAL_CLAUDE_MAX_TURNS if this repeats).",
            kind="max_turns",
            cost_usd=spent,
            tokens_input=tok_in,
            tokens_output=tok_out,
        )
    detail = str(envelope.get("terminal_reason") or "").strip()
    if "budget" in subtype or "budget" in detail.lower():
        raise TransportError(
            f"Claude review truncated by our own guard: --max-budget-usd {max_budget} "
            "exhausted (raise ADVERSARIAL_CLAUDE_MAX_BUDGET_USD if this repeats).",
            kind="max_budget",
            cost_usd=spent,
            tokens_input=tok_in,
            tokens_output=tok_out,
        )

    lowered = f"{subtype} {detail}".lower()
    raise TransportError(
        f"Claude CLI returned an error envelope (subtype={subtype or 'unknown'!r}): "
        f"{detail or '(no terminal_reason reported)'}",
        kind="api" if any(m in lowered for m in _CLAUDE_TRANSIENT_MARKERS) else "permanent",
        cost_usd=spent,
        tokens_input=tok_in,
        tokens_output=tok_out,
    )


def _reported_usage(
    envelope: dict, *, chosen_model: str
) -> tuple[float | None, int, int]:
    """`(cost_usd, tokens_input, tokens_output)` for a failed Claude attempt.

    Cost resolution, in order:

    1. `total_cost_usd` — authoritative, and reported even on subscription
       sessions.
    2. Otherwise estimate from the reported tokens at the RESOLVED model's
       rate. The requested model is known at the call site, so `modelUsage`
       (or the alias fallback) can resolve it; this is the same path the
       success case already trusts.
    3. Otherwise `None` — genuinely unknown. Not 0.0: a zero would be
       indistinguishable from a free call and would quietly shrink the cost
       cap's denominator.
    """
    tok_in, tok_out = _extract_claude_usage(envelope)

    raw_cost = envelope.get("total_cost_usd")
    if isinstance(raw_cost, (int, float)):
        return float(raw_cost), tok_in, tok_out

    if tok_in or tok_out:
        resolved = _resolve_claude_model_id(envelope, fallback=chosen_model)
        estimated = estimate_cost_usd(resolved, tok_in, tok_out)
        # `estimate_cost_usd` returns 0.0 for a model with no rate row, which
        # is the unknown case again rather than a free one.
        if estimated:
            return estimated, tok_in, tok_out

    return None, tok_in, tok_out


def _resolve_claude_model_id(envelope: dict, *, fallback: str) -> str:
    """Resolve the model id actually used, never the `opus`/`sonnet` CLI alias.

    The alias must not reach the rate table or the sidecar: `modelUsage` is
    keyed by the resolved id and each entry carries `canonicalModel`. When the
    envelope carries no `modelUsage` at all, the fallback goes through
    `CLAUDE_MODEL_ALIASES` so the alias still never escapes.

    `modelUsage` can carry MULTIPLE models: the Task-6 live smoke (2026-08-11,
    claude 2.1.227) showed a `--model opus` run reporting BOTH
    `claude-haiku-4-5-…` (the CLI's internal auxiliary model) and
    `claude-opus-5`. Taking the first dict entry recorded haiku for an opus
    round. So: prefer the entry that matches the model we ASKED for; fall back
    to first-entry only when nothing matches (e.g. an alias the map doesn't
    know resolving to an id we can't predict).
    """
    requested = CLAUDE_MODEL_ALIASES.get(fallback.strip().lower(), fallback)

    def _entry_id(key: object, entry: object) -> str | None:
        if isinstance(entry, dict):
            canonical = entry.get("canonicalModel")
            if isinstance(canonical, str) and canonical:
                return canonical
        if isinstance(key, str) and key:
            return key
        return None

    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict):
        ids = [i for i in (_entry_id(k, e) for k, e in model_usage.items()) if i]
        for candidate in ids:  # exact/dated-variant match on the requested model wins
            if candidate == requested or candidate.startswith(f"{requested}-"):
                return candidate
        if ids:
            return ids[0]
    return requested


def _extract_claude_usage(envelope: dict) -> tuple[int, int]:
    """Return `(tokens_input, tokens_output)` from the envelope's `usage`.

    `tokens_input` sums input + cache-creation + cache-read (R1-M2): a probe
    reported `input_tokens: 9` while the cache fields carried ~37k on the SAME
    call, so reading `input_tokens` alone under-reports by three orders of
    magnitude and would make the cost cap meaningless.
    """
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return 0, 0

    def _field(name: str) -> int:
        value = usage.get(name)
        return int(value) if isinstance(value, (int, float)) else 0

    tokens_input = (
        _field("input_tokens")
        + _field("cache_creation_input_tokens")
        + _field("cache_read_input_tokens")
    )
    return tokens_input, _field("output_tokens")


def _claude_cost_usd(
    envelope: dict, *, resolved_model: str, tokens_input: int, tokens_output: int
) -> float:
    """Cost for one claude round.

    `total_cost_usd` is authoritative and is reported non-zero even on
    subscription sessions ($0.0384–0.0588 on trivial probe calls), so it is used
    unconditionally when present. The rate-table estimate is a fallback for a
    future CLI that stops reporting it, keyed on the RESOLVED model id.
    """
    reported = envelope.get("total_cost_usd")
    if isinstance(reported, (int, float)):
        return float(reported)
    return estimate_cost_usd(resolved_model, tokens_input, tokens_output)


# --- Codex CLI path ----------------------------------------------------------


def _invoke_codex(prompt: str, *, round_n: int, model: str | None) -> ReviewResult:
    """Send `prompt` to the Codex CLI via stdin (Windows-safe; §5.1.2 round-2 finding 3)."""
    chosen_model = model or os.environ.get("OPENAI_REVIEWER_MODEL", DEFAULT_CODEX_MODEL)

    cmd = _resolve_codex_command(chosen_model)
    try:
        # encoding="utf-8" is mandatory on Windows: prompts contain `§`,
        # em-dashes, and other non-cp1252 codepoints that would otherwise
        # silently mojibake or raise UnicodeEncodeError. errors="replace"
        # keeps a corrupt byte from killing the loop — the `Reviewer raw
        # response` block in fixes-md will surface any garbled content for
        # human review. Same applies to Codex's UTF-8 stdout. (Code-review
        # finding C1.)
        result = subprocess.run(
            cmd,
            input=prompt,  # stdin — bypasses argv length limits (Windows ~32KB)
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        stderr_excerpt = (exc.stderr or "")[-2000:]
        raise TransportError(
            f"Codex CLI exited {exc.returncode}. stderr tail:\n{stderr_excerpt}"
        ) from exc
    except FileNotFoundError as exc:
        raise TransportError(
            f"Codex CLI executable not found ({cmd[0]}). "
            "Install Codex or set ADVERSARIAL_TRANSPORT=openai."
        ) from exc

    return parse_codex_prose(
        result.stdout,
        round_n=round_n,
        model=chosen_model,
    )


def _resolve_codex_command(model: str) -> list[str]:
    """Pick between `codex exec` and the plugin's `codex-companion.mjs` wrapper.

    `codex exec` is the modern non-interactive entry point and is preferred
    when on PATH. The plugin wrapper is kept as a fallback for environments
    that have CLAUDE_PLUGIN_ROOT but not a global `codex` install.
    """
    if shutil.which("codex"):
        return ["codex", "exec", "-m", model]
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        wrapper = os.path.join(plugin_root, "scripts", "codex-companion.mjs")
        if os.path.isfile(wrapper):
            return ["node", wrapper, "task", "--model", model]
    raise TransportError(
        "Codex transport selected but neither `codex` CLI nor "
        "`codex-companion.mjs` is reachable."
    )
