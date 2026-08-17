"""Tests for scripts/reviewer.py — transport detection + error classification."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

import reviewer
from reviewer import (
    CLAUDE_MODEL_ALIASES,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_OPENAI_MODEL,
    QuotaExhaustedError,
    TransportError,
    TransportSelection,
    TransportUnavailableError,
    _invoke_claude,
    _is_claude_cli_available,
    _is_codex_cli_available,
    _resolve_claude_model_id,
    _resolve_codex_command,
    detect_transport,
    invoke_reviewer,
)


# --- Transport detection priority -------------------------------------------


def test_detect_transport_explicit_openai_override():
    env = {"ADVERSARIAL_TRANSPORT": "openai"}
    sel = detect_transport(env=env)
    assert sel.name == "openai"
    assert "ADVERSARIAL_TRANSPORT" in sel.reason


def test_detect_transport_explicit_codex_override():
    env = {"ADVERSARIAL_TRANSPORT": "codex"}
    sel = detect_transport(env=env)
    assert sel.name == "codex"


def test_detect_transport_explicit_claude_override():
    env = {"ADVERSARIAL_TRANSPORT": "claude"}
    sel = detect_transport(env=env)
    assert sel.name == "claude"
    assert "ADVERSARIAL_TRANSPORT" in sel.reason


def test_detect_transport_invalid_explicit_value_raises():
    env = {"ADVERSARIAL_TRANSPORT": "gemini"}
    with pytest.raises(TransportError, match="must be 'openai', 'codex' or 'claude'"):
        detect_transport(env=env)


def test_detect_transport_anthropic_is_not_an_alias_for_claude():
    """R1-M3: `anthropic` rejects like any other unknown value (decided, not an alias)."""
    env = {"ADVERSARIAL_TRANSPORT": "anthropic"}
    with pytest.raises(TransportError, match="got 'anthropic'"):
        detect_transport(env=env)


def test_detect_transport_openai_when_key_set(empty_env):
    env = {**empty_env, "OPENAI_API_KEY": "sk-test"}
    sel = detect_transport(env=env)
    assert sel.name == "openai"
    assert "OPENAI_API_KEY" in sel.reason


def test_detect_transport_neither_available_raises(empty_env):
    """No env explicit, no OPENAI_API_KEY, no codex on PATH."""
    with pytest.raises(TransportUnavailableError):
        detect_transport(env=empty_env)


def test_detect_transport_picks_openai_over_codex_when_both_available(tmp_path):
    """Priority: explicit > openai > codex."""
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0")
    fake_codex.chmod(0o755)
    env = {
        "PATH": str(tmp_path),
        "PATHEXT": "",
        "OPENAI_API_KEY": "sk-test",
    }
    sel = detect_transport(env=env)
    assert sel.name == "openai"  # OPENAI_API_KEY wins


def test_detect_transport_codex_via_path(tmp_path):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0")
    fake_codex.chmod(0o755)
    env = {"PATH": str(tmp_path), "PATHEXT": ""}
    sel = detect_transport(env=env)
    assert sel.name == "codex"


def test_detect_transport_codex_via_pathext_on_windows(tmp_path):
    """PATHEXT-aware lookup: `codex.exe` on Windows."""
    fake_codex = tmp_path / "codex.exe"
    fake_codex.write_text("")
    env = {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT"}
    sel = detect_transport(env=env)
    assert sel.name == "codex"


def test_detect_transport_codex_via_plugin_root(tmp_path):
    """Plugin wrapper fallback when `codex` not on PATH."""
    plugin_root = tmp_path / "plugin"
    (plugin_root / "scripts").mkdir(parents=True)
    (plugin_root / "scripts" / "codex-companion.mjs").write_text("// stub")
    env = {
        "PATH": "",
        "PATHEXT": "",
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
    }
    sel = detect_transport(env=env)
    assert sel.name == "codex"


def test_is_codex_cli_available_hermetic(empty_env):
    """I1: a hermetic empty env should return False even when codex is on real PATH."""
    assert _is_codex_cli_available(empty_env) is False


def test_is_codex_cli_available_finds_codex(tmp_path):
    fake = tmp_path / "codex"
    fake.write_text("")
    fake.chmod(0o755)
    env = {"PATH": str(tmp_path), "PATHEXT": ""}
    assert _is_codex_cli_available(env) is True


# --- Claude CLI detection ----------------------------------------------------


def test_detect_transport_claude_via_path(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 0")
    fake.chmod(0o755)
    env = {"PATH": str(tmp_path), "PATHEXT": ""}
    sel = detect_transport(env=env)
    assert sel.name == "claude"
    assert "PATH" in sel.reason


def test_detect_transport_picks_openai_over_claude_when_both_available(tmp_path):
    """Priority: explicit > OPENAI_API_KEY > claude > codex."""
    (tmp_path / "claude").write_text("")
    (tmp_path / "claude").chmod(0o755)
    env = {"PATH": str(tmp_path), "PATHEXT": "", "OPENAI_API_KEY": "sk-test"}
    assert detect_transport(env=env).name == "openai"


def test_detect_transport_picks_claude_over_codex_when_both_available(tmp_path):
    """Claude outranks the legacy Codex CLI in the auto-detect ladder."""
    for name in ("claude", "codex"):
        (tmp_path / name).write_text("")
        (tmp_path / name).chmod(0o755)
    env = {"PATH": str(tmp_path), "PATHEXT": ""}
    assert detect_transport(env=env).name == "claude"


def test_is_claude_cli_available_hermetic(empty_env):
    """Hermetic env walk: empty PATH is False even when claude is on the real PATH."""
    assert _is_claude_cli_available(empty_env) is False


def test_is_claude_cli_available_via_pathext_on_windows(tmp_path):
    (tmp_path / "claude.exe").write_text("")
    env = {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT"}
    assert _is_claude_cli_available(env) is True


# --- Claude CLI invocation ---------------------------------------------------


CANNED_REVIEW_JSON = json.dumps(
    {"status": "NO_FINDINGS", "findings": [], "open_questions": []}
)


def _claude_envelope(**overrides):
    """A `claude -p --output-format json` result envelope (probed shape, 2.1.227)."""
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": CANNED_REVIEW_JSON,
        "duration_ms": 4200,
        "num_turns": 12,
        "total_cost_usd": 0.0384,
        "usage": {
            "input_tokens": 9,
            "output_tokens": 500,
            "cache_creation_input_tokens": 1200,
            "cache_read_input_tokens": 37000,
        },
        "modelUsage": {
            "claude-opus-5-20260101": {
                "canonicalModel": "claude-opus-5",
                "inputTokens": 9,
                "outputTokens": 500,
            }
        },
        "permission_denials": [],
        "terminal_reason": "end_turn",
    }
    envelope.update(overrides)
    return envelope


class _FakeRun:
    """Records the subprocess.run call and returns a canned CompletedProcess."""

    def __init__(self, stdout: str, *, raises: Exception | None = None) -> None:
        self.stdout = stdout
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(cmd, 0, stdout=self.stdout, stderr="")


@pytest.fixture
def clean_claude_env(monkeypatch):
    """Drop every ADVERSARIAL_CLAUDE_* / CLAUDE_REVIEWER_MODEL override."""
    for key in (
        "CLAUDE_REVIEWER_MODEL",
        "ADVERSARIAL_CLAUDE_TOOLS",
        "ADVERSARIAL_CLAUDE_TIMEOUT_S",
        "ADVERSARIAL_CLAUDE_MAX_TURNS",
        "ADVERSARIAL_CLAUDE_MAX_BUDGET_USD",
    ):
        monkeypatch.delenv(key, raising=False)


def test_invoke_claude_happy_path(monkeypatch, clean_claude_env, tmp_path):
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    result = _invoke_claude(
        "PROMPT BODY", round_n=1, model=None, repo_root=str(tmp_path)
    )

    assert result.status == "NO_FINDINGS"
    assert result.transport == "claude"
    assert result.model == "claude-opus-5"  # resolved id, not the `opus` alias
    call = fake.calls[0]
    # Prompt travels over stdin (Windows argv limits), cwd is the reviewed repo.
    assert call["input"] == "PROMPT BODY"
    assert call["cwd"] == str(tmp_path)
    assert call["timeout"] == 1200
    assert call["encoding"] == "utf-8"


def test_invoke_claude_argv_carries_containment_flags(
    monkeypatch, clean_claude_env, tmp_path
):
    """R1-H1/M4: containment is enforced by flags, not prose."""
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    cmd = fake.calls[0]["cmd"]

    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == DEFAULT_CLAUDE_MODEL
    # settings isolation: no inherited bypassPermissions / hooks / CLAUDE.md priors
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in cmd
    # The tool SET and the pre-granted subset are deliberately NOT the same.
    # Bash stays in the set so Claude Code's own read-only command recognition
    # can serve `git log` / `git show` / `stat` — the repo verification this
    # transport exists for — but pre-granting it would hand the reviewer a
    # write primitive that the prefix deny floor below cannot close
    # (`printf x > plan.md`, `sed -i`, `python -c`, `tee`). Ungranted means
    # escalation, and escalation under `--setting-sources ""` is a real denial.
    tool_set = cmd[cmd.index("--tools") + 1]
    pre_granted = cmd[cmd.index("--allowedTools") + 1]
    assert tool_set == "Read,Grep,Glob,Bash"
    assert pre_granted == "Read,Grep,Glob"
    assert "Bash" in tool_set.split(",")
    assert "Bash" not in pre_granted.split(",")
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    for denied in (
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Bash(git commit*)",
        "Bash(git push*)",
        "Bash(git reset*)",
        "Bash(git checkout*)",
        "Bash(git restore*)",
        "Bash(git stash*)",
        "Bash(rm -r*)",
        "Bash(sudo*)",
    ):
        assert denied in disallowed
    assert cmd[cmd.index("--max-budget-usd") + 1] == "5.0"


def test_env_cannot_widen_either_tool_value(monkeypatch, tmp_path):
    """`ADVERSARIAL_CLAUDE_TOOLS` states a request, not a grant.

    It is read from an environment `load_local_env()` populates from
    `<cwd>/.env` — and cwd is the repository under review — so a reviewed repo
    could otherwise smuggle tools into the reviewer inspecting it. Absent from
    `--allowedTools` is not enough: the containment probe showed Claude Code
    runs ungranted commands it classifies read-only, so an unexpected tool in
    the SET could execute without ever appearing in the grant.
    """
    monkeypatch.setenv(
        "ADVERSARIAL_CLAUDE_TOOLS", "Read,Grep,Glob,Bash,WebSearch,WebFetch,Agent"
    )
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    cmd = fake.calls[0]["cmd"]

    for smuggled in ("WebSearch", "WebFetch", "Agent"):
        assert smuggled not in cmd[cmd.index("--tools") + 1]
        assert smuggled not in cmd[cmd.index("--allowedTools") + 1]


def test_env_can_still_narrow_to_a_shell_free_reviewer(monkeypatch, tmp_path):
    """Narrowing is the supported direction, and it narrows the grant with it."""
    monkeypatch.setenv("ADVERSARIAL_CLAUDE_TOOLS", "Read,Grep,Glob")
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    cmd = fake.calls[0]["cmd"]

    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep,Glob"


def test_disjoint_tool_request_fails_closed(monkeypatch, tmp_path):
    """An empty intersection must fail loudly, never degrade to a broader set."""
    monkeypatch.setenv("ADVERSARIAL_CLAUDE_TOOLS", "Write,Edit")
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    with pytest.raises(reviewer.TransportError, match="requested no tool"):
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert fake.calls == []  # never reached the CLI


def test_invoke_claude_pins_max_turns_flag(monkeypatch, clean_claude_env, tmp_path):
    """`--max-turns` is enforced but ABSENT from `claude --help` on 2.1.227.

    Silent-removal canary (R1-H4): if the CLI drops the flag, the Task-6 live
    smoke fails loudly — this test pins that the argv builder still emits it.
    """
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    cmd = fake.calls[0]["cmd"]
    assert cmd[cmd.index("--max-turns") + 1] == "120"


def test_invoke_claude_env_knobs_override_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_REVIEWER_MODEL", "sonnet")
    monkeypatch.setenv("ADVERSARIAL_CLAUDE_TOOLS", "Read,Grep,Glob")
    monkeypatch.setenv("ADVERSARIAL_CLAUDE_TIMEOUT_S", "600")
    monkeypatch.setenv("ADVERSARIAL_CLAUDE_MAX_TURNS", "40")
    monkeypatch.setenv("ADVERSARIAL_CLAUDE_MAX_BUDGET_USD", "1.5")
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))

    cmd = fake.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
    assert cmd[cmd.index("--max-turns") + 1] == "40"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.5"
    assert fake.calls[0]["timeout"] == 600


def test_invoke_claude_explicit_model_argument_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_REVIEWER_MODEL", "sonnet")
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    _invoke_claude("p", round_n=1, model="opus", repo_root=str(tmp_path))
    cmd = fake.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_invoke_claude_cost_comes_from_total_cost_usd(
    monkeypatch, clean_claude_env, tmp_path
):
    """R1-M2: total_cost_usd is non-zero even on subscription sessions — record it."""
    fake = _FakeRun(json.dumps(_claude_envelope(total_cost_usd=0.0588)))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    result = _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert result.usage.cost_usd == pytest.approx(0.0588)


def test_invoke_claude_tokens_input_sums_cache_fields(
    monkeypatch, clean_claude_env, tmp_path
):
    """R1-M2: input_tokens was 9 while cache fields carried ~37k on the same call."""
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    result = _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert result.usage.tokens_input == 9 + 1200 + 37000
    assert result.usage.tokens_output == 500


def test_invoke_claude_estimate_fallback_keyed_on_canonical_model(
    monkeypatch, clean_claude_env, tmp_path
):
    """Estimate ONLY when total_cost_usd is absent, keyed on the resolved id."""
    envelope = _claude_envelope()
    del envelope["total_cost_usd"]
    fake = _FakeRun(json.dumps(envelope))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    result = _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    tokens_input = 9 + 1200 + 37000
    expected = (tokens_input * 5.0 + 500 * 25.0) / 1_000_000  # claude-opus-5 rates
    assert result.usage.cost_usd == pytest.approx(expected)


def test_invoke_claude_error_max_turns_is_transient_and_names_budget(
    monkeypatch, clean_claude_env, tmp_path
):
    """R1-H4: is_error/subtype are checked BEFORE result, which is null on errors."""
    envelope = _claude_envelope(
        subtype="error_max_turns", is_error=True, result=None
    )
    fake = _FakeRun(json.dumps(envelope))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError) as exc:
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert exc.value.is_transient is True
    assert "--max-turns 120" in str(exc.value)


def test_invoke_claude_budget_exhaustion_is_transient(
    monkeypatch, clean_claude_env, tmp_path
):
    envelope = _claude_envelope(
        subtype="error_max_budget_usd", is_error=True, result=None
    )
    fake = _FakeRun(json.dumps(envelope))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError) as exc:
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert exc.value.is_transient is True
    assert "--max-budget-usd 5.0" in str(exc.value)


def test_invoke_claude_generic_error_envelope_is_permanent(
    monkeypatch, clean_claude_env, tmp_path
):
    envelope = _claude_envelope(
        subtype="error_during_execution",
        is_error=True,
        result=None,
        terminal_reason="invalid request",
    )
    fake = _FakeRun(json.dumps(envelope))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError) as exc:
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert exc.value.is_transient is False


def test_invoke_claude_overload_error_envelope_is_transient(
    monkeypatch, clean_claude_env, tmp_path
):
    envelope = _claude_envelope(
        subtype="error_during_execution",
        is_error=True,
        result=None,
        terminal_reason="API overloaded_error, please retry",
    )
    fake = _FakeRun(json.dumps(envelope))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError) as exc:
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert exc.value.is_transient is True


def test_invoke_claude_timeout_is_not_retried(monkeypatch, clean_claude_env, tmp_path):
    """A wall-clock timeout is a distinct kind and must NOT be retried.

    It used to be lumped in with rate limits under one `is_transient` boolean.
    Retrying inherits the same timeout, so a single round would spend
    2 x ADVERSARIAL_CLAUDE_TIMEOUT_S — 40 minutes at the default — and still
    fail.
    """
    fake = _FakeRun("", raises=subprocess.TimeoutExpired(cmd=["claude"], timeout=1200))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError) as exc:
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert exc.value.kind == "wall_timeout"
    assert exc.value.is_transient is False
    assert "1200" in str(exc.value)


def test_invoke_claude_called_process_error_mirrors_codex_path(
    monkeypatch, clean_claude_env, tmp_path
):
    err = subprocess.CalledProcessError(2, ["claude"], stderr="boom stderr tail")
    fake = _FakeRun("", raises=err)
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError, match="boom stderr tail"):
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))


def test_invoke_claude_classifies_error_envelope_on_nonzero_exit(
    monkeypatch, clean_claude_env, tmp_path
):
    """A truncated run may ALSO exit non-zero — don't lose the transient hint."""
    envelope = _claude_envelope(subtype="error_max_turns", is_error=True, result=None)
    err = subprocess.CalledProcessError(
        1, ["claude"], output=json.dumps(envelope), stderr=""
    )
    fake = _FakeRun("", raises=err)
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError) as exc:
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert exc.value.is_transient is True
    assert "--max-turns 120" in str(exc.value)


def test_invoke_claude_missing_executable_is_actionable(
    monkeypatch, clean_claude_env, tmp_path
):
    fake = _FakeRun("", raises=FileNotFoundError("claude"))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError, match="not found"):
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))


def test_invoke_claude_unparseable_envelope_raises_transport_error(
    monkeypatch, clean_claude_env, tmp_path
):
    fake = _FakeRun("not json at all")
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    with pytest.raises(TransportError, match="--output-format json"):
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))


# --- Claude model-id resolution (alias must never escape) ---------------------


def test_invoke_claude_without_model_usage_records_the_canonical_id(
    monkeypatch, clean_claude_env, tmp_path
):
    """No `modelUsage` → resolve the `opus` alias, don't record the alias itself."""
    envelope = _claude_envelope()
    del envelope["modelUsage"]
    fake = _FakeRun(json.dumps(envelope))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    result = _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert result.model == "claude-opus-5"


def test_invoke_claude_model_usage_still_wins_over_the_alias_map(
    monkeypatch, clean_claude_env, tmp_path
):
    """The envelope is authoritative: a sonnet run reported as such is recorded so."""
    monkeypatch.setenv("CLAUDE_REVIEWER_MODEL", "opus")
    envelope = _claude_envelope(
        modelUsage={
            "claude-sonnet-5-20260101": {
                "canonicalModel": "claude-sonnet-5",
                "inputTokens": 9,
                "outputTokens": 500,
            }
        }
    )
    fake = _FakeRun(json.dumps(envelope))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    result = _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))
    assert result.model == "claude-sonnet-5"


def test_invoke_claude_alias_fallback_dodges_the_openai_rate_override(
    monkeypatch, clean_claude_env, tmp_path
):
    """An `opus` alias slipped past cost_tracker's gate and got OpenAI rates."""
    monkeypatch.setenv("OPENAI_INPUT_USD_PER_1M", "999.0")
    monkeypatch.setenv("OPENAI_OUTPUT_USD_PER_1M", "999.0")
    envelope = _claude_envelope()
    del envelope["modelUsage"]
    del envelope["total_cost_usd"]  # force the rate-table estimate path
    fake = _FakeRun(json.dumps(envelope))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    result = _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))

    tokens_input = 9 + 1200 + 37000
    expected = (tokens_input * 5.0 + 500 * 25.0) / 1_000_000  # claude-opus-5 rates
    assert result.usage.cost_usd == pytest.approx(expected)


@pytest.mark.parametrize(
    "alias,canonical",
    [("opus", "claude-opus-5"), ("sonnet", "claude-sonnet-5"), ("fable", "claude-fable-5")],
)
def test_claude_model_aliases_cover_the_documented_cli_aliases(alias, canonical):
    assert CLAUDE_MODEL_ALIASES[alias] == canonical


def test_resolve_claude_model_id_passes_unknown_values_through():
    """An id we don't map (or a future alias) is recorded verbatim, not guessed."""
    assert (
        _resolve_claude_model_id({}, fallback="claude-opus-5-20260101")
        == "claude-opus-5-20260101"
    )
    assert _resolve_claude_model_id({}, fallback="haiku") == "haiku"


def test_resolve_claude_model_id_prefers_the_requested_model_over_aux_entries():
    """Task-6 live-smoke regression (2026-08-11): a `--model opus` envelope on
    claude 2.1.227 carried the CLI's internal haiku helper FIRST in modelUsage —
    first-entry resolution recorded haiku for an opus round. The entry matching
    the REQUESTED model must win regardless of dict order."""
    envelope = {
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"canonicalModel": "claude-haiku-4-5"},
            "claude-opus-5": {"canonicalModel": "claude-opus-5"},
        }
    }
    assert _resolve_claude_model_id(envelope, fallback="opus") == "claude-opus-5"


def test_resolve_claude_model_id_matches_dated_variants_of_the_requested_model():
    envelope = {"modelUsage": {"claude-opus-5-20260101": {}}}
    assert (
        _resolve_claude_model_id(envelope, fallback="opus") == "claude-opus-5-20260101"
    )


def test_resolve_claude_model_id_first_entry_when_nothing_matches():
    """Only aux entries present (nothing matching the request): record what ran
    rather than guessing from the alias — the envelope is the ground truth."""
    envelope = {
        "modelUsage": {"claude-haiku-4-5-20251001": {"canonicalModel": "claude-haiku-4-5"}}
    }
    assert _resolve_claude_model_id(envelope, fallback="opus") == "claude-haiku-4-5"


# --- Claude env-knob validation ----------------------------------------------


@pytest.mark.parametrize(
    "env_var,bad_value",
    [
        ("ADVERSARIAL_CLAUDE_MAX_TURNS", "lots"),
        ("ADVERSARIAL_CLAUDE_MAX_TURNS", "12.5"),
        ("ADVERSARIAL_CLAUDE_TIMEOUT_S", "20 minutes"),
        ("ADVERSARIAL_CLAUDE_MAX_BUDGET_USD", "$5"),
    ],
)
def test_invoke_claude_malformed_env_knob_raises_named_transport_error(
    monkeypatch, clean_claude_env, tmp_path, env_var, bad_value
):
    """A typo in .env must name the var and the value, not raise a bare ValueError."""
    monkeypatch.setenv(env_var, bad_value)
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)

    with pytest.raises(TransportError) as exc:
        _invoke_claude("p", round_n=1, model=None, repo_root=str(tmp_path))

    message = str(exc.value)
    assert env_var in message
    assert bad_value in message
    assert exc.value.is_transient is False
    # Fail before spawning the CLI — a bad cap must never reach the subprocess.
    assert fake.calls == []


def test_invoke_reviewer_threads_repo_root_to_claude(
    monkeypatch, clean_claude_env, tmp_path
):
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    invoke_reviewer(
        "p",
        round_n=1,
        transport=TransportSelection("claude", "test"),
        repo_root=str(tmp_path),
    )
    assert fake.calls[0]["cwd"] == str(tmp_path)


def test_invoke_reviewer_defaults_repo_root_to_cwd(
    monkeypatch, clean_claude_env, tmp_path
):
    monkeypatch.chdir(tmp_path)
    fake = _FakeRun(json.dumps(_claude_envelope()))
    monkeypatch.setattr(reviewer.subprocess, "run", fake)
    invoke_reviewer("p", round_n=1, transport=TransportSelection("claude", "test"))
    assert fake.calls[0]["cwd"] == os.getcwd()


# --- Quota exhaustion (runtime fallback is the orchestration's job) ----------


def test_quota_exhausted_error_is_a_transport_error():
    err = QuotaExhaustedError("out of credit")
    assert isinstance(err, TransportError)
    assert err.is_transient is False


def test_invoke_reviewer_propagates_quota_exhausted(monkeypatch):
    """R1-H3: no silent in-module fallback — the prompt was built for openai."""

    def boom(prompt, *, round_n, model):
        raise QuotaExhaustedError("insufficient_quota")

    monkeypatch.setattr(reviewer, "_invoke_openai", boom)
    with pytest.raises(QuotaExhaustedError):
        invoke_reviewer(
            "p", round_n=1, transport=TransportSelection("openai", "test")
        )


class _FakeApiError(Exception):
    """Stand-in for an SDK error object carrying a machine-readable code/body."""

    def __init__(self, message, *, code=None, body=None):
        super().__init__(message)
        self.code = code
        self.body = body


@pytest.mark.parametrize(
    "exc",
    [
        _FakeApiError("Rate limit reached", code="insufficient_quota"),
        _FakeApiError(
            "429",
            body={"error": {"code": "insufficient_quota", "message": "no quota"}},
        ),
        _FakeApiError("You exceeded your current quota, please check your plan"),
        _FakeApiError("Your credit balance is too low to access the API"),
        _FakeApiError("billing_hard_limit_reached"),
    ],
)
def test_openai_quota_error_classifier_matches_quota_class_errors(exc):
    assert reviewer._is_openai_quota_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _FakeApiError("Rate limit reached for gpt-5.6-sol", code="rate_limit_exceeded"),
        _FakeApiError("Incorrect API key provided", code="invalid_api_key"),
        _FakeApiError("The server had an error while processing your request"),
    ],
)
def test_openai_quota_error_classifier_ignores_ordinary_failures(exc):
    """A plain 429 must stay transient — retrying it can succeed."""
    assert reviewer._is_openai_quota_error(exc) is False


def test_invoke_openai_maps_quota_error_to_quota_exhausted(monkeypatch):
    """A quota 429 is a RateLimitError: without this it'd burn D20's retry."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    quota_error = openai.RateLimitError(
        "You exceeded your current quota",
        response=httpx.Response(429, request=request),
        body={"error": {"code": "insufficient_quota"}},
    )

    class _FakeClient:
        def __init__(self):
            self.responses = self

        def create(self, **kwargs):
            raise quota_error

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _FakeClient())

    with pytest.raises(QuotaExhaustedError, match="quota"):
        reviewer._invoke_openai("p", round_n=1, model="gpt-5.6-sol")


def test_invoke_openai_plain_rate_limit_stays_transient(monkeypatch):
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    rate_limited = openai.RateLimitError(
        "Rate limit reached for gpt-5.6-sol in organization org-x",
        response=httpx.Response(429, request=request),
        body={"error": {"code": "rate_limit_exceeded"}},
    )

    class _FakeClient:
        def __init__(self):
            self.responses = self

        def create(self, **kwargs):
            raise rate_limited

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _FakeClient())

    with pytest.raises(TransportError) as exc:
        reviewer._invoke_openai("p", round_n=1, model="gpt-5.6-sol")
    assert not isinstance(exc.value, QuotaExhaustedError)
    assert exc.value.is_transient is True


# --- TransportError classification ------------------------------------------


def test_transport_error_default_not_transient():
    err = TransportError("oops")
    assert err.is_transient is False


def test_transport_error_kind_drives_the_retry_contract():
    """`is_transient` is derived from `kind`, never set independently."""
    assert TransportError("rate limited", kind="api").is_transient is True
    assert TransportError("turns used up", kind="max_turns").is_transient is True
    assert TransportError("budget used up", kind="max_budget").is_transient is True
    assert TransportError("timed out", kind="wall_timeout").is_transient is False
    assert TransportError("bad key", kind="permanent").is_transient is False
    assert TransportError("no credit", kind="quota").is_transient is False


def test_transport_error_rejects_an_unknown_kind():
    """A typo must not silently land in the non-retryable default."""
    with pytest.raises(ValueError, match="unknown TransportError kind"):
        TransportError("boom", kind="transient")


def test_transport_error_is_transient_is_read_only():
    """The boolean was assignable before; that is what let policies diverge."""
    err = TransportError("timed out", kind="wall_timeout")
    with pytest.raises(AttributeError):
        err.is_transient = True  # type: ignore[misc]


def test_transport_unavailable_is_not_transient():
    err = TransportUnavailableError("nothing configured")
    assert err.is_transient is False
    assert isinstance(err, TransportError)


# --- Codex command resolution ------------------------------------------------


def test_resolve_codex_command_uses_codex_when_on_path(tmp_path, monkeypatch):
    """Cross-platform: write both `codex` and `codex.exe` so shutil.which finds it."""
    (tmp_path / "codex").write_bytes(b"")
    (tmp_path / "codex").chmod(0o755)
    (tmp_path / "codex.exe").write_bytes(b"")  # for Windows shutil.which
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    cmd = _resolve_codex_command("gpt-5.5")
    assert cmd[0] == "codex"
    assert cmd[1] == "exec"
    assert "-m" in cmd and "gpt-5.5" in cmd


def test_resolve_codex_command_falls_back_to_companion(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin"
    (plugin_root / "scripts").mkdir(parents=True)
    wrapper = plugin_root / "scripts" / "codex-companion.mjs"
    wrapper.write_text("// stub")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    cmd = _resolve_codex_command("gpt-5.5")
    assert cmd[0] == "node"
    assert str(wrapper) in cmd
    assert "task" in cmd
    assert "--model" in cmd and "gpt-5.5" in cmd


def test_resolve_codex_command_neither_path_nor_companion(monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    with pytest.raises(TransportError, match="neither.*reachable"):
        _resolve_codex_command("gpt-5.5")


# --- Default model -----------------------------------------------------------


def test_default_openai_model_is_gpt_56_sol():
    """July 2026 flagship; bumped from the v2-launch gpt-5.5 default (deployed 2026-08)."""
    assert DEFAULT_OPENAI_MODEL == "gpt-5.6-sol"


def test_default_claude_model_is_the_opus_alias():
    """CLI alias — the resolved id comes back in modelUsage.canonicalModel."""
    assert DEFAULT_CLAUDE_MODEL == "opus"


# --- TransportSelection ------------------------------------------------------


def test_transport_selection_is_frozen():
    sel = TransportSelection(name="openai", reason="test")
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        sel.name = "codex"
