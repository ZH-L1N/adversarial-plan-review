---
name: adversarial-plan-review
description: Run a GAN-style adversarial review loop on a plan markdown file. Claude is the planner; an independent reviewer (OpenAI Responses API with gpt-5.6-sol by default, the headless Claude Code CLI as the repo-verifying auto-fallback — including when OpenAI quota runs out mid-loop — or Codex CLI as legacy fallback) returns severity-tagged findings. v2 features diff-aware reviewing, severity-gated exit, JSON sidecar persistence, plan-bloat detection, and resume support. TRIGGER when the user wants to stress-test a plan with a different model, run adversarial plan review, iterate a plan until it survives discriminator critique, or says "adversarial plan review", "GAN review my plan", "codex-review my plan", or has just finished a plan draft and wants external challenge before implementation. DO NOT TRIGGER for code review, git diff review, PR review, or implementation review — for those use /codex:adversarial-review, /codex:review, or code-review:code-review. This skill operates only on plan markdown files under plans/, never on source code.
---

# Adversarial Plan Review Loop

Two roles, two different models, one loop.

- **Reviewer** — auto-detected per Setup step 2. Default is **OpenAI Responses API** (`gpt-5.6-sol`) when `OPENAI_API_KEY` is set; otherwise the **Claude Code CLI** (`claude -p`, `opus`); otherwise **Codex CLI** (`gpt-5.6-sol` via ChatGPT auth). Adversarial discriminator: tries to break confidence in the plan. The OpenAI path returns schema-validated, severity-tagged findings (`high|medium|low`); the Claude path returns the same JSON validated locally (retry-once on malformed output) and additionally verifies the plan against the repo; the Codex path returns prose findings with severity inferred via keyword heuristic.
- **Planner** — You (Claude). Go through each finding, accept or reject with concrete reasoning, edit the plan for accepted findings. The split between writer and reviewer is the whole point: it prevents the planner from rationalizing its own blind spots.

**v2 status:** Phases 1–4 are live (transport abstraction, structured outputs with severity tags, diff-aware reviewing, severity-gated exit, plan-bloat detection, resume support). See `plans/v2-plan.md` in this skill's own repo for the full design.

## Why this shape

A solo reviewer-planner (same model) tends to agree with itself. Running the reviewer on a different model gives genuinely independent adversarial tension. The planner still makes the final call — a loud reviewer doesn't get to drag scope creep or over-engineering into the plan.

## Scope and safety

This skill is for **planning only**. It must not touch source code, modify git state, or drift outside the plan file and its fixes log.

### Pre-flight check (first step, before Setup)

Before anything else, run:

```bash
git status --porcelain
```

If the output shows any modified, added, or deleted files **outside** `plans/` (for example, changes in `src/`, `tests/`, config files, etc.), **stop and refuse to run**. Tell the user:

> "Working tree has uncommitted changes outside `plans/`. Commit or stash them before running the adversarial plan review so code changes can't be accidentally swept into the loop."

Exemptions: this skill's own `.scratch/` snapshot directory (leftover snapshots from an interrupted run are expected on resume — not a dirty tree) and the `.env`/`.gitignore` writes made by its own first-run branch (see Allowed writes below).

If `git` is not available or this is not a git repo, skip the check and warn the user that the pre-flight guard is disabled.

### Allowed writes (hard boundary)

During the entire loop, the only files you may create or edit are:

- `plans/<version>-<slug>.md` — the plan itself
- `plans/fixs/<version>-<slug>-fixes.md` — the round log
- `plans/fixs/` directory (create if missing)
- `.scratch/<version>-<slug>-plan-snapshot-r*.md` — round baseline snapshots (cleaned up at exit)

**First-run UX exceptions** (Setup step 2 only, not during the loop): when a user supplies an OpenAI API key via `AskUserQuestion`, `first_run.save_openai_key_to_env()` may also write to:

- `.env` — creates or updates the `OPENAI_API_KEY=...` line; gitignored
- `.gitignore` — appends `.env` if not already listed; best-effort

These writes only happen during the first-run prompt branch in Setup step 2 and are scoped to those two paths. Once the loop proper begins (Loop step 1+ below), the boundary above is restored — no `.env` or `.gitignore` writes during the loop itself.

**Every other file is read-only for the duration of this skill.** If a Codex finding calls for code changes, tests, config edits, etc., **do not implement them**. Record the recommendation in the fixes md under "Plan edits applied" as a deferred action (e.g., *"Deferred to implementation: add unit test for X — not performed in this loop"*). The plan review loop produces a better plan; implementation happens in a separate session.

### Git prohibition

Never run any command that modifies git state during this skill:

- No `git commit` — not at the end, not "just to checkpoint the plan"
- No `git add`, `git restore`, `git checkout`, `git stash`, `git reset`, `git push`, `git merge`, `git rebase`, `git branch`
- Only read-only git commands are allowed (`git status`, `git log`, `git diff` — for the pre-flight check or diagnosis)

Commits are the user's decision after the loop exits. The fixes md is already the audit trail; git history is a separate concern.

### If you catch yourself about to break scope

Stop. Re-read this section. Report what you were about to do, and ask the user what to do instead. Do not silently proceed.

## Termination

Exit reasons and their gates are defined by step 7's `evaluate_exit` table
below: `approved`, `resolved`, `resolved_with_deferrals`, `planner_locked`,
`ceiling_hit` (`ADVERSARIAL_MAX_ROUNDS`, defaulting to `loop_state.DEFAULT_MAX_ROUNDS`),
`cost_capped`.

## Reviewer transports

| Transport | Auth | Structured output | Repo access | Model default |
|---|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | Strict JSON schema, server-enforced | No — text only | `gpt-5.6-sol` |
| `claude` | Claude Code login (no key) | JSON-in-prompt + validate/retry | **Yes — settings-isolated** | `opus` (alias; resolved id recorded) |
| `codex` | `codex login` (ChatGPT auth) | Prose + keyword severity heuristic | No — text only | `gpt-5.6-sol` |

**Detection order** (`reviewer.detect_transport`): explicit `ADVERSARIAL_TRANSPORT` → `OPENAI_API_KEY` set → `claude` on PATH → `codex` on PATH → `TransportUnavailableError` (first-run UX). Valid explicit values are exactly `openai`, `codex`, `claude`; `anthropic` is **not** an alias and raises.

### Containment contract for the `claude` transport

The claude reviewer is a real agent with tools, running inside the user's repo. Its boundaries are enforced by **flags, not prose** — `reviewer._invoke_claude` always passes `--setting-sources ""`, `--strict-mcp-config`, `--tools`, `--allowedTools`, a `--disallowedTools` write/git/rm floor, `--max-turns`, and `--max-budget-usd`.

**Do not weaken this to a prompt instruction.** It was probed on `claude 2.1.227`: with a user-level `permissions.defaultMode: bypassPermissions` in `~/.claude/settings.json`, a `-p` child granted **only** `Read`/`Grep`/`Glob` still executed `Write` and created a file. `--allowedTools` alone does not contain — the inherited settings out-rank it. Adding `--setting-sources ""` turned the same attempt into a `permission_denials` entry with no file on disk. Settings isolation also drops the reviewed repo's ambient `CLAUDE.md`/skills/hooks (~37k cache tokens on a one-word probe), which is what keeps the reviewer independent of the priors it is supposed to challenge.

The scope rules in "Scope and safety" above still apply to the reviewer subprocess — the flags are how they are made true, not a replacement for them.

### Calibration for the `claude` transport

`build_reviewer_prompt_v2` appends three claude-only blocks (`transport="claude"`); the openai/codex prompts stay byte-identical.

- `<repo_verification>` — round 1: open every file the plan cites, check named fixtures/helpers/config keys exist, lint-probe embedded code, verify library versions; repo claims need personally-verified `file:line` or probe-output evidence; clean up scratch files, never modify tracked files, never run git write commands. Rounds ≥ 2: verify prior-round resolutions in the diff, then hunt only for new implementation-breaking defects — no full re-sweep.
- `<finding_discipline>` — ≤ 8 findings ranked by impact, ≤ 3 `low`, everything below the bar collapsed into a `suppressed: N below-bar observations` line. Repo access is the capability worth keeping; volume is the failure mode being imported against (12 round-1 findings vs GPT's historical 3–6).
- `<output_format>` — the JSON contract, **derived from `REVIEW_SCHEMA` at import time** so it cannot drift from the validator: exactly one JSON object, no fences, no prose except the trailing `suppressed:` line; the top-level keys and each `findings[]` item's fields with their types, enums, and `additionalProperties: false` ("NO other keys") semantics; plus the cross-field invariants. The openai path gets this enforced server-side by strict structured outputs — the CLI has no enforcement at all, so an unstated contract meant every round rode on `parse_claude_response` guessing.

### Quota fallback (openai → claude)

`invoke_reviewer` raises `QuotaExhaustedError` (a `TransportError`) when the OpenAI account is out of quota/credit, and does **not** fall back on its own — the prompt in hand was built for the openai calibration. The orchestration owns the switch; see Loop step 2.

## Setup

> **v2:** Transport auto-detects between OpenAI Responses API (`gpt-5.6-sol`, default if `OPENAI_API_KEY` is set), the Claude Code CLI (`claude -p`, repo-verifying fallback), and Codex CLI (legacy fallback). Findings are tagged with severity (`high|medium|low`) on the OpenAI and Claude paths. Loop termination is severity-gated via step 7's `evaluate_exit`.

1. **Pre-flight check.** Run `git status --porcelain` and refuse to start if any modified/added/deleted files exist outside `plans/`. Exemptions: this skill's own `.scratch/` snapshot directory (leftover snapshots from an interrupted run are expected on resume — not a dirty tree) and the `.env`/`.gitignore` writes made by its own first-run branch.

2. **Transport check + first-run UX (v2 step 2 of §5.0a in plans/v2-plan.md).** Run:

   ```bash
   uv run --no-project --with openai python "<skill-dir>/scripts/first_run.py" --check
   ```

   Run every other `python` snippet in this skill the same way
   (`uv run --no-project --with openai python …`): bare `python` may lack
   the `openai` package, which the reviewer imports lazily — so `--check`
   alone cannot detect its absence, and `--no-project` keeps `uv` from
   trying to sync the target repo's own environment first.

   If exit code 0, the script prints which transport will be used (`openai`, `claude` or `codex`) plus a `transports available:` line listing every one it found; **record the winning name — the prompt builder and the quota fallback in Loop steps 1–2 both need it.** Proceed to step 3.

   If exit code 2, no transport is configured. Use `AskUserQuestion` to ask the user how to proceed:

   - **Option A — "I have an OpenAI API key (recommended)":** ask the user to paste the key (text input via `AskUserQuestion` "Other" free-form), then call:

     ```python
     # in a small inline Python snippet you run via Bash; mirrors the
     # sys.path setup the loop step 2 snippet uses so `first_run` can find
     # its sibling `reviewer` module.
     import sys
     sys.path.insert(0, "<skill-dir>/scripts")
     from first_run import save_openai_key_to_env
     save_openai_key_to_env(USER_PROVIDED_KEY)
     ```

     Then re-run `first_run.py --check`. It should now report `transport ready: openai`.

   - **Option B — "Use my Claude Code login (no API key)":** confirm `claude` is on PATH (`command -v claude`) and re-run the check; it should report `transport ready: claude`. Nothing to configure — the CLI uses the user's existing Claude Code auth. Mention that this reviewer *reads the repo* (settings-isolated, no writes to tracked files) so it can verify the plan's claims.

   - **Option C — "I have Codex CLI installed":** run `/codex:setup` and re-run the check.

   - **Option D — "I need help setting one up":** print `setup_guide_text()` from `first_run.py` and exit; the user configures and re-invokes the skill.

3. **Interactively ask the user** for the plan **slug** and **version** (e.g., `optical-lcoe`, `v0.0.5`). Always ask — do not guess from context, do not accept args. Deliberate paths prevent accidental overwrites.

   **Orchestrated mode exception:** when this skill is invoked by the `/ship` orchestrator with an explicit slug, version, and rounds ceiling (`ADVERSARIAL_MAX_ROUNDS`), skip this interactive ask and use the supplied values verbatim. Resume detection (step 6) also auto-selects **Resume** when prior sidecars exist instead of asking. The step-7a plan-bloat ask is likewise skipped: on a bloat trigger, auto-select **Switch to consistency-only mode** and record the choice in the round's JSON sidecar and the End report (not the fixes md body — `regenerate_fixes_md` rewrites it each round). Exit-time soft-blocks (step 7) are likewise auto-resolved per their own orchestrated-mode notes. Everything else — scope boundary, git prohibition, step-4a uncertainty consultations — is unchanged.

4. Resolve paths in the user's current working directory:
   - Plan md: `plans/<version>-<slug>.md` — **must already exist**. If missing, fail fast and tell the user to draft it first. This skill does not create plans from scratch.
   - Fixes md: `plans/fixs/<version>-<slug>-fixes.md` — create with the header below if missing. Append if resuming.

5. Create `plans/fixs/` if it does not exist.

6. **Resume detection (v2 step 4 of §5.0a — Phase 4).** Walk prior round JSON sidecars at `plans/fixs/<version>-<slug>-round-*.json`:

   ```bash
   python -c "
   import sys
   sys.path.insert(0, '<skill-dir>/scripts')
   from loop_state import detect_resume
   status = detect_resume(slug='<slug>', version='<version>')
   print(status)
   "
   ```

   - If `has_prior_run=True`, use `AskUserQuestion` to ask the user: **(a)** Resume from round N+1, **(b)** Start over (destructive — see §5.0 destructive ops table; lists files to delete before confirmation), or **(c)** Cancel — **unless in orchestrated mode** (invoked by `/ship`), in which case do not ask: auto-select **Resume**.
   - If `has_prior_run=False`, take the initial baseline snapshot:

     ```bash
     python -c "
     import sys
     sys.path.insert(0, '<skill-dir>/scripts')
     from pathlib import Path
     from loop_state import take_initial_snapshot
     take_initial_snapshot(Path('plans/<version>-<slug>.md'), slug='<slug>', version='<version>')
     "
     ```

7. **Take initial snapshot before round 1** (already done above if no prior run). The skill writes `.scratch/<version>-<slug>-plan-snapshot-r1.md` as the baseline that round-2's diff will compare against.

### Fixes md header (only written on first creation)

The header is rendered by `scripts/render_markdown.py` from the round-1 sidecar after the round completes. The rendered text matches:

```markdown
# Fixes log: <slug> <version>

- Plan: `plans/<version>-<slug>.md`
- Started: <ISO 8601 timestamp>
- Reviewer: <"OpenAI Responses API (gpt-5.6-sol)" | "Codex CLI (gpt-5.6-sol)">
- Planner: Claude
- Termination rules: severity-gated exit | NO FINDINGS | ceiling hit | planner-locked | cost-capped (v2; see plans/v2-plan.md §5.4)
```

## Loop

### Round N

**1. Build the reviewer prompt.**

Locate this skill's directory (the folder containing this SKILL.md). Use the diff-aware v2 builder (`build_reviewer_prompt_v2.py`); the v1 builder is kept only for fallback debugging.

Pass the **active transport** (the name Setup step 2 reported) via `--transport`: it selects the reviewer calibration. Omitting it silently builds the openai prompt, which on a claude round means no repo-verification and no finding caps.

For round 1:

```bash
python "<skill-dir>/scripts/build_reviewer_prompt_v2.py" \
  --plan-file "plans/<version>-<slug>.md" \
  --slug "<slug>" \
  --version "<version>" \
  --round 1 \
  --transport "$TRANSPORT" \
  > /tmp/round-1-prompt.txt
```

For round N > 1, first compute the diff via `loop_state.compute_round_diff()`, then pass it to the builder:

```bash
python -c "
import sys
sys.path.insert(0, '<skill-dir>/scripts')
from pathlib import Path
from loop_state import compute_round_diff
diff_text, recovered = compute_round_diff(
    Path('plans/<version>-<slug>.md'),
    round_n=N, slug='<slug>', version='<version>',
)
Path('/tmp/round-N-diff.patch').write_text(diff_text, encoding='utf-8')
print('recovered_from_git=', recovered)  # True only if .scratch/ wiped + sidecars unrecoverable
"

python "<skill-dir>/scripts/build_reviewer_prompt_v2.py" \
  --plan-file "plans/<version>-<slug>.md" \
  --slug "<slug>" \
  --version "<version>" \
  --round N \
  --diff-file /tmp/round-N-diff.patch \
  $(test "$RECOVERED" = "True" && echo "--diff-recovered-from-git") \
  $(test "$CONSISTENCY_ONLY" = "True" && echo "--consistency-only") \
  --cumulative-cost-usd "$CUMULATIVE_COST" \
  --transport "$TRANSPORT" \
  > /tmp/round-N-prompt.txt
```

The builder emits a richer prompt with prior-rounds summary, accepted findings to verify, rejected findings for context, the unified diff, and the full plan for cross-reference. See plans/v2-plan.md §5.3.

**2. Invoke the reviewer (foreground, read-only).**

The transport (`openai`, `claude` or `codex`) is auto-detected at skill start (Setup step 2). Use `scripts/reviewer.py` regardless of which transport — it abstracts the difference:

```bash
# in a small inline Python snippet you run via Bash, after building the prompt above
python -c "
import os, sys, json, re, subprocess
from pathlib import Path
sys.path.insert(0, '<skill-dir>/scripts')
from reviewer import (
    QuotaExhaustedError, TransportSelection, _is_claude_cli_available,
    detect_transport, invoke_reviewer,
)

SLUG, VERSION, ROUND_N = '<slug>', '<version>', N
SKILL_DIR = '<skill-dir>'
fixes_path = Path(f'plans/fixs/{VERSION}-{SLUG}-fixes.md')

prompt_path = Path('/tmp/round-N-prompt.txt')
selection = detect_transport()
explicit = (os.environ.get('ADVERSARIAL_TRANSPORT') or '').strip().lower()

try:
    result = invoke_reviewer(prompt_path.read_text(encoding='utf-8'), round_n=ROUND_N,
                             transport=selection, repo_root=os.getcwd())
except QuotaExhaustedError:
    # Quota fallback: auto-detected openai only. An EXPLICIT selection surfaces.
    if explicit or not _is_claude_cli_available(dict(os.environ)):
        raise
    print('transport fallback: openai quota exhausted -> claude', file=sys.stderr)
    # Rebuild the prompt for the CLAUDE calibration — the one we just used has
    # no <repo_verification>/<finding_discipline>/<output_format> blocks.
    subprocess.run([sys.executable, f'{SKILL_DIR}/scripts/build_reviewer_prompt_v2.py',
                    '--plan-file', f'plans/{VERSION}-{SLUG}.md', '--slug', SLUG,
                    '--version', VERSION, '--round', str(ROUND_N),
                    '--transport', 'claude',
                    # plus the same --diff-file/--consistency-only/--cumulative-cost-usd
                    # arguments step 1 used for this round, when ROUND_N > 1
                    ], stdout=prompt_path.open('w', encoding='utf-8'), check=True)
    result = invoke_reviewer(prompt_path.read_text(encoding='utf-8'), round_n=ROUND_N,
                             transport=TransportSelection('claude', 'openai quota exhausted'),
                             repo_root=os.getcwd())

# Compute cumulative cost from prior round-stats blocks in the fixes-md.
# (Phase 4 will replace this with sidecar-based recovery.)
prior_cumulative = 0.0
if fixes_path.exists():
    text = fixes_path.read_text(encoding='utf-8')
    matches = re.findall(r'cumulative:\s*\\\$([0-9.]+)', text)
    if matches:
        prior_cumulative = float(matches[-1])
cumulative_cost_usd = prior_cumulative + result.usage.cost_usd

print(json.dumps({
  'status': result.status,
  'findings': [f.to_dict() for f in result.findings],
  'open_questions': [oq.to_dict() for oq in result.open_questions],
  'transport': result.transport,
  'model': result.model,
  'tokens_input': result.usage.tokens_input,
  'tokens_output': result.usage.tokens_output,
  'cost_usd': result.usage.cost_usd,
  'cumulative_cost_usd': round(cumulative_cost_usd, 4),
  'raw_response_text': result.raw_response_text,
}))
"
```

The reviewer module:
- Picks transport via env (`ADVERSARIAL_TRANSPORT`, then `OPENAI_API_KEY`, then Claude CLI, then Codex availability)
- For OpenAI: uses Responses API with strict JSON schema (`gpt-5.6-sol` default), guaranteeing severity-tagged findings
- For Claude: runs `claude -p --output-format json` with the containment flags above, prompt over stdin, `cwd=repo_root` so the repo-verification pass can read the plan's repo; checks `is_error`/`subtype` **before** touching `result` (null on errors), records `total_cost_usd` verbatim, and sums the cache token fields into `tokens_input`
- For Codex: pipes prompt via stdin (Windows-safe; bypasses argv length limits), then runs the keyword-based severity heuristic
- Returns a normalized `ReviewResult` with `status`, `findings`, `open_questions`, transport metadata, and usage figures. **The sidecar records the transport/model that actually ran** — after a quota fallback that is `claude` + the resolved model id, not the openai selection you started with

Retry policy: a `TransportError` with `is_transient=True` gets one retry (D20). That covers a claude round truncated by our own `--max-turns`/`--max-budget-usd` guard — a truncation is not a failure, but two in a row is an operator problem (raise the cap). `QuotaExhaustedError` is never retried; it is the transport switch above.

**3. Interpret the result.**

- If `result.status == "NO_FINDINGS"` → schema guarantees `findings` and `open_questions` are both empty. Append a clean round section to fixes md and **exit with approved**.
- If `result.status == "FINDINGS_PRESENT"` → at least one finding OR open question. Both feed step 4.

(Schema invariant: NO_FINDINGS implies no open questions, and FINDINGS_PRESENT carries at least one finding or open question. See plans/v2-plan.md §5.2. The Codex prose path coerces ambiguous outputs to match the same invariant before returning.)

**4. Planner decision pass.** For each numbered finding, decide **Accept**, **Reject**, or **Uncertain**.

- **Accept:** state the specific change you will make to the plan md (which section, what changes).
- **Reject:** give a concrete, specific reason. Acceptable reasons include:
  - "Already addressed in section X of the plan" (cite it)
  - "Out of scope for this version — belongs to v0.0.(N+1)"
  - "Based on a wrong assumption about the codebase" (explain which)
  - "Acceptable risk given the failure mode and probability" (explain)

  **Unacceptable rejections:** "I disagree", "not important", "Claude's judgment differs". A rejection is a load-bearing decision — it must survive scrutiny. Push back on findings that are real, don't rubber-stamp rejections out of convenience.
- **Uncertain:** reserved for findings where you genuinely cannot decide from the plan text + codebase knowledge — e.g., the finding hinges on a product decision only the user can make, or on information you don't have. In addition, read each finding semantically: if Codex itself is hedging (real uncertainty, not routine qualifiers), treat it as Uncertain even if Codex did not emit an `OPEN QUESTIONS:` block.

  **Uncertain is not a hedge.** If you can reason a finding out, do. Using Uncertain to avoid a hard call is a red flag — see "Red flags — misuse of Uncertain" below.

**4a. User consultation (only if the round produced any Uncertain decisions or Codex returned an `OPEN QUESTIONS:` block).**

Pause after classifying every finding. Make **one** batched `AskUserQuestion` call containing:

- one structured sub-question per uncertain finding (options: **Accept** / **Reject**, plus "Other" for free-text guidance), with the Codex finding pasted verbatim in the question body, and
- one structured sub-question per Codex open question (options sized to the question; "Other" always available).

Do not edit the plan md or make any further decisions until the user responds. When they do, treat the answer as binding: fold it into the decision set exactly as if the planner had produced it (Accept / Reject / specific guidance). User-supplied Rejects count identically to planner Rejects for termination purposes.

**5. Edit the plan md.** For every accepted finding (including user-resolved Accepts), make the stated change now. All edits for the round happen *after* user consultation is resolved — no mid-round edits. Targeted edits only; do not restructure the whole plan unless a finding specifically requires it.

**6. Persist the round (v2 — sidecar-first; markdown rendered from sidecar).**

The JSON sidecar is the source of truth (§5.7). Build a `RoundState`, call `build_sidecar()`, write atomically, then re-render the fixes-md from all sidecars in order:

```bash
python -c "
import sys, time
sys.path.insert(0, '<skill-dir>/scripts')
from datetime import datetime, timezone
from pathlib import Path
from loop_state import (
    RoundState, PlannerDecision, PlanEdit,
    build_sidecar, write_sidecar_atomic, regenerate_fixes_md,
    cleanup_snapshots,
)

# Construct the RoundState from the reviewer result (captured in step 2)
# and the planner decisions + plan edits (captured in steps 4-5).
state = RoundState(
    round_n=N, slug='<slug>', version='<version>',
    transport=result_transport, model=result_model,
    started_at=ROUND_START_ISO, completed_at=datetime.now(tz=timezone.utc).isoformat(),
    reviewer_response=parsed_review_result,   # ReviewResult from step 2
    decisions=[PlannerDecision(...) for ...], # one per finding/open-question
    plan_edits=[PlanEdit(...) for ...],       # one per applied edit
    plan_content_at_end=Path('plans/<version>-<slug>.md').read_text(encoding='utf-8'),
    baseline_plan_content=BASELINE_TEXT_IF_ROUND_1_ELSE_NONE,
    cumulative_cost_usd=cumulative_cost,
    duration_seconds=time.time() - round_start_epoch,
    plan_size_delta=current_plan_size - previous_plan_size,
)

sidecar = build_sidecar(state, raw_response_text=result_raw_response_text)
write_sidecar_atomic(sidecar, slug='<slug>', version='<version>')
regenerate_fixes_md(slug='<slug>', version='<version>')  # markdown is derived
"
```

The rendered fixes-md follows this structure (rendered by `render_markdown.py`; do NOT hand-edit — the next round's regenerate_fixes_md call will overwrite). The outer fence below is 4 backticks so the inner 3-backtick fence around the raw response renders correctly:

````markdown
## Round N — <ISO 8601 timestamp>

### Reviewer findings (<transport>)
1. **[HIGH]** [<category>] <what_can_go_wrong>
   *Concrete fix:* <concrete_fix>
2. **[MEDIUM]** [<category>] <what_can_go_wrong>
   *Concrete fix:* <concrete_fix>
...

OPEN QUESTIONS:
- (oq_rN_1) <question text>
- (oq_rN_2) <question text>

(omit OPEN QUESTIONS section if open_questions is empty)

### Planner decisions (Claude)
1. **Accept** — <what will change in the plan md>
2. **Reject** — <specific reason>
3. **Accept (via user)** — <what will change; user's decision>
4. **Reject (via user)** — <user's stated reason>
...

### Open questions and uncertainty
(include this section ONLY if the round had any Uncertain findings or a non-empty open_questions list; omit entirely otherwise)

- Reviewer open questions (verbatim):
  - (oq_rN_1) ...
- Planner-uncertain findings:
  - Finding N — <why uncertain>

### User resolution (verbatim)
(include this section ONLY if a user consultation actually happened in this round)

> <paste the user's answer verbatim, attribution: "User via AskUserQuestion">

- Finding N → <Accept | Reject | specific guidance>
- Open question oq_rN_1 → <user's answer, condensed>

### Plan edits applied
- <bullet: section updated>
- <bullet: verification step added>

(or: "None — all findings rejected")

### Round stats
- Reviewer: <transport> (<model>)
- Tokens: <tokens_input> input / <tokens_output> output
- Cost: $<cost_usd> (cumulative: $<cumulative_cost_usd>)
- Severity histogram: high=<H>, medium=<M>, low=<L>
- Plan size: <chars> chars

### Reviewer raw response
```text
<verbatim raw_response_text from the ReviewResult — JSON for OpenAI, JSON (possibly fenced / prose-wrapped, incl. any `suppressed: N below-bar observations` line) for Claude, prose for Codex>
```
````

**Severity prefix rules:**
- OpenAI transport: severity comes directly from the schema-validated response (`high|medium|low`)
- Claude transport: severity comes from the reviewer's own JSON, locally validated against the same schema — an out-of-enum severity is a `ReviewSchemaError` and gets one retry, never a silent coercion
- Codex transport: severity is inferred via keyword heuristic in `parse_review.py` (silent/data-loss/security → high; gap/ambiguous/missing-test → medium; otherwise low). Documented as best-effort.

**Open-question IDs (`oq_rN_<index>`)** are assigned post-parse by `assign_open_question_ids()` in `parse_review.py`. They are stable within a round and unique across rounds. Reference these IDs in the `User resolution` section when a question gets answered.

Every user-supplied decision must also appear in the `Planner decisions` list tagged `(via user)` so termination logic can scan a single list.

**7. Termination check (v2 — severity-gated; §5.4).**

Call `loop_state.evaluate_exit()` with the populated `RoundState`:

```bash
python -c "
import sys, os
sys.path.insert(0, '<skill-dir>/scripts')
from loop_state import evaluate_exit, ExitReason, DEFAULT_MAX_ROUNDS

decision = evaluate_exit(
    state,
    max_rounds=int(os.environ.get('ADVERSARIAL_MAX_ROUNDS', str(DEFAULT_MAX_ROUNDS))),
    cumulative_cost_usd=cumulative_cost,
    cost_cap_usd=float(os.environ.get('ADVERSARIAL_MAX_COST_USD', '5.0')),
)
print(decision.reason.value, decision.needs_soft_block)
"
```

Possible outcomes (in priority order — `evaluate_exit` evaluates them in this order and returns the first match):

| `decision.reason` | When | Action |
|---|---|---|
| `approved` | Reviewer returned NO_FINDINGS | **Exit**: clean review, schema guarantees no open questions either |
| `planner_locked` | Every finding this round was rejected (must be checked before `resolved` since rejections count as "decided") | **Soft-block** if open items remain; else exit |
| `resolved` | Zero unresolved highs + zero open questions + every medium decided **AND no findings were accepted this round** (accepts produce edits that need a follow-up review round to validate; this exit fires only when decisions exist but none were accepts — a narrow case in practice) | **Exit**: clean by design |
| `cost_capped` | Cumulative cost ≥ `ADVERSARIAL_MAX_COST_USD` (default $5) | **Soft-block** if `decision.needs_soft_block`; else exit |
| `ceiling_hit` | `round_n >= ADVERSARIAL_MAX_ROUNDS` (defaults to `loop_state.DEFAULT_MAX_ROUNDS`) | **Soft-block** if open items remain; else exit |
| `no_exit` | None of the above | N++, go back to step 1 |

Note: the `resolved_with_deferrals` reason is NOT returned directly by `evaluate_exit`; it is produced by calling `escalate_to_resolved_with_deferrals(decision, deferrals)` after the user completes the soft-block deferral flow described below.

If `decision.needs_soft_block` is True, run the §5.4.1 soft-block flow — **unless in orchestrated mode** (invoked by `/ship`), in which case do not ask: any open HIGH finding → exit with the underlying reason (`ceiling_hit` / `cost_capped` / `planner_locked`) and report the open highs to the orchestrator (it stops its pipeline); otherwise auto-populate `Deferral(item_id, severity, reason="auto-deferred by /ship at exit", target_version="backlog")` for every open medium/low item, stash them on `state.deferrals_at_exit`, re-run step 6 persistence, and promote via `escalate_to_resolved_with_deferrals(decision, deferrals)`. Interactive invocations proceed with the flow below:

1. **Step 1 (action selection):** `AskUserQuestion` with three options — "Defer all (collect reasons + targets in next step)", "Continue looping despite the exit condition", "Exit anyway, accept all risk".
2. **Step 2 (per-item collection, only on Defer):** for each open finding/open-question, `AskUserQuestion` with free-text "Other" for the deferral reason. For mediums, also collect a target version (e.g. "v2.1", "Phase X", "backlog", or free-text). Build a list of `Deferral` objects and stash them on `state.deferrals_at_exit` BEFORE re-running step 6 — the sidecar must persist the deferrals or the exit can't be audited.
3. **Step 2 (accept_all_risk branch):** auto-populate `Deferral(item_id, severity, reason="accepted at exit", target_version="accepted-at-exit")` for every open item. The sentinel string "accepted-at-exit" satisfies the schema's medium-target non-null requirement (round-14 finding 1 of the dogfood).

After collecting deferrals (either via Defer step-2 or accept_all_risk), promote the exit reason via `escalate_to_resolved_with_deferrals(decision, deferrals)` — the end report should show `resolved_with_deferrals` rather than the underlying `ceiling_hit` / `planner_locked` / `cost_capped`, since the audit trail now reflects an explicit deferral with reasons + targets.

If the user picks **Continue**, do NOT exit — N++ and go back to step 1.

**7a. Plan-bloat detection (§5.5; only if exit decision is "no exit yet" and round_n >= ADVERSARIAL_BLOAT_WINDOW).**

```bash
python -c "
import sys, os
sys.path.insert(0, '<skill-dir>/scripts')
from loop_state import evaluate_bloat, load_sidecars

sidecars = load_sidecars(slug='<slug>', version='<version>')
verdict = evaluate_bloat(
    sidecars=sidecars,
    current_plan_size_chars=current_plan_size,
    threshold=float(os.environ.get('ADVERSARIAL_BLOAT_THRESHOLD', '0.20')),
    window=int(os.environ.get('ADVERSARIAL_BLOAT_WINDOW', '3')),
)
print(verdict.triggered, verdict.growth_fraction)
"
```

If `verdict.triggered`, AskUserQuestion with three options — "Continue normally", "Switch to consistency-only mode" (next round's prompt narrows to scrub-only via `--consistency-only` on the v2 builder), "Exit now with bloat note" — **unless in orchestrated mode** (invoked by `/ship`), in which case do not ask: auto-select "Switch to consistency-only mode", record the choice in the round's sidecar and End report, and continue. The mode choice persists for remaining rounds.

After a non-exiting round (no exit + no bloat trigger): N++, go back to step 1.

## End report

After exit, also clean up the snapshot directory:

```bash
python -c "
import sys
sys.path.insert(0, '<skill-dir>/scripts')
from loop_state import cleanup_snapshots
n = cleanup_snapshots(slug='<slug>', version='<version>')
print(f'cleaned {n} snapshot files from .scratch/')
"
```

Then tell the user:

- Final status (v2): **approved** | **resolved** | **resolved-with-deferrals** | **planner-locked** | **ceiling-hit** | **cost-capped**
- Rounds run
- Counts: total findings raised, accepted, rejected, deferred (break out user-resolved counts if non-zero)
- Severity histogram across the full run (rendered from sidecar.stats)
- Total cost (USD) — `cumulative_cost_usd` from the final round's sidecar
- Path to the final plan md
- Path to the fixes md (full transcript) — note this is the rendered view; the JSON sidecars at `plans/fixs/<version>-<slug>-round-*.json` are the audit-trail source of truth

If the loop exits at **ceiling hit** and there are still-open items, list them as two separate groups so the user can see what needs a human call outside the loop:

- Still-open findings (never resolved to Accept/Reject)
- Still-open questions (Codex open questions or planner-Uncertain findings that didn't get user resolution)

Keep the summary to ~6 lines plus the two open-item groups when present. The fixes md is the source of truth; do not reproduce it inline.

## Common pitfalls

- **Don't re-read the plan md between your own edit and Codex's next call.** Your edit is the plan's new state; trust it. Only re-read if Codex specifically challenges that a change was made.
- **Don't summarize or paraphrase Codex's findings** when writing them to the fixes md. Paste them verbatim. Paraphrasing loses signal and makes audit harder.
- **Don't bounce every accept/reject to the user.** The whole point is that you own the planner role. Ask the user only when (a) you are genuinely uncertain on a specific finding and cannot resolve it from the plan + codebase, or (b) Codex returned an `OPEN QUESTIONS:` block or is clearly hedging inside a finding. Confident decisions stay autonomous. If you notice yourself using Uncertain as a hedge to avoid a call you could make, that is the signal to *not* ask — make the call.
- **If Codex returns something that looks like findings but isn't numbered,** still treat it as findings — parse what's there. Only the exact sentinel `NO FINDINGS` triggers approval.
- **If Codex errors out** (network, auth, unknown model even after fallback), stop the loop and report the error. Do not fake findings or substitute a Claude-generated review — that defeats the whole skill.

### Red flags — misuse of Uncertain

- "I'm not 100% sure, better ask" → if you are 80% confident, decide. Uncertain is for ~50/50 and genuine information gaps.
- "The user should weigh in since it is their codebase" → not a reason. Only ask when the decision hinges on information you cannot derive from the plan + code.
- "Batching more questions feels thorough" → interruption cost is real. Ask only for the items you truly cannot resolve.
- "Codex hedged a little, better flag it" → routine qualifiers ("may want to", "consider") are not uncertainty. Reserve the pause for findings where Codex is actually unsure about the underlying risk.
