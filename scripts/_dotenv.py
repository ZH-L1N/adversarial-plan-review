"""Minimal `.env` loader — zero dependencies.

The skill stores `OPENAI_API_KEY` and other settings in a `.env` file next to
SKILL.md (or in the user's cwd). We don't ship `python-dotenv`, so this module
provides a small parser that reads `KEY=VALUE` lines and populates
`os.environ` without overriding values already set by the shell.

Search order (first hit wins; both are loaded — closer one's values take
precedence over farther one's, but neither overrides the existing shell env):

  1. `<cwd>/.env`                    — project-local override
  2. `<skill-root>/.env`             — the skill's own `.env`

`<skill-root>` is the parent of this file's directory (i.e. the directory
that contains SKILL.md). This is robust whether the skill is invoked from a
plugin install, a manual install, or a git checkout.

Existing shell env vars always win — calling `load_local_env()` after
`export OPENAI_API_KEY=...` will NOT clobber the shell-provided value.
"""
from __future__ import annotations

import os
from pathlib import Path


_SKILL_ROOT = Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Lenient: ignores malformed lines."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def load_local_env(*, override: bool = False) -> dict[str, str]:
    """Load .env files from cwd and skill root into os.environ.

    Returns the dict of keys actually set (useful for debugging). Existing
    shell env values are preserved unless `override=True`.
    """
    candidates = [
        Path.cwd() / ".env",
        _SKILL_ROOT / ".env",
    ]
    merged: dict[str, str] = {}
    # Iterate in reverse so cwd wins over skill root.
    for path in reversed(candidates):
        merged.update(_parse_env_file(path))

    applied: dict[str, str] = {}
    for key, value in merged.items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied
