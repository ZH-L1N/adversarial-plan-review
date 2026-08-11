"""First-run UX for the adversarial plan review skill.

This module is invoked at step 2 of the locked startup ordering (§5.0a) — after
the git pre-flight check, before slug/version selection. Its only job is to
make sure a reviewer transport is available before the loop can start.

Two callers:

- **SKILL.md interactive flow.** The skill calls `check_or_prompt()`, which
  detects the transport. If something is configured, the function returns the
  selection. If nothing is configured, it raises `FirstRunRequired` carrying a
  human-readable message; the SKILL.md prompt then routes the user through
  `AskUserQuestion` (a Claude Code tool, not a Python primitive — the skill
  surface owns that) and calls `save_openai_key_to_env()` once the user pastes
  a key.

- **Local CLI / CI smoke tests.** Running `python scripts/first_run.py
  --check` from the shell reports the current transport state and exits 0
  if ready, 2 if not.

Keeping the prompting code in SKILL.md (where AskUserQuestion lives) and the
detection / env-write code here gives us a clean split between Claude-Code-
flavoured UX and pure Python that can be unit-tested.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from _dotenv import load_local_env
from reviewer import (
    TransportSelection,
    TransportUnavailableError,
    _is_claude_cli_available,
    detect_transport,
)

# Load .env from cwd or skill root before any env-var read. Shell-provided
# values still win; .env only fills in keys the shell didn't set.
load_local_env()


# --- Errors ------------------------------------------------------------------


class FirstRunRequired(RuntimeError):
    """No transport (OpenAI key / Claude CLI / Codex CLI); SKILL.md must prompt."""


# --- Dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class FirstRunStatus:
    transport: TransportSelection | None
    has_openai_key: bool
    has_codex: bool
    # Defaulted so existing callers constructing the three-field form still
    # work; `check_or_prompt` always populates it.
    has_claude: bool = False

    @property
    def ready(self) -> bool:
        """True when ANY transport is usable — openai, claude, or codex."""
        return self.transport is not None

    @property
    def available_transports(self) -> list[str]:
        """Names of every transport that could serve, in detection order."""
        found = []
        if self.has_openai_key:
            found.append("openai")
        if self.has_claude:
            found.append("claude")
        if self.has_codex:
            found.append("codex")
        return found


# --- Public surface ----------------------------------------------------------


def check_or_prompt(*, env: dict[str, str] | None = None) -> FirstRunStatus:
    """Detect a usable transport; raise `FirstRunRequired` if none available.

    Returns a `FirstRunStatus` so callers can log which transport won.
    Never mutates state — saving the API key is the SKILL.md side's job after
    `AskUserQuestion` collects it, via `save_openai_key_to_env()`.
    """
    env = dict(os.environ if env is None else env)
    has_openai_key = bool(env.get("OPENAI_API_KEY"))
    has_codex = _is_codex_cli_available(env)
    # Reuse reviewer.py's hermetic PATH walk rather than a second copy: the
    # availability answer here MUST match the one detect_transport acted on.
    has_claude = _is_claude_cli_available(env)
    try:
        selection = detect_transport(env=env)
    except TransportUnavailableError as exc:
        raise FirstRunRequired(str(exc)) from exc

    return FirstRunStatus(
        transport=selection,
        has_openai_key=has_openai_key,
        has_codex=has_codex,
        has_claude=has_claude,
    )


def save_openai_key_to_env(api_key: str, *, env_path: Path | None = None) -> Path:
    """Write `OPENAI_API_KEY=<api_key>` into a local `.env` file.

    Idempotent: if `.env` already has an `OPENAI_API_KEY=` line, it's replaced.
    Otherwise the line is appended. Other lines are preserved verbatim — we
    don't reformat or rewrite unrelated content.

    Returns the resolved `.env` path. Caller is responsible for reloading the
    env (e.g. via `python-dotenv`'s `load_dotenv()`) so the new key is visible
    to the next `detect_transport()` call.
    """
    if not api_key or not api_key.strip():
        raise ValueError("OPENAI_API_KEY cannot be empty")
    if "\n" in api_key or "\r" in api_key:
        raise ValueError("OPENAI_API_KEY cannot contain newlines")

    target = env_path or Path(".env")
    new_line = f"OPENAI_API_KEY={api_key.strip()}\n"

    if not target.exists():
        target.write_text(new_line, encoding="utf-8")
        _ensure_env_gitignored()
        return target

    existing = target.read_text(encoding="utf-8")
    lines = existing.splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("OPENAI_API_KEY="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(new_line)
    target.write_text("".join(lines), encoding="utf-8")
    _ensure_env_gitignored()
    return target


def setup_guide_text() -> str:
    """Return the printable setup-guide text for the 'I need help' branch."""
    return (
        "How to configure a reviewer transport:\n"
        "\n"
        "Option 1 — OpenAI API key (recommended; enables severity tagging):\n"
        "  1. Visit https://platform.openai.com/api-keys\n"
        "  2. Create a new secret key (set a $10/month limit if you want)\n"
        "  3. Add `OPENAI_API_KEY=sk-...` to a `.env` file in your repo root\n"
        "     (the file is gitignored by this skill's `.gitignore`)\n"
        "  4. Re-run the skill.\n"
        "\n"
        "Option 2 — Claude Code CLI (auto-fallback; repo-verifying reviewer):\n"
        "  1. Install Claude Code and log in — your existing subscription is\n"
        "     enough; there is NO API key to provision.\n"
        "  2. Re-run this skill — it picks up `claude` from PATH automatically\n"
        "     whenever OPENAI_API_KEY is unset.\n"
        "  3. Set `ADVERSARIAL_TRANSPORT=claude` to force it even when an\n"
        "     OpenAI key is configured.\n"
        "     The reviewer subprocess runs settings-isolated inside your repo\n"
        "     with a read-mostly tool floor, so it can verify the plan against\n"
        "     the actual files — see README.md for the containment contract.\n"
        "\n"
        "Option 3 — Codex CLI (legacy fallback; no severity tagging):\n"
        "  1. Install via https://github.com/openai/codex\n"
        "  2. Run `codex login` to authenticate against your ChatGPT account\n"
        "  3. Re-run this skill — it will pick up the CLI automatically.\n"
        "\n"
        "Any path works. OpenAI is the default because strict structured\n"
        "outputs make severity tagging reliable, which is what lets the loop's\n"
        "severity-gated exit terminate cleanly. Claude is the preferred\n"
        "fallback (it outranks Codex) because it validates severity by retry\n"
        "and can open the repo files the plan cites.\n"
    )


# --- Private helpers ---------------------------------------------------------


def _is_codex_cli_available(env: dict[str, str]) -> bool:
    """Hermetic codex-availability check that uses the injected env's PATH.

    Code-review finding I1: previously this used `shutil.which("codex")`
    which always reads `os.environ["PATH"]`, defeating the docstring's
    "hermetic env in tests" guarantee. Now walks `env["PATH"]` manually so a
    test passing `env={"PATH": ""}` actually reports False even if the
    process has codex on its real PATH.
    """
    import os  # local — cheap, keeps top of module focused

    # 1. Look for `codex` (or platform-specific extensions) on PATHEXT-aware PATH
    path_env = env.get("PATH", "")
    pathext = env.get("PATHEXT", "")
    # PATHEXT is Windows-only and always ';'-separated (see reviewer.py's twin check).
    extensions = [""] + [ext.lower() for ext in pathext.split(";") if ext] if pathext else [""]
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        for ext in extensions:
            candidate = Path(directory) / f"codex{ext}"
            if candidate.is_file():
                return True

    # 2. Fall back to the plugin-shipped wrapper if present
    plugin_root = env.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        wrapper = Path(plugin_root) / "scripts" / "codex-companion.mjs"
        if wrapper.is_file():
            return True
    return False


def _ensure_env_gitignored() -> None:
    """Best-effort: make sure `.env` won't be committed.

    Reads `.gitignore` from the cwd; appends a `.env` line if missing. We
    don't fail loudly if `.gitignore` doesn't exist or can't be written —
    the shipping `.gitignore` already lists `.env`, this is just a belt-and-
    braces defence for repos that initialised the skill differently.
    """
    gitignore = Path(".gitignore")
    if not gitignore.exists():
        return
    try:
        contents = gitignore.read_text(encoding="utf-8")
    except OSError:
        return
    if any(line.strip() == ".env" for line in contents.splitlines()):
        return
    appended = contents
    if appended and not appended.endswith("\n"):
        appended += "\n"
    appended += ".env\n"
    try:
        gitignore.write_text(appended, encoding="utf-8")
    except OSError:
        pass


# --- CLI entry point ---------------------------------------------------------


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report transport status. Exit 0 if ready, 2 if not.",
    )
    args = parser.parse_args(argv)

    if args.check:
        try:
            status = check_or_prompt()
        except FirstRunRequired as exc:
            print(f"first-run setup required: {exc}", file=sys.stderr)
            print(setup_guide_text(), file=sys.stderr)
            return 2
        print(
            f"transport ready: {status.transport.name} ({status.transport.reason})"
        )
        print(f"transports available: {', '.join(status.available_transports) or 'none'}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
