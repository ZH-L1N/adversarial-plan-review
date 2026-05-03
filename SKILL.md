---
name: adversarial-plan-review
description: Run a GAN-style adversarial review loop on a plan markdown file. Claude is the planner; an independent reviewer (OpenAI Responses API with gpt-5.5 by default, or Codex CLI as legacy fallback) returns severity-tagged findings. v2 features diff-aware reviewing, severity-gated exit, JSON sidecar persistence, plan-bloat detection, and resume support. TRIGGER when the user wants to stress-test a plan with a different model, run adversarial plan review, iterate a plan until it survives discriminator critique, or says "adversarial plan review", "GAN review my plan", "codex-review my plan", or has just finished a plan draft and wants external challenge before implementation. DO NOT TRIGGER for code review, git diff review, PR review, or implementation review — for those use /codex:adversarial-review, /codex:review, or code-review:code-review. This skill operates only on plan markdown files under plans/, never on source code.
---

# Adversarial Plan Review Loop

Two roles, two different models, one loop.

- **Reviewer** — auto-detected per Setup step 2. Default is **OpenAI Responses API** (`gpt-5.5`) when `OPENAI_API_KEY` is set; falls back to **Codex CLI** (`gpt-5.5` via ChatGPT auth) otherwise. Adversarial discriminator: tries to break confidence in the plan. The OpenAI path returns schema-validated, severity-tagged findings (`high|medium|low`); the Codex path returns prose findings with severity inferred via keyword heuristic.
- **Planner** — You (Claude). Go through each finding, accept or reject with concrete reasoning, edit the plan for accepted findings. The split between writer and reviewer is the whole point: it prevents the planner from rationalizing its own blind spots.

**v2 status (alpha):** Phases 1+2 are live (transport abstraction + structured outputs with severity tags). Phases 3+4 (diff-aware reviewing, severity-gated exit, plan-bloat detection, resume support) are in-flight. See `plans/v2-plan.md` for the full design.

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

If `git` is not available or this is not a git repo, skip the check and warn the user that the pre-flight guard is disabled.

### Allowed writes (hard boundary)

During the entire loop, the only files you may create or edit are:

- `plans/<version>-<slug>.md` — the plan itself
- `plans/fixs/<version>-<slug>-fixes.md` — the round log
- `plans/fixs/` directory (create if missing)

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

## Termination (whichever comes first)

1. Planner rejects **every** finding in a round → **planner-locked**
2. Reviewer returns exactly `NO FINDINGS` → **approved**
3. Round 10 reached → **ceiling hit**, report still-open findings

## Setup

> **v2 (alpha):** Phases 1+2 of the v2 plan are live. Transport now auto-detects between OpenAI Responses API (`gpt-5.5`, default if `OPENAI_API_KEY` is set) and Codex CLI (legacy fallback). Findings are tagged with severity (`high|medium|low`) when the OpenAI path is used. Loop termination still follows v1 rules; severity-gated exit lands in Phase 4.

1. **Pre-flight check.** Run `git status --porcelain` and refuse to start if any modified/added/deleted files exist outside `plans/`.

2. **Transport check + first-run UX (v2 step 2 of §5.0a in plans/v2-plan.md).** Run:

   ```bash
   python "<skill-dir>/scripts/first_run.py" --check
   ```

   If exit code 0, the script prints which transport will be used (`openai` or `codex`); proceed to step 3.

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

   - **Option B — "I have Codex CLI installed":** run `/codex:setup` and re-run the check.

   - **Option C — "I need help setting one up":** print `setup_guide_text()` from `first_run.py` and exit; the user configures and re-invokes the skill.

3. **Interactively ask the user** for the plan **slug** and **version** (e.g., `optical-lcoe`, `v0.0.5`). Always ask — do not guess from context, do not accept args. Deliberate paths prevent accidental overwrites.

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

   - If `has_prior_run=True`, use `AskUserQuestion` to ask the user: **(a)** Resume from round N+1, **(b)** Start over (destructive — see §5.0 destructive ops table; lists files to delete before confirmation), or **(c)** Cancel.
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
- Reviewer: <"OpenAI Responses API (gpt-5.5)" | "Codex CLI (gpt-5.5)">
- Planner: Claude
- Termination rules: severity-gated exit | NO FINDINGS | ceiling hit | planner-locked | cost-capped (v2; see plans/v2-plan.md §5.4)
```

## Loop

### Round N

**1. Build the reviewer prompt.**

Locate this skill's directory (the folder containing this SKILL.md). Use the diff-aware v2 builder (`build_reviewer_prompt_v2.py`); the v1 builder is kept only for fallback debugging.

For round 1:

```bash
python "<skill-dir>/scripts/build_reviewer_prompt_v2.py" \
  --plan-file "plans/<version>-<slug>.md" \
  --slug "<slug>" \
  --version "<version>" \
  --round 1 \
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
  > /tmp/round-N-prompt.txt
```

The builder emits a richer prompt with prior-rounds summary, accepted findings to verify, rejected findings for context, the unified diff, and the full plan for cross-reference. See plans/v2-plan.md §5.3.

**2. Invoke the reviewer (foreground, read-only).**

The transport (`openai` or `codex`) is auto-detected at skill start (Setup step 2). Use `scripts/reviewer.py` regardless of which transport — it abstracts the difference:

```bash
# in a small inline Python snippet you run via Bash, after building the prompt above
python -c "
import sys, json, re
from pathlib import Path
sys.path.insert(0, '<skill-dir>/scripts')
from reviewer import invoke_reviewer

SLUG, VERSION, ROUND_N = '<slug>', '<version>', N
fixes_path = Path(f'plans/fixs/{SLUG}-{VERSION}-fixes.md')

result = invoke_reviewer(open('/tmp/round-N-prompt.txt').read(), round_n=ROUND_N)

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
- Picks transport via env (`ADVERSARIAL_TRANSPORT`, then `OPENAI_API_KEY`, then Codex availability)
- For OpenAI: uses Responses API with strict JSON schema (`gpt-5.5` default), guaranteeing severity-tagged findings
- For Codex: pipes prompt via stdin (Windows-safe; bypasses argv length limits), then runs the keyword-based severity heuristic
- Returns a normalized `ReviewResult` with `status`, `findings`, `open_questions`, transport metadata, and usage figures

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
<verbatim raw_response_text from the ReviewResult — JSON for OpenAI path, prose for Codex>
```
````

**Severity prefix rules:**
- OpenAI transport: severity comes directly from the schema-validated response (`high|medium|low`)
- Codex transport: severity is inferred via keyword heuristic in `parse_review.py` (silent/data-loss/security → high; gap/ambiguous/missing-test → medium; otherwise low). Documented as best-effort.

**Open-question IDs (`oq_rN_<index>`)** are assigned post-parse by `assign_open_question_ids()` in `parse_review.py`. They are stable within a round and unique across rounds. Reference these IDs in the `User resolution` section when a question gets answered.

Every user-supplied decision must also appear in the `Planner decisions` list tagged `(via user)` so termination logic can scan a single list.

**7. Termination check (v2 — severity-gated; §5.4).**

Call `loop_state.evaluate_exit()` with the populated `RoundState`:

```bash
python -c "
import sys, os
sys.path.insert(0, '<skill-dir>/scripts')
from loop_state import evaluate_exit, ExitReason

decision = evaluate_exit(
    state,
    max_rounds=int(os.environ.get('ADVERSARIAL_MAX_ROUNDS', '20')),
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
| `resolved` | Zero unresolved highs + zero open questions + every medium decided | **Exit**: clean by design |
| `cost_capped` | Cumulative cost ≥ `ADVERSARIAL_MAX_COST_USD` (default $5) | **Soft-block** if `decision.needs_soft_block`; else exit |
| `ceiling_hit` | `round_n >= ADVERSARIAL_MAX_ROUNDS` (default 20) | **Soft-block** if open items remain; else exit |
| `no_exit` | None of the above | N++, go back to step 1 |

Note: the `resolved_with_deferrals` reason is NOT returned directly by `evaluate_exit`; it is produced by calling `escalate_to_resolved_with_deferrals(decision, deferrals)` after the user completes the soft-block deferral flow described below.

If `decision.needs_soft_block` is True, run the §5.4.1 soft-block flow:

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

If `verdict.triggered`, AskUserQuestion with three options — "Continue normally", "Switch to consistency-only mode" (next round's prompt narrows to scrub-only via `--consistency-only` on the v2 builder), "Exit now with bloat note". The mode choice persists for remaining rounds.

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
