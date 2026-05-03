"""Tests for scripts/reviewer.py — transport detection + error classification."""
from __future__ import annotations

import os

import pytest

from reviewer import (
    DEFAULT_OPENAI_MODEL,
    TransportError,
    TransportSelection,
    TransportUnavailableError,
    _is_codex_cli_available,
    _resolve_codex_command,
    detect_transport,
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


def test_detect_transport_invalid_explicit_value_raises():
    env = {"ADVERSARIAL_TRANSPORT": "anthropic"}
    with pytest.raises(TransportError, match="must be 'openai' or 'codex'"):
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


# --- TransportError classification ------------------------------------------


def test_transport_error_default_not_transient():
    err = TransportError("oops")
    assert err.is_transient is False


def test_transport_error_can_be_transient():
    err = TransportError("rate limited", is_transient=True)
    assert err.is_transient is True


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


def test_default_openai_model_is_gpt_55():
    """v2 default per §5.1.1 / D3."""
    assert DEFAULT_OPENAI_MODEL == "gpt-5.5"


# --- TransportSelection ------------------------------------------------------


def test_transport_selection_is_frozen():
    sel = TransportSelection(name="openai", reason="test")
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        sel.name = "codex"
