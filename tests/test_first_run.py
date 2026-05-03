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


def test_setup_guide_mentions_both_transports():
    text = setup_guide_text()
    assert "OpenAI API key" in text
    assert "Codex CLI" in text
    assert "platform.openai.com/api-keys" in text


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
