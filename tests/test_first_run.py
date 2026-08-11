"""Tests for scripts/first_run.py — env writes + transport check."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from first_run import (
    FirstRunRequired,
    FirstRunStatus,
    _is_codex_cli_available,
    check_or_prompt,
    save_openai_key_to_env,
    setup_guide_text,
)


# --- save_openai_key_to_env: idempotent ---------------------------------------


def test_save_creates_new_env_file(tmp_path):
    env_path = tmp_path / ".env"
    save_openai_key_to_env("sk-test123", env_path=env_path)
    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-test123\n"


def test_save_replaces_existing_key_in_place(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FOO=bar\nOPENAI_API_KEY=sk-old\nOTHER=baz\n", encoding="utf-8"
    )
    save_openai_key_to_env("sk-new", env_path=env_path)
    contents = env_path.read_text(encoding="utf-8")
    assert "sk-new" in contents
    assert "sk-old" not in contents
    # Other lines preserved verbatim
    assert "FOO=bar\n" in contents
    assert "OTHER=baz\n" in contents


def test_save_appends_when_key_missing(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=bar\n", encoding="utf-8")
    save_openai_key_to_env("sk-test", env_path=env_path)
    contents = env_path.read_text(encoding="utf-8")
    assert "FOO=bar" in contents
    assert "OPENAI_API_KEY=sk-test" in contents


def test_save_appends_with_newline_when_existing_lacks_trailing_newline(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=bar", encoding="utf-8")  # no trailing \n
    save_openai_key_to_env("sk-test", env_path=env_path)
    contents = env_path.read_text(encoding="utf-8")
    assert contents == "FOO=bar\nOPENAI_API_KEY=sk-test\n"


# --- save_openai_key_to_env: input validation --------------------------------


def test_save_rejects_empty_key(tmp_path):
    with pytest.raises(ValueError, match="cannot be empty"):
        save_openai_key_to_env("", env_path=tmp_path / ".env")


def test_save_rejects_whitespace_only_key(tmp_path):
    with pytest.raises(ValueError, match="cannot be empty"):
        save_openai_key_to_env("   ", env_path=tmp_path / ".env")


def test_save_rejects_newline_in_key(tmp_path):
    with pytest.raises(ValueError, match="cannot contain newlines"):
        save_openai_key_to_env("sk-test\nMALICIOUS=injection", env_path=tmp_path / ".env")


def test_save_rejects_carriage_return_in_key(tmp_path):
    with pytest.raises(ValueError, match="cannot contain newlines"):
        save_openai_key_to_env("sk-test\rinjection", env_path=tmp_path / ".env")


def test_save_strips_surrounding_whitespace(tmp_path):
    env_path = tmp_path / ".env"
    save_openai_key_to_env("  sk-test  ", env_path=env_path)
    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-test\n"


def test_save_returns_path(tmp_path):
    env_path = tmp_path / ".env"
    returned = save_openai_key_to_env("sk-test", env_path=env_path)
    assert returned == env_path


# --- save_openai_key_to_env: gitignore touch --------------------------------


def test_save_appends_dotenv_to_gitignore_if_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.log\n", encoding="utf-8")
    save_openai_key_to_env("sk-test", env_path=tmp_path / ".env")
    assert ".env" in gitignore.read_text(encoding="utf-8").splitlines()


def test_save_does_not_duplicate_dotenv_in_gitignore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".env\n*.log\n", encoding="utf-8")
    save_openai_key_to_env("sk-test", env_path=tmp_path / ".env")
    contents = gitignore.read_text(encoding="utf-8")
    assert contents.count(".env") == 1  # not duplicated


def test_save_skips_gitignore_when_absent(tmp_path, monkeypatch):
    """Don't create a fresh .gitignore — only touch existing one."""
    monkeypatch.chdir(tmp_path)
    save_openai_key_to_env("sk-test", env_path=tmp_path / ".env")
    assert not (tmp_path / ".gitignore").exists()


# --- check_or_prompt --------------------------------------------------------


def test_check_or_prompt_returns_status_when_openai_set(empty_env):
    env = {**empty_env, "OPENAI_API_KEY": "sk-test"}
    status = check_or_prompt(env=env)
    assert status.ready is True
    assert status.has_openai_key is True
    assert status.transport.name == "openai"


def test_check_or_prompt_raises_when_nothing_configured(empty_env):
    with pytest.raises(FirstRunRequired):
        check_or_prompt(env=empty_env)


def test_check_or_prompt_reports_claude_when_only_claude(tmp_path):
    """`ready` is any transport — a Claude Code login alone is enough."""
    fake = tmp_path / "claude"
    fake.write_text("")
    fake.chmod(0o755)
    (tmp_path / "claude.exe").write_text("")
    env = {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE"}
    status = check_or_prompt(env=env)
    assert status.ready is True
    assert status.transport.name == "claude"
    assert status.has_claude is True
    assert status.has_openai_key is False
    assert status.has_codex is False


def test_check_or_prompt_lists_every_available_transport(tmp_path):
    for name in ("claude", "codex", "claude.exe", "codex.exe"):
        (tmp_path / name).write_text("")
    env = {
        "PATH": str(tmp_path),
        "PATHEXT": ".COM;.EXE",
        "OPENAI_API_KEY": "sk-test",
    }
    status = check_or_prompt(env=env)
    assert status.transport.name == "openai"  # detection order unchanged
    assert status.available_transports == ["openai", "claude", "codex"]


def test_check_or_prompt_has_claude_false_in_hermetic_env(empty_env):
    """The claude probe honours the injected PATH, not the real one."""
    env = {**empty_env, "OPENAI_API_KEY": "sk-test"}
    assert check_or_prompt(env=env).has_claude is False


def test_check_or_prompt_returns_codex_when_only_codex(tmp_path):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("")
    fake_codex.chmod(0o755)
    (tmp_path / "codex.exe").write_text("")
    env = {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE"}
    status = check_or_prompt(env=env)
    assert status.transport.name == "codex"
    assert status.has_openai_key is False
    assert status.has_codex is True


# --- _is_codex_cli_available hermetic ---------------------------------------


def test_is_codex_cli_available_hermetic_empty_path():
    """I1: hermetic env (empty PATH) returns False even when codex on real PATH."""
    assert _is_codex_cli_available({"PATH": "", "PATHEXT": ""}) is False


def test_is_codex_cli_available_via_pathext(tmp_path):
    (tmp_path / "codex.exe").write_text("")
    env = {"PATH": str(tmp_path), "PATHEXT": ".EXE"}
    assert _is_codex_cli_available(env) is True


def test_is_codex_cli_available_via_plugin_root(tmp_path):
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "codex-companion.mjs").write_text("// stub")
    env = {"PATH": "", "PATHEXT": "", "CLAUDE_PLUGIN_ROOT": str(plugin)}
    assert _is_codex_cli_available(env) is True


# --- setup_guide_text -------------------------------------------------------


def test_setup_guide_mentions_all_three_transports():
    text = setup_guide_text()
    assert "OpenAI API key" in text
    assert "Claude Code CLI" in text
    assert "Codex CLI" in text
    assert "platform.openai.com/api-keys" in text


def test_setup_guide_claude_section_says_no_key_and_how_to_force():
    text = setup_guide_text()
    assert "subscription" in text
    assert "NO API key" in text
    assert "ADVERSARIAL_TRANSPORT=claude" in text


# --- CLI --check entry point ------------------------------------------------


def test_cli_check_exits_2_when_unconfigured(tmp_path, monkeypatch):
    """`first_run.py --check` must exit 2 when no transport available."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PATHEXT", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ADVERSARIAL_TRANSPORT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "first_run.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 2
    assert "first-run setup required" in result.stderr.lower() or "not configured" in result.stderr.lower()


def test_cli_check_exits_0_when_openai_key_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "first_run.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0
    assert "openai" in result.stdout.lower()


# --- FirstRunStatus dataclass -----------------------------------------------


def test_first_run_status_ready_property():
    from reviewer import TransportSelection

    status = FirstRunStatus(
        transport=TransportSelection(name="openai", reason="test"),
        has_openai_key=True,
        has_codex=False,
    )
    assert status.ready is True

    blank = FirstRunStatus(transport=None, has_openai_key=False, has_codex=False)
    assert blank.ready is False


def test_first_run_status_ready_for_claude_only():
    """`ready` = any transport, so a claude-only host is ready."""
    from reviewer import TransportSelection

    status = FirstRunStatus(
        transport=TransportSelection(name="claude", reason="Claude CLI on PATH"),
        has_openai_key=False,
        has_codex=False,
        has_claude=True,
    )
    assert status.ready is True
    assert status.available_transports == ["claude"]


# --- CLI --check names the transports found ----------------------------------


def test_cli_check_names_available_transports(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "first_run.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0
    assert "transports available:" in result.stdout
