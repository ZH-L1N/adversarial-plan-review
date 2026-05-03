"""Transport abstraction for the adversarial plan reviewer.

Two paths converge on the same `ReviewResult` shape (defined in
`parse_review.py`):

- **OpenAI Responses API** (`gpt-5.5` by default) — JSON schema enforcement
  via the strict structured-output mode. The recommended path.
- **Codex CLI** (`codex-companion.mjs task`) — v1-compatible prose path used
  when no `OPENAI_API_KEY` is configured. Severity is inferred via keyword
  heuristic; no schema enforcement.

Transport selection follows v2-plan §5.1:
  1. `ADVERSARIAL_TRANSPORT=openai|codex` env var, if set
  2. Otherwise auto-detect: `OPENAI_API_KEY` → openai, else Codex CLI
  3. Otherwise raise so the first-run UX (`first_run.py`) can take over

Phase 1+2 scope: this module replaces the v1 ad-hoc Codex invocation in
SKILL.md but keeps v1 loop logic intact. Severity-gated exit, diff-aware
prompts, and resume support land in Phase 3+4.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from _dotenv import load_local_env
from parse_review import (
    REVIEW_SCHEMA,
    ReviewResult,
    ReviewSchemaError,
    parse_codex_prose,
    parse_openai_response,
)
from cost_tracker import estimate_cost_usd

# Populate os.environ from .env (shell values win) so OPENAI_API_KEY and
# friends are visible whether the user exported them or just wrote them to
# the skill's .env file.
load_local_env()

DEFAULT_OPENAI_MODEL = "gpt-5.5"
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_MAX_OUTPUT_TOKENS = 8000


# --- Errors ------------------------------------------------------------------


class TransportError(RuntimeError):
    """Reviewer transport could not complete the request.

    Distinct from `ReviewSchemaError` — this means the network/CLI call itself
    failed, not that the response was malformed. Callers may retry or escalate
    based on which error type they see.

    `is_transient` (code-review finding I3): hint to callers whether the
    underlying error class suggests a retryable transient (rate limit,
    network blip, server-side overload) vs a permanent issue
    (auth failure, malformed request). The retry-once-then-fail policy at
    v2-plan D20 only applies to transient errors.
    """

    def __init__(self, message: str, *, is_transient: bool = False) -> None:
        super().__init__(message)
        self.is_transient = is_transient


class TransportUnavailableError(TransportError):
    """No reviewer transport is configured.

    The first-run UX in `first_run.py` should have already prompted the user
    before any reviewer call reaches this module. If we still see this, the
    pre-flight ordering (§5.0a step 2) was bypassed — fail loud.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, is_transient=False)


# --- Transport detection -----------------------------------------------------


@dataclass(frozen=True)
class TransportSelection:
    name: str  # "openai" | "codex"
    reason: str  # Human-readable explanation, surfaced in logs


def detect_transport(*, env: dict[str, str] | None = None) -> TransportSelection:
    """Pick the active reviewer transport.

    Priority per v2-plan §5.1:
      1. `ADVERSARIAL_TRANSPORT` explicit override
      2. `OPENAI_API_KEY` set → openai
      3. Codex CLI on PATH → codex
      4. raise `TransportUnavailableError`
    """
    env = dict(os.environ if env is None else env)
    explicit = (env.get("ADVERSARIAL_TRANSPORT") or "").strip().lower()
    if explicit == "openai":
        return TransportSelection("openai", "ADVERSARIAL_TRANSPORT=openai")
    if explicit == "codex":
        return TransportSelection("codex", "ADVERSARIAL_TRANSPORT=codex")
    if explicit:
        raise TransportError(
            f"ADVERSARIAL_TRANSPORT must be 'openai' or 'codex', got '{explicit}'"
        )

    if env.get("OPENAI_API_KEY"):
        return TransportSelection("openai", "OPENAI_API_KEY is set")
    if _is_codex_cli_available(env):
        return TransportSelection("codex", "Codex CLI on PATH")

    raise TransportUnavailableError(
        "No reviewer transport configured. "
        "Set OPENAI_API_KEY (recommended) or install the Codex CLI; "
        "see the first-run UX (§5.6 of plans/v2-plan.md) for details."
    )


def _is_codex_cli_available(env: dict[str, str]) -> bool:
    """Hermetic codex-availability check using the injected env's PATH.

    Code-review finding I1: previously used `shutil.which("codex")` which
    always reads the live `os.environ["PATH"]`, defeating the env-injection
    contract that `detect_transport(env=...)` advertises. Now walks
    `env["PATH"]` (and PATHEXT on Windows) manually.
    """
    path_env = env.get("PATH", "")
    pathext = env.get("PATHEXT", "")
    extensions = (
        [""] + [ext.lower() for ext in pathext.split(os.pathsep) if ext]
        if pathext
        else [""]
    )
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        for ext in extensions:
            candidate = os.path.join(directory, f"codex{ext}")
            if os.path.isfile(candidate):
                return True

    plugin_root = env.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        wrapper = os.path.join(plugin_root, "scripts", "codex-companion.mjs")
        if os.path.isfile(wrapper):
            return True
    return False


# --- Public entry point ------------------------------------------------------


def invoke_reviewer(
    prompt: str,
    *,
    round_n: int,
    model: str | None = None,
    transport: TransportSelection | None = None,
) -> ReviewResult:
    """Run the reviewer against `prompt`, return parsed `ReviewResult`.

    `round_n` is required because the post-parse `assign_open_question_ids()`
    helper needs it to mint the `oq_r{round}_{idx}` identifiers (§5.2).
    """
    if transport is None:
        transport = detect_transport()

    if transport.name == "openai":
        return _invoke_openai(prompt, round_n=round_n, model=model)
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
            is_transient=is_transient,
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
    except ReviewSchemaError:
        # Schema violations are surfaced to the caller for the retry-once policy
        # (D20). Annotate so logs show which transport produced the bad output.
        raise


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
