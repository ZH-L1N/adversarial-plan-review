"""Tests for the minimal .env loader."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from _dotenv import _parse_env_file, load_local_env


def test_parse_simple(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=bar\nBAZ=qux\n")
    assert _parse_env_file(env) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_ignores_comments_and_blanks(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# heading comment\n"
        "\n"
        "FOO=bar\n"
        "  # indented comment\n"
        "BAZ=qux\n"
    )
    assert _parse_env_file(env) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_strips_quotes_and_export(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "export FOO=bar\n"
        'BAZ="quoted value"\n'
        "QUX='single quoted'\n"
    )
    assert _parse_env_file(env) == {
        "FOO": "bar",
        "BAZ": "quoted value",
        "QUX": "single quoted",
    }


def test_parse_skips_malformed_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("=missing_key\nGOOD=ok\nno_equals_sign\n")
    assert _parse_env_file(env) == {"GOOD": "ok"}


def test_parse_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _parse_env_file(tmp_path / "missing.env") == {}


def test_load_local_env_populates_environ(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADV_TEST_KEY", raising=False)
    (tmp_path / ".env").write_text("ADV_TEST_KEY=from_dotenv\n")
    applied = load_local_env()
    assert applied.get("ADV_TEST_KEY") == "from_dotenv"
    assert os.environ.get("ADV_TEST_KEY") == "from_dotenv"


def test_load_local_env_does_not_override_shell(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADV_TEST_KEY", "from_shell")
    (tmp_path / ".env").write_text("ADV_TEST_KEY=from_dotenv\n")
    load_local_env()
    assert os.environ["ADV_TEST_KEY"] == "from_shell"


def test_load_local_env_override_true_clobbers_shell(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADV_TEST_KEY", "from_shell")
    (tmp_path / ".env").write_text("ADV_TEST_KEY=from_dotenv\n")
    load_local_env(override=True)
    assert os.environ["ADV_TEST_KEY"] == "from_dotenv"
