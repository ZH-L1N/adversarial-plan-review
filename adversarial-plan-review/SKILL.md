---
name: adversarial-plan-review
description: Run a GAN-style adversarial review loop on a plan markdown file where Claude is the planner and Codex CLI (GPT-5) is an independent adversarial reviewer. TRIGGER when the user wants to stress-test a plan with a different model, run adversarial plan review, iterate a plan until it survives discriminator critique, or says "adversarial plan review", "GAN review my plan", "codex-review my plan", or has just finished a plan draft and wants external challenge before implementation. DO NOT TRIGGER for code review, git diff review, PR review, or implementation review — for those use /codex:adversarial-review, /codex:review, or code-review:code-review. This skill operates only on plan markdown files under plans/, never on source code.
---

# Adversarial Plan Review Loop

Two roles, two different models, one loop.

- **Reviewer** — Codex CLI (GPT-5 via user's ChatGPT auth). Adversarial discriminator. Tries to break confidence in the plan. Returns numbered findings or the exact token `NO FINDINGS`.
- **Planner** — You (Claude). Go through each finding, accept or reject with concrete reasoning, edit the plan for accepted findings. The split between writer and reviewer is the whole point: it prevents the planner from rationalizing its own blind spots.

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

- `plans/<slug>-<version>.md` — the plan itself
- `plans/fixs/<slug>-<version>-fixes.md` — the round log
- `plans/fixs/` directory (create if missing)

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

1. Confirm the Codex plugin is available: either `${CLAUDE_PLUGIN_ROOT}/scripts/codex-companion.mjs` resolves, or `/codex:setup` reports ready. If not, stop and tell the user to install/configure the Codex plugin.
2. **Interactively ask the user** for the plan **slug** and **version** (e.g., `optical-lcoe`, `v0.0.5`). Always ask — do not guess from context, do not accept args. Deliberate paths prevent accidental overwrites.
3. Resolve paths in the user's current working directory:
   - Plan md: `plans/<slug>-<version>.md` — **must already exist**. If missing, fail fast and tell the user to draft it first. This skill does not create plans from scratch.
   - Fixes md: `plans/fixs/<slug>-<version>-fixes.md` — create with the header below if missing. Append if resuming.
4. Create `plans/fixs/` if it does not exist.

### Fixes md header (only written on first creation)

```markdown
# Fixes log: <slug> <version>

- Plan: `plans/<slug>-<version>.md`
- Started: <ISO 8601 timestamp>
- Reviewer: Codex CLI (GPT-5, via `codex-companion.mjs task`)
- Planner: Claude
- Termination rules: planner-rejects-all | NO FINDINGS | 10-round ceiling
```

## Loop

### Round N

**1. Build the reviewer prompt.**

Locate this skill's directory (the folder containing this SKILL.md). Then:

```bash
python "<skill-dir>/scripts/build_reviewer_prompt.py" \
  --plan-file "plans/<slug>-<version>.md" \
  --fixes-file "plans/fixs/<slug>-<version>-fixes.md" \
  --round N
```

The script emits the full adversarial prompt to stdout, including prior rounds from the fixes md so Codex has continuity.

**2. Invoke Codex (foreground, read-only, pinned model).**

```bash
PROMPT="$(python <skill-dir>/scripts/build_reviewer_prompt.py --plan-file ... --fixes-file ... --round N)"
node "${CLAUDE_PLUGIN_ROOT}/scripts/codex-companion.mjs" task --model gpt-5.4 "$PROMPT"
```

Omit `--write`. Run foreground so the output comes back in the same turn — no `/codex:status` polling.

**Fallback:** if Codex rejects `gpt-5.4` as an unknown model, retry without `--model`. Do this silently the first time; note it in the fixes md round section.

**3. Capture Codex output.** Trim leading/trailing whitespace. Check for the exact sentinel `NO FINDINGS`.

- If `NO FINDINGS` → append a round section to fixes md recording the clean review, then **exit with approved**.
- Otherwise → parse numbered findings. If an `OPEN QUESTIONS:` block is present after the findings, parse it separately. Both feed step 4.

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

**6. Append to the fixes md.** Use this exact structure:

```markdown
## Round N — <ISO 8601 timestamp>

### Reviewer findings (Codex)
<verbatim Codex output — the numbered list it produced, including any
OPEN QUESTIONS: block>

### Planner decisions (Claude)
1. **Accept** — <what will change in the plan md>
2. **Reject** — <specific reason>
3. **Accept (via user)** — <what will change; user's decision>
4. **Reject (via user)** — <user's stated reason>
...

### Open questions and uncertainty
(include this section ONLY if the round had any Uncertain findings or a Codex
OPEN QUESTIONS: block; omit entirely otherwise)

- Codex open questions (verbatim):
  - ...
- Planner-uncertain findings:
  - Finding N — <why uncertain>

### User resolution (verbatim)
(include this section ONLY if a user consultation actually happened in this round)

> <paste the user's answer verbatim, attribution: "User via AskUserQuestion">

- Finding N → <Accept | Reject | specific guidance>
- Codex Q1 → <user's answer, condensed>

### Plan edits applied
- <bullet: section updated>
- <bullet: verification step added>

(or: "None — all findings rejected")
```

Every user-supplied decision must also appear in the `Planner decisions` list tagged `(via user)` so termination logic can scan a single list.

**7. Termination check.**

- Every finding in this round was rejected → **exit planner-locked**. User-supplied Rejects (from step 4a) count identically to planner-supplied Rejects — a round where every finding ends up rejected by any combination of planner and user still locks the loop.
- N == 10 → **exit ceiling-hit**, list still-open findings *and* any still-open open-questions in the final report as separate groups.
- Otherwise → N++, go back to step 1.

## End report

At loop exit, tell the user:

- Final status: **approved** | **planner-locked** | **ceiling hit**
- Rounds run
- Counts: total findings raised, accepted, rejected (break out user-resolved counts if non-zero)
- Path to the final plan md
- Path to the fixes md (full transcript)

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
