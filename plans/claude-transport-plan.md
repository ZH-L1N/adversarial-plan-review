# Claude CLI transport ("claude") — design & implementation plan

**Status: implemented 2026-08-11 14:09** — one calibrated adversarial round pre-implementation
(Opus 5, 8 findings 4H/4M, 6 suppressed, all accepted; sidecar in `plans/fixs/`), implemented
via workflow agents (one mid-run server-error recovery), post-implementation verify round
found 1H/2M/2L — all fixed. Gate: 327/327 with jsonschema. Live smoke = Task 6.

## Why

2026-08-11: OpenAI credits ran out mid-milestone; the MH-Perception v0.4.7 plan review ran
with Opus 5 agents improvised as the discriminator. Post-hoc analysis of all 68 review
rounds across 15 milestones (sidecars in MH-Perception `plans/fixs/`) found:

- The improvised Claude reviewer raised 12 findings in round 1 vs GPT's historical 3–6
  (mean 3.7). Decomposition: (1) **repo access** — all 5 round-1 HIGHs required opening
  repo files / running tools, a defect class the text-only GPT transport structurally
  cannot see; (2) **the LOW tier** — GPT reported zero lows across ~50 rounds; Claude
  reported 3–5/round, and lows are where churn lived; (3) checklist seeding + cheap
  verification for a tool-equipped agent.
- Every one of those 5 HIGHs would have red-gated the implementation. Repo verification
  is the capability to keep; volume discipline is what needs importing.
- This plan's own round-1 review (calibrated single round) validated the discipline
  design: 8 ranked findings, every one carrying live probe evidence, 6 below-bar
  observations suppressed.

## Decisions (agreed with Zehui, 2026-08-11)

1. **Transport shape:** headless Claude Code CLI subprocess (`claude -p`), symmetric to
   the existing Codex CLI path.
2. **Calibration:** repo access + discipline — HIGH/MEDIUM are the working currency,
   ≤8 findings/round ranked, ≤3 lows, full verification checklist only in round 1,
   personally-verified evidence required for repo claims.
3. **Selection policy:** GPT (`gpt-5.6-sol`) default when `OPENAI_API_KEY` works; Claude
   is the auto-fallback (outranking legacy Codex) and explicit via
   `ADVERSARIAL_TRANSPORT=claude`. Quota exhaustion on an auto-detected OpenAI transport
   falls back to Claude once, loudly, **with the prompt rebuilt for the claude
   calibration** (R1-H3).
4. **Drift ports first** — and the drift is larger than a model bump (R1-M1): it includes
   a fixes-md filename bug fix and the /ship orchestrated-mode contract.
5. **Containment is enforced by flags, not prose** (R1-H1/M4): the reviewer subprocess
   runs settings-isolated with an explicit tool floor. Probes-enabled Bash stays the
   default (per Zehui's calibration decision) but behind real guardrails.

## Verified CLI facts (claude 2.1.227, probed live 2026-08-11 — R1 evidence)

- Flags confirmed: `--output-format json`, `--allowedTools`, `--disallowedTools`,
  `--tools`, `--model` (aliases `opus|sonnet|fable` documented), `--setting-sources`,
  `--permission-mode`, `--strict-mcp-config`, `--max-budget-usd`, `--json-schema`
  (native structured-output validation), `--system-prompt`/`--append-system-prompt`.
  `--max-turns` is accepted and enforced but **absent from `--help`** (silent-removal
  risk — pin with a test).
- Result envelope: `result` (str, **null on error**), `subtype`
  (`success`/`error_max_turns`/…), `is_error`, `usage`
  (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, …), `total_cost_usd` (**non-zero even on subscription
  sessions** — $0.0384–0.0588 observed on trivial haiku calls), `modelUsage` (keyed by
  resolved model id, carries `canonicalModel`), `permission_denials`, `num_turns`,
  `terminal_reason`, `duration_ms`.
- `usage.input_tokens` was 9 while cache fields carried ~37k on the same call —
  cost/token accounting MUST include cache tokens (R1-M2).
- **`--allowedTools` alone does not contain**: with the user-level
  `permissions.defaultMode: bypassPermissions` (this machine's `~/.claude/settings.json`),
  a `-p` child granted only Read/Grep/Glob still executed Write and created a file.
  Adding `--setting-sources ""` produced a `permission_denials` entry and no file
  (R1-H1). `--tools Read,Grep,Glob,Bash` alone is also insufficient — Bash is a write
  primitive.
- A one-word reply run from a repo root consumed ~37k cache tokens of ambient context
  (CLAUDE.md + skills + settings), i.e. the child inherits the reviewed repo's priors
  and hooks unless isolated (R1-M4).

## Non-goals

- No hybrid GPT+Claude round-1 mode in this iteration.
- No changes to `REVIEW_SCHEMA`, `loop_state` logic, or `render_markdown`.
  **Exception (R1-H2):** `sidecar_schema.json`'s `transport` enum must gain `"claude"` —
  without it, `loop_state.py:511-520` hard-fails the first sidecar write on any machine
  with `jsonschema` installed. Enum widening is backward-compatible for old readers;
  bump the schema minor version per repo convention.
  **Exception (verify finding, 2026-08-11):** `render_markdown._human_transport` gains
  ONE dict entry — `"claude": "Claude Code CLI"`. Without it the fixes-md header and
  round-stats lines render a bare `claude` where the other two transports get real
  labels. Display-string only; no renderer logic, no sidecar-shape change.
- No changes to the OpenAI/Codex invocation paths beyond the typed quota error (Task 1).

---

## Task 0 — Port the installed-copy drift into the repo (two commits)

The live copy at `~/.claude/skills/adversarial-plan-review/` is ~174 diff lines ahead of
the repo (R1-M1). Port in two commits:

**Commit 1 — `chore: port gpt-5.6-sol default bump from the deployed copy`:**
- `scripts/reviewer.py`: `DEFAULT_OPENAI_MODEL` / `DEFAULT_CODEX_MODEL` = `"gpt-5.6-sol"`
  (+ docstring line).
- `scripts/cost_tracker.py`: gpt-5.6 family rows in `_DEFAULT_RATES`.
- `.env.example`, `README.md` rate table + model text.
- `tests/test_cost_tracker.py` (copy from installed) and
  `tests/test_reviewer.py::test_default_openai_model_is_gpt_55` → update name +
  assertion to gpt-5.6-sol (R1-M3 named it explicitly).

**Commit 2 — `docs: port SKILL.md orchestration/termination rewrite from the deployed copy`:**
- The fixes-md path **bug fix**: `f'plans/fixs/{SLUG}-{VERSION}-fixes.md'` →
  `f'plans/fixs/{VERSION}-{SLUG}-fixes.md'` — the installed order matches what
  `loop_state.py:446` actually writes; the repo's SKILL.md reads a filename that is
  never written, silently zeroing `prior_cumulative` in cumulative-cost recovery.
- The `/ship` "Orchestrated mode exception" block; `.scratch/` pre-flight exemptions;
  the Termination section → `evaluate_exit` table (+ `ADVERSARIAL_MAX_ROUNDS`);
  the `uv run --no-project --with openai python` invocation mandate.

Verify: post-port `diff -r` (excluding `.git`, `__pycache__`, `.pytest_cache`, `.env`,
`plans/`) between repo and installed is **empty**; test gate green (see Task 5 gate —
now includes `--with jsonschema`).

## Task 1 — `claude` transport in `scripts/reviewer.py`

**Detection** (`detect_transport`):
- Accept `ADVERSARIAL_TRANSPORT=claude`. Error message for unknown values becomes
  `"ADVERSARIAL_TRANSPORT must be 'openai', 'codex' or 'claude', got '…'"`.
  `anthropic` is NOT an alias — it rejects like any unknown value (decided; R1-M3 asked).
- Auto-detect priority: `OPENAI_API_KEY` → openai; Claude CLI on PATH → claude; Codex →
  codex; else `TransportUnavailableError`.
- `_is_claude_cli_available(env)`: hermetic PATH walk for `claude` (+PATHEXT), same
  env-injection contract as the codex check.

**Invocation** (`_invoke_claude(prompt, *, round_n, model, repo_root)`):

```python
DEFAULT_CLAUDE_MODEL = "opus"  # CLI alias; resolved id comes back in modelUsage

cmd = [
    "claude", "-p",
    "--output-format", "json",
    "--model", chosen_model,
    # Containment + independence (R1-H1, R1-M4): flags, not prose.
    "--setting-sources", "",              # no user/project settings -> no inherited
                                          # bypassPermissions, hooks, or ambient CLAUDE.md priors
    "--strict-mcp-config",                # no MCP servers
    "--tools", tools,                     # restrict the tool SET (default Read,Grep,Glob,Bash)
    "--allowedTools", tools,              # and pre-grant exactly it (isolated => real denials)
    "--disallowedTools", "Write,Edit,MultiEdit,NotebookEdit,"
                         "Bash(git commit*),Bash(git push*),Bash(git reset*),"
                         "Bash(git checkout*),Bash(git restore*),Bash(git stash*),"
                         "Bash(rm -r*),Bash(sudo*)",
    "--max-turns", str(max_turns),        # default 120 (2x observed max 74; R1-H4)
    "--max-budget-usd", str(max_budget),  # default 5.0 — the documented budget guard
]
result = subprocess.run(
    cmd, input=full_prompt, capture_output=True, check=True, text=True,
    encoding="utf-8", errors="replace",
    timeout=timeout_s,                    # ADVERSARIAL_CLAUDE_TIMEOUT_S, default 1200
    cwd=repo_root,                        # the plan's repo — where verification happens
)
```

- Prompt over **stdin** (Windows argv limits). The discriminator role travels **in the
  prompt itself** (it already does — `build_reviewer_prompt_v2` emits `<role>`), so
  settings isolation costs nothing; `--append-system-prompt` stays available if field
  use shows the role needs system-prompt weight.
- `invoke_reviewer` gains `repo_root: str | None = None` (default `os.getcwd()`);
  openai/codex ignore it.
- Env knobs: `CLAUDE_REVIEWER_MODEL` (default `opus`), `ADVERSARIAL_CLAUDE_TOOLS`
  (default `Read,Grep,Glob,Bash`; document `Read,Grep,Glob` read-only preset — dropping
  Bash from `--tools` removes the write primitive entirely), `ADVERSARIAL_CLAUDE_TIMEOUT_S`
  (1200), `ADVERSARIAL_CLAUDE_MAX_TURNS` (120), `ADVERSARIAL_CLAUDE_MAX_BUDGET_USD` (5.0).
- **Envelope handling — check `is_error`/`subtype` BEFORE touching `result`** (it is
  null on errors, R1-H4):
  - `subtype == "success"` → parse `result` text.
  - `subtype == "error_max_turns"` (or budget exhaustion) → `TransportError` with
    `is_transient=True` and the budget named in the message — the run was truncated by
    our own guard, not failed; D20 retry-once applies (the retry inherits the same caps;
    two truncations → surfaced to the operator).
  - other `is_error` → `TransportError`, transient iff the message smells like
    overload/timeout.
  - `subprocess.TimeoutExpired` → transient `TransportError`; `CalledProcessError` /
    `FileNotFoundError` → mirror the Codex path.
- **Cost + tokens (R1-M2):** `cost_usd = envelope["total_cost_usd"]` unconditionally
  (probed non-zero even on subscription — plan's old estimate branch was dead code;
  open question 3 is moot). Estimate fallback ONLY when the field is absent/None, keyed
  on the **resolved** id from `modelUsage[*].canonicalModel` (never the `opus` alias),
  via new rows in `cost_tracker._DEFAULT_RATES` for `claude-opus-5` / `claude-sonnet-5`.
  `tokens_input = input_tokens + cache_creation_input_tokens + cache_read_input_tokens`
  (state in a comment; fixture test with all fields non-zero). The `OPENAI_*_USD_PER_1M`
  env overrides must not apply to claude rows.

**Parsing** (`scripts/parse_review.py`): new `parse_claude_response(...)` — strip fences /
extract outermost `{…}` defensively, then reuse the SAME validation `parse_openai_response`
uses (factor its validation body into a shared helper if not already reusable — verify at
implementation; R1 confirmed the validator exists as module-level code reachable for
reuse). Malformed → `ReviewSchemaError` → existing D20 retry.
Optional fast-follow noted, not required: the CLI's native `--json-schema` flag could
replace defensive parsing entirely — try it during Task 6's smoke; adopt if it validates
`REVIEW_SCHEMA` cleanly.

**Runtime quota fallback (R1-H3 — architecture changed):** `_invoke_openai` maps
quota-class errors (`RateLimitError` + `insufficient_quota`, exhausted-credits auth
variants) to a new typed `QuotaExhaustedError(TransportError)`. `invoke_reviewer` does
NOT silently fall back (the prompt it holds was built for the openai calibration). The
**orchestration** (SKILL.md step) catches `QuotaExhaustedError`, and when the transport
was auto-detected and Claude is available: logs
`transport fallback: openai quota exhausted → claude`, **rebuilds the prompt with
`transport="claude"`**, and re-invokes with an explicit claude selection. Sidecar
records the transport/model that actually ran. Explicit `ADVERSARIAL_TRANSPORT=openai`
surfaces the error to the operator instead.

## Task 2 — Calibration block in `scripts/build_reviewer_prompt_v2.py`

Builders gain `transport: str = "openai"`; when `"claude"`, append after `<finding_bar>`:

- `<repo_verification>` — round 1: "You run inside the plan's repo with
  Read/Grep/Glob/Bash. Verify the plan against reality: open every file it cites, check
  named fixtures/helpers/config keys exist, lint-probe embedded code against the repo's
  actual linter config, verify the library versions its claims depend on. Repo claims
  require personally-verified `file:line` (or probe-output) evidence. Clean up scratch
  files; never modify tracked files; never run git write commands." Rounds ≥ 2:
  "Verify prior-round resolutions in the plan diff, then hunt only for new
  implementation-breaking defects — no full re-sweep."
- `<finding_discipline>` — "At most 8 findings, ranked by impact; at most 3 `low`; count
  anything below the bar in a final `suppressed: N below-bar observations` line rather
  than reporting it. HIGH/MEDIUM are the working currency; `low` only when it still
  changes implementation outcome."
- `<output_format>` (verify finding, 2026-08-11) — the JSON contract, **derived from
  `REVIEW_SCHEMA`** at import time (field names, types, enums,
  `additionalProperties: false` → "NO other keys", cross-field invariants), demanding a
  single object with no fences and no prose beyond the trailing `suppressed:` line.
  The openai path gets this server-side from strict structured outputs; the CLI has
  nothing, so without the block the contract was never stated to the reviewer at all.
  Derivation (not a hand-written copy of the v1 `<output_format>`) is the anti-drift
  requirement — a test walks `REVIEW_SCHEMA` and asserts every required field name
  reaches the prompt.

openai/codex prompts stay **byte-identical** (regression test). SKILL.md's prompt-build
step passes the active transport through — including the rebuilt-prompt fallback path
(Task 1).

## Task 3 — `scripts/first_run.py`

- `FirstRunStatus` gains `has_claude` (import the hermetic check from reviewer.py);
  `ready` = any transport. `--check` exit codes unchanged (/ship Stage-0 contract);
  output names the transports found.
- `setup_guide_text()` gains the Claude section (subscription login, no key,
  `ADVERSARIAL_TRANSPORT=claude` to force).

## Task 4 — Docs

- `SKILL.md`: transport table row (auth = Claude Code login; structured output =
  "JSON-in-prompt + validate/retry"; repo access = yes, **settings-isolated**);
  detection order; the containment contract and WHY prose-only guards are insufficient
  (cite the R1-H1 probe); the quota-fallback flow incl. prompt rebuild; calibration
  description. Frontmatter description mentions the Claude fallback.
- `README.md`: env-var rows (`CLAUDE_REVIEWER_MODEL`, `ADVERSARIAL_CLAUDE_TOOLS`,
  `ADVERSARIAL_CLAUDE_TIMEOUT_S`, `ADVERSARIAL_CLAUDE_MAX_TURNS`,
  `ADVERSARIAL_CLAUDE_MAX_BUDGET_USD`, `ADVERSARIAL_TRANSPORT=openai|codex|claude`);
  "when to use which transport" paragraph citing the v0.4.7 evidence.
- `.env.example`: the new vars, commented out.

## Task 5 — Tests (all mocked; no live CLI calls)

Gate for every commit:
`uv run --no-project --with openai --with pytest --with jsonschema python -m pytest tests/`
(R1-H2: without `jsonschema` the sidecar enum path silently degrades to the structural
fallback and the enum bug would pass).

- `tests/test_reviewer.py`:
  - detection priority + hermetic claude PATH walk;
  - **update** `test_detect_transport_invalid_explicit_value_raises` to the new message,
    input changed `"anthropic"` → `"gemini"`, plus a case asserting
    `ADVERSARIAL_TRANSPORT=anthropic` rejects (R1-M3);
  - `_invoke_claude` happy path (canned success envelope): asserts stdin prompt, argv
    contains the **containment flags** (`--setting-sources ""`, `--strict-mcp-config`,
    `--tools`, `--disallowedTools` incl. `Write`), cwd threading, model/env resolution;
  - `error_max_turns` envelope → transient `TransportError`, `result:null` never touched;
  - timeout → transient; unknown-flag rejection canary: a test pinning that
    `--max-turns` remains in the argv builder with a comment that the flag is
    undocumented on 2.1.227 (silent CLI removal → the smoke in Task 6 catches it);
  - quota fallback: `_invoke_openai` raising `QuotaExhaustedError` propagates (no silent
    in-module fallback); orchestration-level rebuild is covered by a prompt-builder test.
- `tests/test_parse_review.py`: `parse_claude_response` on clean/fenced/prose-wrapped
  JSON + schema violation.
- `tests/test_build_reviewer_prompt_v2.py`: claude blocks iff `transport="claude"`;
  round-1 vs diff-round wording; caps text; openai/codex byte-identity regression.
- `tests/test_first_run.py`: `has_claude` + guide text.
- `tests/test_cost_tracker.py`: `claude-opus-5` row keyed by canonical id; fixture with
  non-zero cache tokens proving `tokens_input` sums all three input fields; env-override
  isolation (OPENAI_* overrides don't touch claude rows).
- `tests/test_sidecar_schema.py`: `transport:"claude"` validates; unknown value rejects
  (R1-H2).

## Task 6 — Deploy + live smoke

- Sync repo → `~/.claude/skills/adversarial-plan-review/` (rsync excluding `.env`,
  caches, `plans/`); `first_run.py --check` exits 0 naming claude.
- Live smoke round (`ADVERSARIAL_TRANSPORT=claude`) against a small real plan in a
  scratch repo: sidecar written with `transport:"claude"` and non-zero
  `total_cost_usd`-sourced cost; findings ≤ 8; evidence shows repo files were opened;
  **`git status --porcelain` in the reviewed repo is byte-identical before/after the
  round** (R1-H1 acceptance); try `--json-schema REVIEW_SCHEMA` as a bonus probe.
- Commits per task (`chore:`/`feat:`/`test:`/`docs:` prefixes, matching history).

## Resolved questions (were open pre-review)

1. **Tool default** — probes-enabled `Read,Grep,Glob,Bash` stands (Zehui's call), now
   contained by flags: settings isolation + `--disallowedTools` write/git/rm floor +
   budget caps + the smoke's clean-tree assertion. Read-only preset documented.
2. **Guards** — `--max-turns 120` (2× observed max; R1-H4) + `--max-budget-usd 5.0`
   (documented flag, primary) + 1200 s wall clock; truncation classified transient.
3. **Subscription cost bookkeeping** — moot: `total_cost_usd` is reported non-zero on
   subscription sessions; record it directly (R1-M2).
