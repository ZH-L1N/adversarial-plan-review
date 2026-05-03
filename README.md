# adversarial-plan-review

A Claude Code skill that runs a GAN-style adversarial review loop on a plan markdown file. **Claude is the planner; an independent LLM (OpenAI Responses API or the Codex CLI) is the reviewer.** The reviewer tries to break confidence in the plan; Claude accepts or rejects each finding with concrete reasoning and edits the plan for accepted findings. The split prevents the planner from rationalizing its own blind spots.

> **Status:** v2.0.0 — Phases 1–4 shipped. Transport abstraction, structured outputs with severity tags, diff-aware reviewing, severity-gated exit, JSON sidecar persistence, plan-bloat detection, and resume support are all live. See [`plans/v2-plan.md`](plans/v2-plan.md) for the full design and [`plans/fixs/v2-plan-fixes.md`](plans/fixs/v2-plan-fixes.md) for the 18-round dogfood that produced it.

## When to use

Trigger this skill when:
- You've drafted an implementation plan and want adversarial pressure before writing code
- A plan has multiple stakeholders and you want a neutral, structured critique
- You're about to ship a complex feature and want one more pass at "what could go wrong?"

Don't use it for:
- Code review (use `/code-review:code-review` or `/codex:review` instead)
- PR / git diff review
- Source-code-level investigations — this skill never reads or edits source code

## Install

The skill is distributed as a Claude Code plugin. Two ways to install:

### From the Claude Code marketplace

```
/plugin marketplace add Exowatt-Labs/adversarial-plan-review
/plugin install adversarial-plan-review
```

### Directly into your skills directory

```bash
git clone https://github.com/Exowatt-Labs/adversarial-plan-review.git ~/.claude/skills/adversarial-plan-review
```

Restart Claude Code (or start a new session). Trigger the skill by saying *"adversarial plan review"*, *"GAN review my plan"*, or *"codex-review my plan"* once you have a plan markdown file under `plans/`.

## Choose a reviewer transport

The skill auto-detects between two transports per [§5.0a](plans/v2-plan.md#L99) of the design:

| Transport | What you need | Severity tags? | Best when |
|---|---|---|---|
| **OpenAI Responses API** (default) | `OPENAI_API_KEY` env var | ✅ Yes — schema-validated `high\|medium\|low` per finding | You want the v2 severity-gated exit to actually work |
| **Codex CLI** (legacy fallback) | [`codex` CLI](https://github.com/openai/codex) installed and authenticated | ⚠️ Inferred via keyword heuristic | You're already on ChatGPT auth and don't want to provision an API key |

Either path produces structured findings. The OpenAI path uses [strict structured outputs](https://platform.openai.com/docs/guides/structured-outputs) so severity tagging is reliable; the Codex path uses a keyword heuristic (`silent`/`data loss`/`security` → high; `gap`/`ambiguous`/`missing test` → medium; otherwise low) and is documented as best-effort.

### Setting up OpenAI (recommended)

1. Generate an API key at https://platform.openai.com/api-keys. A `$5–10/month` usage cap is plenty for typical plan reviews.
2. Drop it into a `.env` file at your repo root:
   ```env
   OPENAI_API_KEY=sk-proj-...
   OPENAI_REVIEWER_MODEL=gpt-5.5    # default — feel free to omit
   ```
3. The skill reads `.env` at startup. The shipped `.gitignore` excludes `.env`.

### Setting up Codex CLI

1. Install per https://github.com/openai/codex.
2. Run `codex login` to authenticate against your ChatGPT account.
3. The skill picks up `codex` automatically when `OPENAI_API_KEY` is unset.

If you launch the skill with neither configured, the first-run UX walks you through choosing one.

## Quick start

```bash
# 0. Make sure you're at a clean working tree (the skill refuses to start otherwise).
git status

# 1. Have a plan ready under plans/. The skill does not create plans from scratch.
ls plans/optical-lcoe-v0.0.5.md

# 2. Trigger the skill in Claude Code.
#    The skill will:
#      a. Run a pre-flight git check
#      b. Auto-detect or prompt for transport (Setup §5.0a step 2)
#      c. Ask you for the plan slug + version (e.g. "optical-lcoe", "v0.0.5")
#      d. Detect any prior runs and offer resume / start-over / cancel
#      e. Run the loop
```

The skill loops until one of these exit reasons fires (priority order, per [§5.4](plans/v2-plan.md#L546)):

| Exit reason | When |
|---|---|
| `approved` | Reviewer returned `NO_FINDINGS` (schema guarantees no open questions either) |
| `planner_locked` | Every finding this round was rejected by the planner |
| `resolved` | Zero unresolved highs + zero open questions + every medium decided |
| `cost_capped` | Cumulative spend ≥ `ADVERSARIAL_MAX_COST_USD` (default `$5`) |
| `ceiling_hit` | Round count ≥ `ADVERSARIAL_MAX_ROUNDS` (default `20`) |
| `resolved_with_deferrals` | One of the above fired with open items, and the user explicitly deferred them via `AskUserQuestion` |

If the loop hits ceiling/cost-cap/planner-lock with open items, the **soft-block flow** asks the user to defer (with reasons + target versions for mediums), continue, or accept-all-risk. See [§5.4.1](plans/v2-plan.md#L557).

## What you get out

Two artifacts per round:

```
plans/fixs/<version>-<slug>-round-{N}.json       # JSON sidecar — source of truth
plans/fixs/<version>-<slug>-fixes.md             # Markdown — rendered from sidecars
```

The JSON sidecar is the audit trail. The markdown is generated from it via [`scripts/render_markdown.py`](scripts/render_markdown.py); hand edits to the markdown are silently overwritten on the next round per [§5.7.5](plans/v2-plan.md). If you want to correct a typo durably, edit the JSON.

Each sidecar carries:
- The reviewer's full structured response (findings, open questions, severity histogram)
- The raw unparsed reviewer text (`raw_response_text` — for v1-style audit fidelity)
- The plan content + SHA-256 at end of round
- Round-1 only: the pre-loop baseline + SHA-256 (so resume can reconstruct round-2 diffs even if `.scratch/` was wiped)
- Per-round + cumulative cost
- Planner decisions (one per finding)
- Plan edits applied
- Optional: `restart_metadata` (post-Start-over rounds) or `deferrals_at_exit` (final rounds with soft-block deferrals)

See [`scripts/sidecar_schema.json`](scripts/sidecar_schema.json) for the full schema.

## Environment variables

All are optional except `OPENAI_API_KEY` when using the OpenAI transport.

### Transport

| Variable | Default | Description |
|---|---|---|
| `ADVERSARIAL_TRANSPORT` | (auto) | Force `openai` or `codex`; otherwise auto-detect |
| `OPENAI_API_KEY` | — | Required when transport is `openai` |
| `OPENAI_REVIEWER_MODEL` | `gpt-5.5` | Model used by the OpenAI Responses API path. Other valid options: `gpt-5.5-pro` (~6× cost, higher accuracy), `gpt-5.4`, `gpt-5-mini` |
| `OPENAI_MAX_TOKENS` | `8000` | Max output tokens per reviewer response |
| `OPENAI_INPUT_USD_PER_1M` | (built-in rates) | Override input price per 1M tokens (for non-default billing tiers) |
| `OPENAI_OUTPUT_USD_PER_1M` | (built-in rates) | Override output price per 1M tokens |

### Loop control

| Variable | Default | Description |
|---|---|---|
| `ADVERSARIAL_MAX_ROUNDS` | `20` | Hard ceiling on review rounds (raised from v1's 10) |
| `ADVERSARIAL_MAX_COST_USD` | `5.0` | Per-run cost cap |
| `ADVERSARIAL_BLOAT_THRESHOLD` | `0.20` | Plan-bloat trigger threshold (fractional growth) |
| `ADVERSARIAL_BLOAT_WINDOW` | `3` | Plan-bloat lookback window (number of rounds) |

> **Note:** these `ADVERSARIAL_*` knobs are read by the SKILL.md inline Python at the loop layer, not from inside the modules under `scripts/`. If you wire `loop_state.evaluate_exit` directly from a custom driver, pass them as keyword arguments — `loop_state` does not read env vars itself.

> Severity-gated exit is hard-wired (it's the v2 design thesis); there is no toggle to disable it. To allow exit with unresolved highs, use the soft-block `Exit anyway, accept all risk` branch — see [§5.4.1](plans/v2-plan.md#L557).

A complete `.env.example` ships with the repo.

## Cost estimation

Built-in per-1M-token rates (cached as of April 2026):

| Model | Input $/1M | Output $/1M |
|---|---|---|
| `gpt-5.5` | $5 | $30 |
| `gpt-5.5-pro` | $30 | $180 |
| `gpt-5.4` | $5 | $30 |
| `gpt-5` | $5 | $30 |
| `gpt-5-mini` | $1 | $4 |

A **typical 5-round review** of a 1,000-line plan runs about:

```
Per round:
  ~12K input tokens + ~2K output tokens
  ≈ $0.06 + $0.06 = $0.12

5 rounds ≈ $0.60
```

A **stress-test 15-round review** of a complex plan (like the v2 plan's own dogfood) runs about $1.50–$3 on `gpt-5.5`. The `$5` default cap leaves headroom; raise to `$15` for very large plans on `gpt-5.5-pro`.

The cost cap is *informational* in Phase 1+2 (tracked but not enforced) and *gating* in Phase 4 (loop pauses for user input when exceeded).

## Workflow walkthrough

A typical mid-size plan converges in 3–7 rounds. Here's what each looks like:

### Round 1: full-plan adversarial review

Builder emits the full plan + `<role>` adversarial-stance block. Reviewer returns either `NO_FINDINGS` (rare on a real first draft) or a list of structured findings tagged with severity. Planner accepts or rejects each finding with stated reasons and edits the plan for accepts. Round-1 sidecar carries the pre-loop baseline so the round-2 diff is reconstructable later.

### Round 2+: diff-aware verify-then-attack

Builder emits a tighter prompt:
- `<prior_rounds_summary>` — what happened last round (severity histogram, decisions)
- `<prior_decisions>` — last 3 rounds verbatim, older rounds 1-line summarized
- `<accepted_findings_to_verify>` — what the planner said they'd fix; reviewer cross-checks against the diff
- `<rejected_findings_for_context>` — what was rejected; reviewer should not re-raise without new evidence
- `<plan_diff>` — unified diff between r{N-1} snapshot and current plan (snapshot recovered from `.scratch/` or sidecar `plan_content`)
- `<full_plan>` — full text for cross-reference
- `<instructions>` — two-pass: (1) verify accepted edits actually landed; (2) adversarial pass on the diff

### Convergence: `resolved` exit

When the loop hits a round where every finding is decided (Accept/Reject) and zero open questions remain, the exit gate fires `resolved` and the loop ends cleanly. The end report shows the severity trajectory across rounds, total cost, and where the plan and audit trail live.

### Edge: plan-bloat warning

After round 4+, if the plan grew >20% over 3 rounds with no new high-severity findings, the loop offers three options: continue normally, switch to consistency-only mode (narrowed scrub-only reviewer), or exit now. This is the [§5.5](plans/v2-plan.md#L620) defense against the "reviewer scrubs cross-references forever" failure mode.

### Edge: resume after session interruption

Long runs can span multiple Claude sessions. On the next invocation, the skill detects existing JSON sidecars, validates them via `loop_state.detect_resume()`, and offers resume from round N+1 or destructive start-over. The markdown is regenerated from sidecars; hand edits are silently overwritten.

## Troubleshooting

### "No reviewer transport configured"

The first-run UX should have caught this, but if you skip past it: set `OPENAI_API_KEY` in `.env` (or export to your shell) and re-run.

### "OpenAI Responses API call failed"

The exception message includes the OpenAI error class. Common cases:
- `AuthenticationError` — your key is invalid or revoked. Re-generate at platform.openai.com.
- `RateLimitError` — wait and retry; the SDK auto-retries on 429.
- `BadRequestError` — usually a schema problem (rare with v2's strict-safe schema). Open an issue with the request_id.

The error carries an `is_transient` hint; the loop retries once on transient errors before failing the round.

### "Codex CLI exited 1"

The Codex CLI returned a non-zero exit. Check:
1. `codex login` — is your auth current?
2. The chosen model (`OPENAI_REVIEWER_MODEL=gpt-5.5` by default) — is it available to your Codex tier?
3. Try invoking `codex exec -m gpt-5.5 < /tmp/round-N-prompt.txt` manually to see the full stderr.

### "Schema validation failed: ..."

The reviewer returned malformed JSON or violated a cross-field invariant (e.g. `NO_FINDINGS` with non-empty `open_questions`). The `is_transient=False` retry-once policy applies. If it persists, the reviewer is consistently emitting bad output — open an issue and include the `raw_response_text` from the failing round's sidecar.

### "Non-contiguous sidecar range"

Resume detection found gaps in the sidecar audit trail (e.g. rounds 1, 2, 3, 5 — round 4 missing). The loop refuses to resume on a corrupted audit trail. Either:
- Delete the post-gap sidecars to truncate to the last contiguous round, then resume
- Choose start-over (destructive — see [§5.0](plans/v2-plan.md#L72))

### "WARNING: snapshot ... hash mismatch"

A `.scratch/` snapshot didn't hash-match its corresponding sidecar — usually means someone edited the snapshot file by hand. The loop falls through to sidecar recovery automatically; the warning is informational. If you didn't edit it manually, check for filesystem corruption.

### Resume after `.scratch/` was wiped

Expected behavior: the skill recovers snapshots from sidecar `plan_content` (or `baseline_plan_content` for round 1). No action needed.

## Repository structure

```
SKILL.md                              # The actual skill — Claude Code reads this to drive the loop
README.md                             # This file (user-facing landing page)
.env.example                          # Documented environment variables
plans/v2-plan.md                      # Full design doc (1,159 lines) — the source of truth for behavior
plans/fixs/v2-plan-fixes.md           # 18-round dogfood transcript that produced the v2 design
scripts/
  reviewer.py                         # Transport abstraction (OpenAI / Codex)
  parse_review.py                     # REVIEW_SCHEMA, parsers, severity inference
  cost_tracker.py                     # Per-round + cumulative cost
  first_run.py                        # Pre-flight transport check + .env writer
  build_reviewer_prompt.py            # v1 builder (legacy fallback)
  build_reviewer_prompt_v2.py         # v2 diff-aware builder
  render_markdown.py                  # JSON sidecar → markdown fixes-md
  loop_state.py                       # State machine (snapshots, exit gates, resume)
  sidecar_schema.json                 # JSON Schema for per-round sidecars
tests/
  test_*.py                           # 216 tests across all modules + E2E smoke
```

## Design history

This skill went through a 17-round adversarial review against itself before any v2 code was written. The full transcript lives at [`plans/fixs/v2-plan-fixes.md`](plans/fixs/v2-plan-fixes.md). Key architectural decisions are tracked in the §4 Decisions table of [`plans/v2-plan.md`](plans/v2-plan.md) (D1–D26).

If you're considering modifications to the loop logic, transport abstraction, or sidecar schema, read those documents first — many of the design choices that look quirky are deliberate fixes for failure modes the dogfood surfaced.

## License

MIT. See `LICENSE`.

## Contributing

Issues and PRs welcome. Before opening a PR:

1. Run `python -m pytest tests/` — the suite must pass.
2. If you change the loop logic or sidecar schema, update the relevant section of `plans/v2-plan.md` and bump the schema version in `scripts/sidecar_schema.json` + `loop_state.SCHEMA_VERSION`.
3. For non-trivial changes, run the v1 dogfood loop against your modified plan to surface contradictions before code review.
