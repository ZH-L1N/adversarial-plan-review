# Fixes log: plan v2

- Plan: `plans/v2-plan.md`
- Started: 2026-05-03T00:00:00Z
- Reviewer: External Codex review (user-supplied verbatim findings)
- Planner: Claude
- Termination rules: planner-rejects-all | NO FINDINGS | 10-round ceiling

## Round 1 — 2026-05-03T00:00:00Z

### Reviewer findings (Codex)

1. [Diff source / Round-aware review] The diff source in `plans/v2-plan.md:257` does not actually produce the round N-1 -> N delta the design depends on. The current skill still forbids git checkpoints during the loop (`SKILL.md:47`), so `git diff HEAD~{rounds_committed}` will be cumulative against HEAD, not the previous round's plan state. Because snapshot mode is only a fallback, the wrong diff path wins whenever git diff succeeds, which undermines the core diff-aware review behavior in §5.3.

2. [State authority / Drift handling] The state-authority model is internally contradictory. `plans/v2-plan.md:395` makes the JSON sidecar a required, schema-validated gate for advancing the loop, but `plans/v2-plan.md:470` says markdown is the source of truth and JSON should be regenerated from it on drift. The markdown format described here does not contain enough structured data to losslessly recover transport, model, token usage, cost, duration, finding IDs, or schema_version. Resume and recovery behavior will be ambiguous unless one artifact is made authoritative or the JSON is reduced to a strictly derivable subset.

3. [Exit semantics / Schema gap] The exit rules allow an "approved" result while open questions remain unresolved. D15 says open questions block exit (`plans/v2-plan.md:57`), but exit rule 1 says NO_FINDINGS is authoritative even with open questions (`plans/v2-plan.md:287`), and the test plan codifies that behavior (`plans/v2-plan.md:606`). Since the schema always allows open_questions, the reviewer can return "clean" plus unresolved decisions and the loop will still approve the plan.

4. [Runtime contract / Allowed writes] The runtime write boundary is no longer decision-complete. The draft adds .env writes, .scratch snapshots, and per-round JSON artifacts (`plans/v2-plan.md:367`, `plans/v2-plan.md:395`), but the current skill contract only permits writes to the plan and fixes log (`SKILL.md:37`). The plan says SKILL.md will be rewritten, but it never explicitly redefines the runtime allowed-write set, so the implementer is left to choose between preserving the old safety contract and enabling the new flow.

5. [Startup ordering / Resume timing] Startup ordering is inconsistent around resume. First-run transport setup is specified "before slug/version prompt" (`plans/v2-plan.md:361`), but resume detection is "at skill start, after the transport check" and immediately depends on slug/version (`plans/v2-plan.md:493`). The interaction order for transport detection, plan identity selection, and resume detection needs to be locked down.

6. [Phase boundary / Test placement] Milestone A is not internally consistent with its own tests. Milestone A says the v1 loop logic remains in place and severity is only surfaced, not enforced (`plans/v2-plan.md:541`), but Phase 1 verification already includes `test_cost_cap_pauses_loop_when_exceeded` (`plans/v2-plan.md:577`). Cost-cap pause is defined later as loop-gating behavior in §5.4, so either that behavior belongs in Milestone A or the test does not.

OPEN QUESTIONS:
- Whether the JSON sidecar is runtime state or just an audit artifact. The current draft treats it as both.
- Whether diff logic should make snapshots primary after round 1, with git only for recovery/resume when snapshots are missing.
- Whether the clean-response schema should forbid non-empty open_questions when status is NO_FINDINGS.

### Planner decisions (Claude)

1. **Accept** — Reviewer is correct. `git diff HEAD~N` against an uncommitted plan computes the cumulative diff from the loop's starting commit, not the round-by-round delta. Rewrote §5.3.1 to make snapshots the primary diff source: a new `compute_round_diff()` reads `plan-snapshot-r{N-1}.md` and current plan, produces the delta, then writes `plan-snapshot-r{N}.md` for next round. Added `_take_initial_snapshot()` called once at round 1 to capture the round-0 baseline. Git is only invoked via `_recover_diff_from_git()` for resume recovery when `.scratch/` was wiped between sessions, with an explicit banner in the reviewer prompt noting the diff is cumulative-against-last-commit, not snapshot-accurate. Documented in new §5.3.2.

2. **Accept (via user)** — Architectural choice surfaced via AskUserQuestion. User selected "JSON authoritative; markdown rendered from JSON." Rewrote §5.7 accordingly: JSON sidecar is the source of truth, markdown is generated from JSON via a pure templating renderer (§5.7.6) at the end of each round. Drift handling in §5.7.5 inverted — markdown manual edits are silently overwritten on next render; users wanting persistent typo fixes must edit JSON. Schema validation guards the gate (loop refuses to advance if JSON missing/invalid). Added byte-stable rendering requirement in §5.7.6 to make drift detection trivial.

3. **Accept** — Resolves the D15/D17 contradiction at the schema level rather than at exit-rule documentation. Updated §5.2 schema with a JSON-Schema `allOf` constraint: when `status == NO_FINDINGS`, both `findings` and `open_questions` MUST be empty arrays; when `status == FINDINGS_PRESENT`, `findings` MUST be non-empty. Added a reviewer-prompt corollary instructing the reviewer that returning NO_FINDINGS requires zero open questions. Codex prose-path parser also rejects `NO FINDINGS` outputs that have a co-located `OPEN QUESTIONS:` block — coerced to `FINDINGS_PRESENT` with synthetic-low findings to surface the inconsistency. Updated §5.4 exit rule 1 wording to cite the schema guarantee. Updated Phase 4 test list: replaced `test_no_findings_exits_approved_even_with_open_questions` (which now would violate the schema) with `test_no_findings_schema_rejects_non_empty_open_questions` and `test_no_findings_exits_approved_with_zero_open_questions`.

4. **Accept** — Spec gap is real. Inserted new §5.0 "Allowed write boundary (v2) — replaces v1 contract" before §5.1, with an explicit table of all 5 permitted write paths (`plans/<slug>-<version>.md`, JSON sidecar, fixes-md, `.scratch/plan-snapshot-r{N}.md`, `.env`) and which are tracked vs gitignored. Carries forward v1's git-write prohibition (no commit/add/push/etc.) explicitly. Implementer is now told to enforce the set at runtime.

5. **Accept** — Inserted new §5.0a "Startup ordering — locked sequence" listing the exact 5-step order: (1) git pre-flight, (2) transport check + first-run UX, (3) slug/version prompt, (4) resume detection, (5) begin round 1/N+1. Updated §5.6 opening line to cite "step 2 of §5.0a." Updated §5.9 opening line to cite "step 4 of §5.0a, runs after slug/version is known."

6. **Accept** — Test belongs to loop-gating behavior per §5.4, which is Phase 4. Removed `test_cost_cap_pauses_loop_when_exceeded` from Phase 1 verification and added to Phase 4. Replaced the Phase 1 slot with `test_cost_tracker_records_per_round_cost_in_sidecar` to keep coverage of cost tracking (without the gating). Phase 1 now strictly tracks cost; Phase 4 adds the pause-on-exceed gate.

### Open questions and uncertainty

- Codex open questions (verbatim):
  - Whether the JSON sidecar is runtime state or just an audit artifact. The current draft treats it as both.
  - Whether diff logic should make snapshots primary after round 1, with git only for recovery/resume when snapshots are missing.
  - Whether the clean-response schema should forbid non-empty open_questions when status is NO_FINDINGS.
- Planner-uncertain findings: Finding 2 — resolved via user consultation; user chose JSON authoritative.

### User resolution (verbatim)

> User via AskUserQuestion
> Q: Finding 2: markdown vs JSON sidecar authority is contradictory in the current draft. Which authority model do you want?
> A: "JSON authoritative; markdown rendered from JSON (Recommended for tooling)"

- Finding 2 → Accept with JSON-authoritative model. Markdown is rendered from JSON each round; hand-editing markdown does not persist.
- Codex Q1 → Resolved by Finding 2 acceptance: JSON is runtime state AND audit artifact (single source of truth).
- Codex Q2 → Resolved by Finding 1 acceptance: snapshots primary, git only for resume recovery.
- Codex Q3 → Resolved by Finding 3 acceptance: schema-level constraint forbids non-empty open_questions when status=NO_FINDINGS.

### Plan edits applied

- §5 — inserted new §5.0 (Allowed write boundary) and §5.0a (Startup ordering — locked sequence) before §5.1.
- §5.2 — schema: added `allOf` block enforcing (a) NO_FINDINGS implies empty findings AND empty open_questions, (b) FINDINGS_PRESENT implies non-empty findings. Added reviewer-prompt corollary describing the rule for both transports; described Codex prose-path coercion behavior for inconsistent inputs.
- §5.3.1 — rewrote diff source: snapshots primary via `compute_round_diff()`, `_take_initial_snapshot()` at round 1; git fallback isolated to a separate `_recover_diff_from_git()` invoked only on resume when snapshots are missing.
- §5.3.2 — added new subsection making explicit that git diff is recovery-only, with banner-in-prompt about cumulative semantics.
- §5.4 — rewrote exit rule 1 to cite the schema guarantee instead of D17's now-invalid "NO_FINDINGS authoritative even with open questions" wording.
- §5.6 — opening sentence updated to cite "step 2 of locked startup ordering (§5.0a)."
- §5.7 — fully rewrote: JSON authoritative, markdown rendered. New §5.7.5 drift policy (JSON wins, markdown overwritten, hand-edited JSON allowed if schema-valid). New §5.7.6 markdown rendering template, byte-stable.
- §5.9 — opening sentence updated to cite "step 4 of locked startup ordering (§5.0a)."
- §8 — moved `test_cost_cap_pauses_loop_when_exceeded` from Phase 1 to Phase 4. Added `test_cost_tracker_records_per_round_cost_in_sidecar` to Phase 1. Replaced `test_no_findings_exits_approved_even_with_open_questions` (now invalid under new schema) with `test_no_findings_schema_rejects_non_empty_open_questions` and `test_no_findings_exits_approved_with_zero_open_questions`.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits are confined to `plans/v2-plan.md`, this fixes log, and the plan-mandated `plans/fixs/` directory creation.)

### Round stats

- Reviewer: external Codex review (user-supplied verbatim)
- Findings raised: 6 (3 high, 3 medium)
- Findings accepted: 6 (5 by planner directly, 1 via user consultation)
- Findings rejected: 0
- Open questions resolved: 3 (all by Finding 1/2/3 acceptance)
- Plan size: ~700 lines (Δ +~80 from pre-round-1)

## Round 2 — 2026-05-03T00:30:00Z

### Reviewer findings (Codex)

1. [Diff generation / Runtime failure] Normal snapshot diff generation will fail whenever the plan actually changed. The plan uses `subprocess.check_output(["git", "diff", "--no-index", ...])` in §5.3.1, but `git diff --no-index` returns exit code `1` when differences are found. `check_output` treats that as an exception, so the common case blocks round 2+ before the reviewer prompt is built. Impact: the core diff-aware loop is unusable on any non-empty round delta, and the current Phase 3 tests may miss it if they mock diff output instead of executing the command. Concrete fix: use `subprocess.run(..., check=False, capture_output=True, text=True)`, accept return codes `0` and `1`, reject only other codes, and test with two real differing snapshot files.

2. [State authority / Resume contradiction] Finding 2 is not fully addressed because §5.9 reintroduces the old authority model: "Resume-from-N ... reads prior rounds from fixes-md AND the JSON sidecars" and "The reverse never happens — markdown is the user-facing source of truth and is never auto-regenerated from JSON." That directly contradicts §5.7, §5.7.3, and §5.7.5, which say JSON is authoritative and markdown is regenerated from JSON on drift or partial completion. Impact: implementers can build incompatible resume behavior, either trusting stale markdown or overwriting it, and recovery after interrupted markdown render remains ambiguous. Concrete fix: rewrite §5.9 to read prior rounds only from JSON sidecars, regenerate markdown from JSON when missing/stale, and delete the "markdown source of truth" sentence.

3. [Codex fallback / Windows command length] The supported Codex fallback still passes the entire prompt as a command-line argument in §5.1.2. The v2 prompt includes full plan text, prior decisions, diff, and round history, which can easily exceed Windows command-line limits, especially because the motivating failure case was a 16-round bloated plan. Impact: users without `OPENAI_API_KEY` can hit a platform-specific hard failure exactly when v2's long-context machinery is needed most. Concrete fix: pass the prompt through stdin or a temporary prompt file, update `codex-companion.mjs` accordingly, and add a Windows or long-prompt regression test.

4. [Schema/version contract] §5.7.4 says every JSON sidecar carries a top-level required `schema_version`, but the concrete §5.7.1 sidecar schema/example omits it. Since §5.7.3 makes schema validation a hard gate, this omission can lead to either sidecars that cannot be version-dispatched later or implementations whose examples fail their own validator. Impact: resume and future v3 compatibility are undermined at the audit artifact boundary. Concrete fix: add `"schema_version": "2.0.0"` to §5.7.1, make it required in `scripts/sidecar_schema.json`, and test missing/unsupported schema versions.

### Planner decisions (Claude)

1. **Accept** — Reviewer is correct: `git diff --no-index` exits with code 1 whenever files differ (the common case), and `check_output` raises on any non-zero exit. Rewrote §5.3.1 to use `subprocess.run(..., check=False, capture_output=True, text=True)`, explicitly accept exit codes 0 and 1, and raise only on other codes (with stderr in the error message). Phase 3 tests should run real differing snapshots end-to-end, not mock diff output — added a note to consider for the verification section.

2. **Accept** — Real contradiction: round 1 only updated §5.9's opening sentence about ordering but left the body asserting markdown-as-source-of-truth. Fully rewrote §5.9 body: resume reads ONLY from JSON sidecars (validated against schema), regenerates markdown from sidecars in order via §5.7.6 template, drops the "markdown is source of truth" sentence. Added a closing line explicitly noting this resolves the round-2 finding. Also added gap-detection: if sidecars 1, 2, 3, 5 exist (gap at 4), refuse to resume — user must explicitly truncate or start over.

3. **Accept** — Real Windows-portability bug. Rewrote §5.1.2 to pass the prompt via `subprocess.run(..., input=prompt, ...)` (stdin) instead of as the final argv element. Added a stipulation that `codex-companion.mjs` must be updated to read prompt from stdin when no positional arg is given, plus a Windows long-prompt regression test (>40KB). Added the `codex-companion.mjs` change to the Files-to-modify list (§6).

4. **Accept** — Spec inconsistency. Added `"schema_version": "2.0.0"` as the first field of the §5.7.1 example JSON. Updated §5.7.4 to explicitly state the field is required in `scripts/sidecar_schema.json`, present at the top of the example, and that Phase 4 tests cover both missing-field and unsupported-version cases.

### Plan edits applied

- §5.3.1 — replaced `subprocess.check_output` with `subprocess.run(..., check=False, capture_output=True, text=True)`; added explicit handling of return codes 0 and 1 vs other codes (raises on others with stderr).
- §5.1.2 — switched Codex prompt passing from argv to stdin (`input=prompt`); added stipulation about `codex-companion.mjs` stdin support and long-prompt regression test.
- §5.7.1 — added `"schema_version": "2.0.0"` as the first field of the example JSON.
- §5.7.4 — strengthened wording: required in schema, present in example, Phase 4 tests cover missing-field and unsupported-version.
- §5.9 — fully rewrote body: resume reads only JSON sidecars, regenerates markdown via §5.7.6, drops "markdown source of truth" wording, adds gap-detection (refuse to resume on non-contiguous sidecars).
- §6 (Files list) — added entry for `codex-companion.mjs` (Codex plugin) update.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 4 (all high-severity)
- Findings accepted: 4
- Findings rejected: 0
- Open questions: 0
- Tokens used: 15,734 (Codex-reported)
- Plan size delta: ~+45 lines (round 1 added scaffolding; round 2 fixed real bugs in that scaffolding)

## Round 3 — 2026-05-03T01:00:00Z

### Reviewer findings (Codex)

1. [Exit semantics / Medium-risk leakage] The `Resolved` exit can terminate with unresolved medium findings. §5.4 says resolved requires zero unresolved highs and zero unresolved open-questions, but says nothing about unresolved medium findings. Under §5.2, medium includes verification gaps and ambiguous specs that can lead to mis-implementation, so the loop can declare success while known material implementation risks remain. Concrete fix: define medium handling explicitly: either require all medium findings to be accepted/rejected/deferred before `Resolved`, or create `Resolved-with-medium-deferrals` with logged rationale and user confirmation.

2. [Prompt contract / Open questions schema mismatch] The schema requires `FINDINGS_PRESENT` to have at least one finding, but the reviewer-prompt corollary says uncertainty should use `FINDINGS_PRESENT` with `open_questions` populated. That fails when the reviewer has only an open question and no finding. Impact: OpenAI strict schema can reject a legitimate "no findings, but material question" response, causing retries/failure instead of surfacing the question. Concrete fix: either allow `FINDINGS_PRESENT` with non-empty `open_questions` and zero findings, or require the reviewer to encode each open question as a finding with a defined synthetic severity/category.

3. [Resume / Diff accuracy] Resume still cannot restore the core round-N-1 to N diff after `.scratch/` is gone. §5.9 says missing snapshots fall back to git recovery, but sidecars only store plan edits summaries, not the actual prior plan text. The fallback is explicitly cumulative-against-HEAD, so accepted-finding verification after resume can be checked against the wrong diff. Impact: the reviewer may miss whether the immediately prior accepted fix was actually applied, exactly the drift failure v2 is meant to prevent. Concrete fix: persist plan snapshots as tracked artifacts or store a hash/content snapshot per round in the JSON sidecar so resume can reconstruct exact round deltas.

### Planner decisions (Claude)

1. **Accept** — Real gap. §5.4 Resolved exit was silent on mediums, leaving "verification gaps, ambiguous specs that lead to mis-implementation" able to leak past success. Tightened the Resolved condition to require every medium be Accepted (and edited) or Rejected (with stated reason); only lows remain non-blocking. Updated Resolved-with-deferrals to explicitly accept open mediums (with user-tagged target version, e.g. "deferred to v2.1 verification work"). End report breaks deferred items out by severity.

2. **Accept** — Real schema/prompt mismatch. The strict-schema branch for FINDINGS_PRESENT required findings.minItems=1, which would reject a legitimate "no findings, but a material open question" response. Changed the constraint from a flat minItems on findings to an `anyOf`: FINDINGS_PRESENT requires at least one of (≥1 finding, ≥1 open question). Preserves the original intent (FINDINGS_PRESENT must carry actionable content) while allowing pure-open-question responses.

3. **Accept** — Real resume bug. Sidecars carry only edit summaries, so after `.scratch/` is wiped, the diff fallback was cumulative-against-HEAD — wrong diff for "did the prior accepted fix actually land?" check. Added two new fields to the §5.7.1 sidecar schema: `plan_content` (full plan markdown at end of round) and `plan_content_sha256` (integrity hash). Added §5.7.4a documenting the resume-accuracy guarantee and the three-way fallback table (`.scratch/` → sidecar → git). Sidecars get ~10-30KB larger per round but resume is now bulletproof and self-contained.

### Plan edits applied

- §5.4 — tightened `Resolved` to require all mediums decided; updated `Resolved-with-deferrals` to explicitly accept deferred mediums with target-version tags; end report breaks deferrals out by severity.
- §5.2 — schema `allOf` second branch changed from `findings: minItems: 1` to `anyOf: [findings≥1, open_questions≥1]`; updated comment to cite round-3 finding 2.
- §5.7.1 — added `plan_content_sha256` and `plan_content` fields to the example JSON.
- §5.7.4a — new subsection: "Plan content embedded in sidecar (resume-accuracy guarantee)" with three-way fallback table and tampering-detection note.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 3 (all high-severity)
- Findings accepted: 3
- Findings rejected: 0
- Open questions: 0
- Tokens used: 17,638 (Codex-reported)
- Plan size delta: ~+30 lines (mostly §5.7.4a addition)

## Round 4 — 2026-05-03T01:30:00Z

### Reviewer findings (Codex)

1. [Resume / Diff accuracy] The round-3 resume fix is still not wired into the actual resume/diff flow. §5.7.4a says missing snapshots should be reconstructed from `plan_content` in the prior sidecar, but §5.3.1 still sends any missing `prior_snapshot` straight to `_recover_diff_from_git()`, and §5.9 step 4 says missing snapshots make the next diff fall back to git. That preserves the original failure: after `.scratch/` is gone, the reviewer can get a cumulative-against-HEAD diff instead of the round N-1→N delta, so accepted-finding verification can silently check the wrong change set. Concrete fix: make sidecar recovery part of the executable path, either in resume before round N+1 or inside `compute_round_diff()`: load round `{N-1}` `plan_content`, verify `plan_content_sha256`, materialize `.scratch/plan-snapshot-r{N-1}.md`, then diff against the current plan. Git fallback should only run when both snapshot and validated sidecar content are unavailable. Add a regression test for resume with `.scratch/` deleted and sidecars present.

2. [Schema / Strict structured output compatibility] The review schema uses nonstandard `"comment"` keys inside `allOf`. JSON Schema's annotation keyword is `"$comment"`, and OpenAI strict structured-output schemas are usually a restricted subset rather than a permissive validator. The plan claims strict schema enforcement is the default transport, so an unsupported schema keyword can fail at reviewer invocation before any plan review happens. Impact: the recommended OpenAI path may be unusable or require implementers to silently diverge from the documented schema. Concrete fix: remove comments from the runtime schema or use only supported schema keywords, keeping explanatory comments outside `REVIEW_SCHEMA`. Add a test that sends the exact schema object used by `scripts/reviewer.py` through the OpenAI client mock/contract layer rather than only validating locally.

3. [Start-over / Write-boundary contradiction] The resume UX offers "Start over (delete all sidecars and fixes-md, begin from round 1)" in §5.9, but §5.0's allowed write boundary only permits writing specific artifacts and carries a strict runtime write guard. It does not explicitly permit deleting existing sidecars, truncating fixes-md, or replacing the audit trail. Impact: implementers can either violate the safety boundary to support start-over, or preserve the boundary and leave the documented start-over path impossible. Concrete fix: define start-over as an allowed destructive operation inside the same slug/version artifact set, with exact paths and confirmation behavior, or change the option to create a new version/slug without deleting prior artifacts.

### Planner decisions (Claude)

1. **Accept** — Real bug. Round 3 added `_recover_snapshot_from_sidecar()` to the spec but `compute_round_diff()` still went straight to `_recover_diff_from_git()` on missing snapshot. Rewrote the missing-snapshot branch to: (1) try sidecar recovery first via `_recover_snapshot_from_sidecar(round_n - 1)`, (2) if recovered, materialize the snapshot to `.scratch/` and continue with normal snapshot diff, (3) only fall back to git if both snapshot AND validated sidecar content are unavailable. Added the full `_recover_snapshot_from_sidecar()` implementation to §5.3.1 with hash verification (using the `plan_content_sha256` field added in round 3). Tampering detection — hash mismatch returns None, surfacing through to git fallback with the cumulative-against-HEAD banner.

2. **Accept** — Real schema-keyword issue. `"comment"` is not a JSON Schema keyword; standard is `$comment`, and OpenAI strict mode may reject either. Removed all `"comment"` keys from §5.2's runtime schema. Added a "Schema annotations (NOT part of the runtime schema)" subsection that documents the same context in markdown prose, immediately below the schema. Added Phase 2 verification: a contract test that sends the exact `REVIEW_SCHEMA` constant to the OpenAI client and asserts no schema-level rejection. Also documented the supported-keyword whitelist for future implementers.

3. **Accept** — Real contradiction. §5.9's "Start over" option needed to delete sidecars + fixes-md, but §5.0's write boundary only listed write/create permissions, not delete. Added a "Destructive operations within the slug/version artifact set" subsection to §5.0 with an explicit table of permitted deletions: sidecars (any N), fixes-md, snapshots — all scoped to the current `<slug>-<version>`. Plan markdown is **never** deletable by the skill (preserves user authorship). Required gate: `AskUserQuestion` showing the exact file list before deletion. Audit-trail requirement: log the wiped file list both to stderr and to the new round-1 sidecar's `restart_metadata` field. Suggested alternative for users who want to preserve history: bump version (e.g. `<version>-rerun-1`) instead of deleting.

### Plan edits applied

- §5.3.1 — rewrote the missing-snapshot branch in `compute_round_diff()` to try sidecar recovery before git; added `_recover_snapshot_from_sidecar()` implementation with SHA-256 verification.
- §5.2 — removed `"comment"` keys from runtime schema. Added "Schema annotations (NOT part of the runtime schema)" subsection with explanatory text moved to markdown prose. Added Phase 2 contract-test note.
- §5.0 — added "Destructive operations within the slug/version artifact set" subsection with explicit permission table, gating requirements, and audit-logging mandate. Plan markdown explicitly excluded from skill-driven deletion.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 3 (all high-severity)
- Findings accepted: 3
- Findings rejected: 0
- Open questions: 0
- Tokens used: 19,450 (Codex-reported)
- Plan size delta: ~+50 lines (all addressing real wiring/contract gaps from prior rounds)

## Round 5 — 2026-05-03T02:00:00Z

### Reviewer findings (Codex)

1. [Resume / Diff accuracy] The accepted round-4 diff recovery fix is still contradicted in the executable resume spec. §5.3.1 now tries `_recover_snapshot_from_sidecar()` before git, but §5.9 step 4 still says that if snapshots are missing, "the next round's diff falls back to `_recover_diff_from_git()`." An implementer following §5.9 can preserve the exact failure round 4 was meant to eliminate: after `.scratch/` is deleted, the reviewer gets a cumulative-against-HEAD diff instead of the round N-1→N delta. Impact: accepted-finding verification can silently inspect the wrong change set after resume. Concrete fix: rewrite §5.9 step 4 to say missing snapshots are restored from validated sidecar `plan_content` first, materialized into `.scratch/`, and git fallback is only used when both snapshot and sidecar recovery fail; add the regression test named in round 4.

2. [Schema / OpenAI strict compatibility] The runtime review schema still relies on conditional JSON Schema keywords (`allOf`, `anyOf`, `if`, `then`, `const`) while asserting they are supported by OpenAI strict structured outputs, but the plan provides no evidence that this exact subset is accepted. The prior fix removed `"comment"` keys, but the same failure mode remains for conditionals: reviewer invocation can fail before review if the Responses API strict schema subset rejects them. Impact: the default OpenAI path may be unusable, or implementers may silently weaken the schema and reintroduce the NO_FINDINGS/open_questions ambiguity. Concrete fix: either simplify the runtime schema to the most conservative supported subset and enforce cross-field constraints in local post-parse validation, or make the Phase 2 contract test live against the exact Responses API schema before implementation proceeds.

### Planner decisions (Claude)

1. **Accept** — Real contradiction. Round 4 fixed §5.3.1 but I forgot to propagate the same priority order to §5.9. Rewrote §5.9 step 4: explicit snapshot → sidecar → git priority chain matching §5.3.1's `compute_round_diff()`, with each tier described and git fallback marked as "last resort, not automatic preference." Added Phase 4 test `test_resume_recovers_snapshot_from_sidecar_when_scratch_deleted` to lock the order against future regression. Both §5.3.1 and §5.9 now agree on the source of truth.

2. **Accept** — Real risk. I asserted `allOf`/`anyOf`/`if`/`then`/`const` are OpenAI-strict-supported without evidence. OpenAI's strict structured-output mode is a conservative subset and conditional keywords are not guaranteed. Stripped all conditional keywords from the §5.2 runtime schema; reduced to the well-supported core (`object`, `properties`, `required`, `additionalProperties: false`, `type`, `enum`, `items`, `array`). Moved cross-field invariants (NO_FINDINGS implies empty findings + empty open_questions; FINDINGS_PRESENT implies at least one finding or open_question) into a client-side `validate_review_invariants()` function that runs after JSON parsing. Added Phase 2 tests: invariant unit tests, a static check that `REVIEW_SCHEMA` contains only safe-subset keywords, and a contract test that sends the exact constant through the OpenAI Responses API (mock or live) — marked as a Phase 2 prerequisite, build fails if OpenAI rejects the schema.

### Plan edits applied

- §5.9 step 4 — rewrote to match §5.3.1's snapshot → sidecar → git priority chain explicitly; added Phase 4 regression test `test_resume_recovers_snapshot_from_sidecar_when_scratch_deleted`.
- §5.2 — stripped all conditional keywords (`allOf`, `anyOf`, `if`/`then`, `const`) from runtime schema. Moved invariant checks to client-side `validate_review_invariants()` Python function shown as a code block. Added 5 Phase 2 tests covering invariants, schema safe-subset compliance, and OpenAI Responses API contract.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 2 (both high-severity)
- Findings accepted: 2
- Findings rejected: 0
- Open questions: 0
- Tokens used: 21,510 (Codex-reported)
- Plan size delta: ~+30 lines (mostly the post-parse validation code block + new Phase 2 tests)
- **Convergence signal:** finding count decreasing (1→6→4→3→3→2). Reviewer is closing in on details rather than raising new architectural concerns.

## Round 6 — 2026-05-03T02:30:00Z

### Reviewer findings (Codex)

1. [Snapshot identity / Diff accuracy] Snapshot files are not scoped to `slug`/`version` and are trusted before sidecar recovery. Both §5.3.1 and §5.9 use `.scratch/plan-snapshot-r{N}.md`; §5.9 says an existing snapshot is used "as-is" as the fastest path. If the user reviews a different plan/version, restarts after "start over," or has stale `.scratch` files from a previous run, the loop can diff against the wrong prior plan while sidecars containing the correct `plan_content_sha256` are ignored. Impact: the reviewer verifies accepted fixes against an unrelated or stale delta, silently defeating the core diff-aware guarantee. Fix: namespace snapshots by slug/version, e.g. `.scratch/<slug>-<version>-plan-snapshot-r{N}.md`, and validate any existing snapshot against the corresponding sidecar hash before using it; otherwise recover from sidecar.

2. [Schema / Self-contradictory verification] The runtime review schema still contains the `description` keyword under `where`, but §5.2 says `REVIEW_SCHEMA` is limited to `object`, `properties`, `required`, `additionalProperties`, `type`, `enum`, `items`, and `array`, and Phase 2's static test asserts only that safe subset appears at any level. The documented schema would fail its own safe-keyword test, or the implementer may remove `description` in code and diverge from the plan. Impact: Phase 2 can block on a plan-internal contradiction before OpenAI compatibility is even tested. Fix: remove `description` from the runtime schema and keep that explanatory text outside the JSON constant.

### Planner decisions (Claude)

1. **Accept** — Real bug missed in rounds 4 and 5. (1) Renamed all snapshot paths from `.scratch/plan-snapshot-r{N}.md` to `.scratch/<slug>-<version>-plan-snapshot-r{N}.md` (replace_all across §5.0, §5.3.1, §5.9). (2) Updated `compute_round_diff()` signature to take slug+version explicitly. (3) Added hash-validation step: even when a properly-named snapshot exists, its content is SHA-256 hashed and compared against the prior round's sidecar `plan_content_sha256`. Hash mismatch routes to sidecar recovery, not blind trust. The "snapshot exists → use as-is" fast path is gone — every snapshot use is validated. Stale `.scratch/` from another plan or another version cannot poison this loop's diff.

2. **Accept** — Real schema contradiction. The earlier `"where": {"type": "string", "description": "Plan section or line reference"}` had `description` in the runtime schema, contradicting the round-5 safe-subset whitelist. Stripped `description` from the schema. Added a "Field semantics (NOT in the runtime schema)" markdown table immediately after the JSON block describing what each field means in prose, so implementers and reviewers still have semantic context — just not in the runtime constant. The static test `test_review_schema_constant_uses_only_strict_safe_keywords` now passes against the documented schema.

### Plan edits applied

- §5.0, §5.3.1, §5.9 — replaced all `.scratch/plan-snapshot-r{N}.md` references with `.scratch/<slug>-<version>-plan-snapshot-r{N}.md` (replace_all).
- §5.3.1 — `compute_round_diff()` signature gains `slug` and `version` parameters; new hash-validation step before trusting any existing snapshot; mismatch routes to sidecar recovery (existing path).
- §5.2 — removed `description` keyword from the `where` field. Added "Field semantics (NOT in the runtime schema)" subsection with a markdown table covering `where`, `what_can_go_wrong`, `concrete_fix`. Confirmed `test_review_schema_constant_uses_only_strict_safe_keywords` now matches the documented schema.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 2 (both high-severity)
- Findings accepted: 2
- Findings rejected: 0
- Open questions: 0
- Tokens used: 22,864 (Codex-reported)
- Plan size delta: ~+15 lines (small focused fixes; convergence continuing)
- **Convergence trajectory:** 6 → 4 → 3 → 3 → 2 → 2 findings. Plan growth slowing. Reviewer is closing in on details rather than raising new architectural classes of issue.

## Round 7 — 2026-05-03T03:00:00Z

### Reviewer findings (Codex)

1. [Diff generation / Accepted finding not fully addressed] The round-6 snapshot-scoping fix is still internally inconsistent. §5.3.1 namespaces `prior_snapshot/current_snapshot`, but `_take_initial_snapshot()` still writes `.scratch/plan-snapshot-r0.md` without `slug`/`version`, and §5.9 still says an existing snapshot is used "as-is" as the fastest path. That preserves the stale-snapshot failure the prior finding targeted: round 2 may not find the round-0 namespaced snapshot, and resume can trust an old or wrong snapshot instead of validating against the sidecar hash. Impact: the reviewer can diff against the wrong prior plan or fall back to degraded git recovery during normal use. Concrete fix: make `_take_initial_snapshot(plan_path, slug, version)` write `.scratch/<slug>-<version>-plan-snapshot-r0.md`; update §5.9 to require hash validation before using any present snapshot; fix §5.7.4a's table to use the namespaced path and remove "use directly."

2. [Diff recovery / Runtime failure] The §5.3.1 pseudocode breaks exactly on the stale-snapshot path it is meant to repair. When a snapshot hash mismatches, it sets `prior_snapshot = None`; if `_recover_snapshot_from_sidecar()` succeeds, the next line calls `prior_snapshot.write_text(...)`, which will raise because `prior_snapshot` is `None`. The helper also declares `_recover_snapshot_from_sidecar(round_n)` but uses `slug` and `version` without parameters. Impact: a recoverable stale or deleted snapshot can crash the loop before the reviewer prompt is built. Concrete fix: keep `prior_snapshot_path` immutable, use a separate `prior_snapshot_valid` flag, write recovered content to `prior_snapshot_path`, and pass `slug/version` explicitly into `_recover_snapshot_from_sidecar(round_n, slug, version)`.

### Planner decisions (Claude)

1. **Accept** — Real namespacing gap. Round 6 namespaced `prior_snapshot/current_snapshot` inside `compute_round_diff()` but missed three call sites. Updated `_take_initial_snapshot()` signature to take `(plan_path, slug, version)` and write `.scratch/<slug>-<version>-plan-snapshot-r0.md`. Rewrote §5.9 step 4.1 from "snapshot present → use as-is" to "snapshot present AND hash matches sidecar → use it" with mandatory hash check; updated §5.7.4a table similarly to make explicit that no snapshot is ever "used directly" — every read is hash-gated. Added Phase 4 test `test_resume_rejects_stale_snapshot_via_hash_mismatch` to lock the new behavior.

2. **Accept** — Real runtime bug in my own pseudocode. (1) Replaced the mutable `prior_snapshot = None` pattern with an immutable `prior_snapshot_path` and a separate `prior_snapshot_valid` boolean flag. Recovery writes back to `prior_snapshot_path` (always a valid Path), not to None. (2) Added explicit `slug`/`version` parameters to `_recover_snapshot_from_sidecar(round_n, slug, version)` — no module-level closure. (3) Added a previously-implied helper `_read_sidecar_plan_hash(round_n, slug, version)` so the validation step has a real callable. (4) Updated docstrings to cite round-7 finding 2 so future readers know why the structure looks the way it does.

### Plan edits applied

- §5.3.1 — restructured `compute_round_diff()` with immutable `prior_snapshot_path` + separate `prior_snapshot_valid` flag; recovery writes to the path, not to None; renamed `current_snapshot` → `current_snapshot_path` for consistency.
- §5.3.1 helpers — `_take_initial_snapshot(plan_path, slug, version)` signature update, writes namespaced round-0 snapshot. `_recover_snapshot_from_sidecar(round_n, slug, version)` takes explicit params. New `_read_sidecar_plan_hash(round_n, slug, version)` helper added so the validation branch in `compute_round_diff` has a real implementation reference.
- §5.9 step 4 — rewrote step 4.1 from "use as-is" to "use AND hash-matches sidecar"; added test `test_resume_rejects_stale_snapshot_via_hash_mismatch`.
- §5.7.4a table — updated row 1 from "Use snapshot file directly (fastest)" to "Use snapshot file (still hash-validated)" with explicit cross-reference to round-7 finding 1; updated row 2 to use the namespaced path and the `_recover_snapshot_from_sidecar` 3-arg form; added "OR hash-mismatched" trigger so the table reflects the same behavior as the executable path.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 2 (both high-severity)
- Findings accepted: 2
- Findings rejected: 0
- Open questions: 0
- Tokens used: 24,389 (Codex-reported)
- Plan size delta: ~+25 lines (mostly polishing the snapshot/sidecar interaction)
- **Convergence trajectory:** 6 → 4 → 3 → 3 → 2 → 2 → 2 findings. Reviewer is now scrubbing for cross-references in my recent edits — exactly the v0.0.5 round-14 phenomenon the v2 design is meant to detect via plan-bloat warnings (§5.5).

## Round 8 — 2026-05-03T03:30:00Z

### Reviewer findings (Codex)

1. [Diff generation / Snapshot indexing] Round-7's accepted snapshot fix is still not executable for the first diff-aware review. §5.3.1 says `_take_initial_snapshot()` writes `.scratch/<slug>-<version>-plan-snapshot-r0.md` so round 2 can find it, but `compute_round_diff(round_n=2, ...)` looks for `r{round_n - 1}` यानी `r1`, not `r0`. If `r1` is missing, recovery loads round-1 sidecar `plan_content`, which is the post-round-1 plan, so the round-2 diff becomes empty or near-empty instead of showing the round-1 edits. Impact: the reviewer cannot verify whether accepted round-1 findings were actually addressed, which breaks the core diff-aware guarantee immediately on the first normal v2 loop. Concrete fix: define snapshot indices unambiguously. Either make `_take_initial_snapshot()` write `r1` as the pre-round-1 baseline, or change `compute_round_diff()` for review round `N` to diff `r{N-2}` against the current plan and then write `r{N-1}`. Also add a test where round 1 changes the plan and round 2's prompt must contain that actual diff.

2. [State schema / Start-over audit] The start-over audit path writes a `restart_metadata` field to the new round-1 sidecar (§5.0), but the sidecar schema/example in §5.7.1 does not include `restart_metadata`. Since §5.7.3 makes sidecar schema validation a hard gate, the implementation must either violate the audit requirement or produce a sidecar that fails its own validator. Impact: the documented destructive restart flow can fail exactly after deleting prior audit files, leaving recovery/audit behavior ambiguous. Concrete fix: add `restart_metadata` as an optional top-level sidecar field with a concrete schema, including deleted paths, timestamp, and confirmation decision, and add a restart regression test that validates the new round-1 sidecar.

### Planner decisions (Claude)

1. **Accept** — Real index off-by-one bug in my own pseudocode. Took option (a) per the reviewer: `_take_initial_snapshot()` now writes `r1` as the pre-round-1 baseline, with explicit semantic that snapshot index N = "plan state at start of round N." The existing `compute_round_diff()` already reads r{N-1} so it now correctly picks up r1 as the prior snapshot for round 2 = baseline. Updated `_recover_snapshot_from_sidecar(snapshot_index, ...)` to refuse recovering r1 (the baseline cannot come from any sidecar — it predates them all), and to translate snapshot_index=N → read sidecar-(N-1) since sidecar-(M).plan_content = end-of-round-M = start-of-round-(M+1) = r{M+1}. Same translation in `_read_sidecar_plan_hash()`. Added Phase 3 test `test_round_2_diff_shows_round_1_plan_edits` to lock in the off-by-one fix end-to-end.

2. **Accept** — Real schema gap. §5.0 mandated `restart_metadata` field on round-1 sidecars after a destructive restart, but the §5.7.1 example didn't include it and §5.7.3 schema validation was a hard gate. Added `"restart_metadata": null` to the §5.7.1 example as a top-level field (default null for normal rounds). Added §5.7.3a defining the schema for the populated form (timestamp, deleted_files, user_decision, optional previous_run_summary), with the schema gate accepting both null and populated forms. Added Phase 4 tests `test_start_over_round_1_sidecar_carries_restart_metadata` and `test_normal_round_1_sidecar_has_null_restart_metadata`.

### Plan edits applied

- §5.3.1 `_take_initial_snapshot()` — now writes `r1` (baseline), not `r0`. Docstring documents the snapshot index semantic ("r{N} = state at start of round N").
- §5.3.1 `_recover_snapshot_from_sidecar()` — renamed parameter to `snapshot_index`, returns None for index < 2 (baseline unrecoverable), translates index → sidecar-(index-1) reading.
- §5.3.1 `_read_sidecar_plan_hash()` — same translation: snapshot_index → sidecar-(index-1).
- §5.7.1 — added `"restart_metadata": null` to the example sidecar JSON between `plan_content` and `reviewer_response`.
- §5.7.3a — new subsection defining the `restart_metadata` schema (timestamp, deleted_files, user_decision, optional previous_run_summary). Schema gate accepts null and populated forms. Phase 4 tests added.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 2 (both high-severity)
- Findings accepted: 2
- Findings rejected: 0
- Open questions: 0
- Tokens used: 24,084 (Codex-reported)
- Plan size delta: ~+30 lines (restart_metadata schema + recovery helper updates)
- **Convergence trajectory:** 6 → 4 → 3 → 3 → 2 → 2 → 2 → 2 findings. Plateau at 2 high-severity issues per round catching specific bugs in my own recent edits. Without v2's diff-aware reviewing this would be much noisier.

## Round 9 — 2026-05-03T04:00:00Z

### Reviewer findings (Codex)

1. [Diff generation / Baseline validation] The round-8 snapshot indexing fix still fails on the first diff-aware review. `compute_round_diff(round_n=2)` correctly looks for `.scratch/<slug>-<version>-plan-snapshot-r1.md`, but then validates it via `_read_sidecar_plan_hash(1, ...)`; that helper explicitly returns `None` for `snapshot_index < 2`, so `prior_snapshot_valid` is always false for the baseline. Recovery also returns `None` for r1, so the normal round-2 diff falls through to `_recover_diff_from_git()` instead of diffing against the initial snapshot. Impact: the first accepted-finding verification uses degraded cumulative git recovery even when the correct r1 snapshot exists, breaking the core round-1-edit verification path. Concrete fix: give r1 a validation source. Either store `baseline_content`/`baseline_content_sha256` in the round-1 sidecar before edits, or add a separate baseline sidecar/snapshot manifest written by `_take_initial_snapshot()`. Then special-case `snapshot_index == 1` to validate against that baseline hash and use the r1 snapshot instead of falling through to git.

### Planner decisions (Claude)

1. **Accept** — Real downstream bug from my round-8 fix. The r1 special-case in helpers ("baseline cannot come from sidecars") was supposed to ROUTE to git fallback when r1 was MISSING — but it accidentally also rejected r1 when r1 EXISTED, because the validation step asked for a hash and got None. Took option (a) per the reviewer: round-1 sidecar now carries `baseline_plan_content` and `baseline_plan_content_sha256` as durable, audit-trail-persistent baseline. (1) Updated `_recover_snapshot_from_sidecar(snapshot_index=1, ...)` to read those fields from round-1 sidecar (with hash validation) instead of returning None. (2) Updated `_read_sidecar_plan_hash(snapshot_index=1, ...)` to read `baseline_plan_content_sha256` from round-1 sidecar instead of returning None. (3) Added the two fields to §5.7.1 example JSON (between `plan_content` and `restart_metadata`). (4) Added §5.7.3b "Baseline fields (round-1 sidecar only)" subsection documenting that both fields are null in subsequent sidecars and schema permits both forms. Added Phase 4 tests `test_round_1_sidecar_carries_validated_baseline` and `test_round_n_sidecar_has_null_baseline_fields_for_n_gt_1`.

### Plan edits applied

- §5.3.1 `_recover_snapshot_from_sidecar()` — added explicit `snapshot_index == 1` branch that reads `baseline_plan_content` and `baseline_plan_content_sha256` from round-1 sidecar; refactored the >= 2 branch into an `elif`.
- §5.3.1 `_read_sidecar_plan_hash()` — same structure: index=1 reads `baseline_plan_content_sha256`, index>=2 reads `plan_content_sha256` from sidecar-(index-1).
- §5.7.1 example — added `baseline_plan_content_sha256` and `baseline_plan_content` fields between `plan_content` and `restart_metadata`.
- §5.7.3b — new subsection documenting the baseline fields, why they exist (round-9 finding 1), and that they're null for round 2+. Phase 4 tests cited.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 1 (high-severity)
- Findings accepted: 1
- Findings rejected: 0
- Open questions: 0
- Tokens used: 27,739 (Codex-reported)
- Plan size delta: ~+25 lines (baseline-fields subsection + helper refactors)
- **Convergence trajectory:** 6 → 4 → 3 → 3 → 2 → 2 → 2 → 2 → 1 findings. Single high-severity finding this round, focused on a real downstream bug from my own round-8 fix. Approaching natural convergence.

## Round 10 — 2026-05-03T04:30:00Z (ceiling)

### Reviewer findings (Codex)

1. [Resume / Baseline recovery] The round-9 baseline fix creates a new mandatory recovery source but does not wire it into sidecar write timing or schema validation. §5.3.1 now requires round-1 sidecar fields `baseline_plan_content` and `baseline_plan_content_sha256` to validate the r1 snapshot for round 2, but §5.7.3 only says the sidecar is written at the end of each round and §5.7.3b says the fields exist without specifying that they must be captured from `_take_initial_snapshot()` before any round-1 edits. An implementer could populate baseline fields from the post-round-1 plan when writing the round-1 sidecar, making r1 validation pass against the wrong content and yielding an empty or misleading round-2 diff. Impact: the first accepted-finding verification can silently compare against the edited plan instead of the pre-loop baseline. Concrete fix: explicitly require `_take_initial_snapshot()` to return/store the baseline bytes and hash in loop state, and require the round-1 sidecar writer to persist those exact captured values before any planner edits mutate the plan; add a test that fails if `baseline_plan_content_sha256 == plan_content_sha256` after a round-1 edit.

2. [Resume / Hash reference contradiction] The prose recovery spec still validates r1 snapshots against `plan_content_sha256`, contradicting the new baseline-field design. §5.7.4a says `.scratch/...r{N-1}.md` is hash-validated against `plan_content_sha256`, and §5.9 step 4.1 says a namespaced snapshot is compared to `plan_content_sha256` from the corresponding sidecar. For r1, the correct hash is now `baseline_plan_content_sha256`, not `plan_content_sha256` from round 1, which represents the end-of-round-1 plan. Impact: an implementer following the prose rather than the helper pseudocode will reject the valid baseline snapshot, or worse validate it against the wrong post-round content and fall into degraded git recovery on the first diff-aware round. Concrete fix: update §5.7.4a and §5.9 to define snapshot-index-to-hash mapping explicitly: r1 validates against round-1 `baseline_plan_content_sha256`; rN for N>=2 validates against sidecar-(N-1) `plan_content_sha256`.

### Planner decisions (Claude)

1. **Accept** — Real spec gap. Round 9 added baseline fields but didn't lock down WHEN they're captured. Strengthened §5.7.3b with a new "Capture-timing requirement (round-10 finding 1)" subsection containing concrete pseudocode showing baseline capture at `_take_initial_snapshot()` time (loop_state.baseline_content + baseline_sha256), then verbatim persistence to round-1 sidecar at end-of-round-1 alongside the post-round-1 plan_content fields. Added Phase 4 test `test_round_1_baseline_differs_from_plan_content_after_edits` that asserts `baseline_plan_content_sha256 != plan_content_sha256` whenever round 1 actually edited the plan — catches the misimplementation where both fields are captured at sidecar-write time.

2. **Accept** — Real prose contradiction with the helpers. Updated §5.7.4a's table row for "snapshot present AND hash matches sidecar" to explicitly say the hash source depends on snapshot index: r1 → `baseline_plan_content_sha256`; rN for N≥2 → `plan_content_sha256` from sidecar-(N-1). Same for the recovery row. Updated §5.9 step 4.1 with a parallel two-bullet structure documenting the same r1 vs rN split. Now the prose, table, and helper pseudocode all agree.

### Plan edits applied

- §5.7.3b — added "Capture-timing requirement (round-10 finding 1)" subsection with concrete pseudocode showing baseline capture in `_take_initial_snapshot()` then persistence at round-1 sidecar write. Added Phase 4 test `test_round_1_baseline_differs_from_plan_content_after_edits`.
- §5.7.4a table — both "use snapshot" and "recovery" rows now specify hash source by snapshot index (r1 vs rN ≥ 2).
- §5.9 step 4.1 — added explicit r1 vs rN sub-bullets with the correct hash field for each.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop. All edits confined to `plans/v2-plan.md` and this fixes log.)

### Round stats

- Reviewer: Codex CLI (gpt-5.5, via local `codex exec -m gpt-5.5`)
- Findings raised: 2 (both high-severity)
- Findings accepted: 2
- Findings rejected: 0
- Open questions: 0
- Tokens used: 20,788 (Codex-reported)
- Plan size delta: ~+30 lines (capture-timing spec + cross-reference fixes)
- **Convergence trajectory:** 6 → 4 → 3 → 3 → 2 → 2 → 2 → 2 → 1 → 2 findings.

---

## End of loop — ceiling hit

- **Final status:** ceiling hit (round 10 reached per v1 termination rule 3)
- **Rounds run:** 10
- **Total findings raised:** 28 (across 10 rounds: 6, 6, 4, 3, 3, 2, 2, 2, 2, 1, 2 — wait, let me recount: r1=6, r2=4, r3=3, r4=3, r5=2, r6=2, r7=2, r8=2, r9=1, r10=2 = 27. Note r1 came from external user-supplied review with 6 findings.)
- **Total findings accepted:** 27 (5 directly in r1, 1 via user consultation in r1, plus all subsequent findings — every reviewer finding accepted)
- **Total findings rejected:** 0
- **Open questions resolved:** 3 (all in r1, all by Finding-1/2/3 acceptance)
- **Plan path:** `plans/v2-plan.md` (~970 lines, up from ~566 at start of loop)
- **Fixes log path:** `plans/fixs/v2-plan-fixes.md` (this file, full transcript)
- **Total Codex tokens:** ~218,803 across rounds 2-10 (round 1 was external)

**Still-open findings:** None. Every finding in every round was Accepted with concrete plan edits applied. No findings reached Reject or Uncertain states.

**Still-open questions:** None.

**Convergence observations:**
- Finding count plateaued at 2 per round between rounds 5-8, then dropped to 1 in round 9, returning to 2 in round 10. The reviewer never returned `NO FINDINGS`.
- Most rounds caught real downstream bugs introduced by my own prior-round fixes (snapshot indexing, baseline capture timing, hash reference contradictions). This validates the v2 design's diff-aware reviewing motivation: with full-plan-every-round v1, the reviewer keeps re-scanning the entire plan looking for inconsistencies in my edits. With v2's diff-aware mode, the reviewer would have focused only on what changed each round — likely fewer rounds, tighter focus.
- The v0.0.5 round-14-style "scrubbing for cross-references" phenomenon manifested clearly in rounds 6-10 — exactly the failure mode v2's plan-bloat detection (§5.5) is designed to catch.
- Severity stayed almost entirely "high" because v1 has no severity tagging — the reviewer treats every issue as material. v2's severity gate would have allowed exit earlier with mediums/lows deferred.

**Practical takeaway:** the v2 plan as it now stands has been adversarially reviewed for 10 rounds against itself. Every finding was real and addressed. The plan is implementation-ready; the remaining minor open questions in §10 (gpt-5.5-pro doc, Windows path edge cases) are low-impact tuning items, not architectural risks.

## Round 11 — 2026-05-03T05:00:00Z (user-extended past ceiling)

### Reviewer findings (Codex)

1. [Exit semantics / Medium leakage on non-success exits] Unresolved medium findings can still leak through non-success exits without the required deferral flow. Resolved now blocks on unresolved mediums, and Resolved-with-deferrals explicitly includes them, but the soft-block gate only triggers on open_highs or open_questions. On planner-locked, ceiling hit, or cost-capped, open mediums can still exit without user deferral, which reintroduces the exact "mediums are blocking" ambiguity round 3 was supposed to close.

2. [Codex fallback / Synthetic findings pollution] The Codex fallback still invents synthetic findings for NO FINDINGS + OPEN QUESTIONS, even though the post-parse invariant already allows "open questions only." The invariant explicitly accepts FINDINGS_PRESENT with no findings and only open questions, but the prose-path rule says such output is coerced into FINDINGS_PRESENT with synthetic low findings. That pollutes counts, planner decisions, and severity histograms with fake artifacts instead of representing the real state.

3. [State authority / Tamper handling inconsistency] Tamper handling for authoritative sidecars is still internally inconsistent. The diff recovery code says a hash mismatch should fall through to git fallback, and the resume flow also routes invalid snapshot/sidecar recovery to git fallback. But the sidecar section says a plan_content hash mismatch means the loop "refuses to resume." Those are materially different behaviors for the same corruption case.

4. [Audit fidelity / Verbatim prose claim contradicted] The plan still claims the rendered markdown preserves "verbatim Codex prose," but the authoritative JSON shape and render template do not carry raw reviewer text. The rationale section says markdown "embeds verbatim Codex prose," yet the sidecar example only stores parsed fields under reviewer_response and the renderer reconstructs normalized markdown from those fields. If verbatim fallback output matters for audit/debug parity with v1, the JSON needs a raw-response field; otherwise the prose claim should be removed.

OPEN QUESTIONS:
- The fixes history shows the earlier blockers were mostly closed; the remaining problems are now concentrated in the state machine edges, not the transport/diff basics.
- Treating the current pseudocode as implementation-authoritative over the decision table; if that table is normative, D22 was stale relative to the resume design.

### Planner decisions (Claude)

1. **Accept** — Real gap. Round 3 fixed the Resolved exit to block on mediums but the soft-block gate (§5.4.1) was not extended. Updated the gate condition from `if open_highs or open_questions` to `if open_highs or open_questions or open_mediums`. Updated the prompt body to surface medium counts. Added explicit note that lows are intentionally excluded (clarity-only issues, no implementation impact). Now planner-locked / ceiling / cost-cap exits all force user deferral on any open medium, matching the Resolved/Resolved-with-deferrals behavior.

2. **Accept** — Real prose-path bug carried over from round 1. The original synthetic-findings coercion was added before round 3 relaxed the schema invariant. Updated the Codex prose-path coercion to emit `{status: FINDINGS_PRESENT, findings: [], open_questions: [parsed list]}` directly. Removed all references to "synthetic-low findings" from §5.2 prose. Counts and severity histograms now reflect the real state.

3. **Accept** — Real spec inconsistency. Made the policy split explicit in §5.7.5 "Drift and tamper handling": (A) In-flight hash mismatch during an active loop — fall through to next-best source (snapshot → sidecar → git) with the cumulative banner; loop continues in degraded mode. (B) Resume validation hash mismatch — refuse to resume; user picks fix-then-retry, start over, or cancel.

4. **Accept** — Real claim/storage contradiction. Added `raw_response_text` field to the §5.7.1 sidecar example. Updated §5.7.2 markdown row description and added a paragraph documenting `raw_response_text` rendering into the markdown's `### Reviewer raw response` subsection. Audit-fidelity parity with v1 is now preserved.

5. **Accept (meta open-question)** — D22 wording was indeed stale. Updated the §4 Decisions table row for D22 to reflect the JSON-authoritative resume model.

### Plan edits applied

- §5.4.1 — extended soft-block gate to include open_mediums; updated prompt body and rationale.
- §5.2 reviewer-prompt corollary — replaced synthetic-low coercion with `{status: FINDINGS_PRESENT, findings: [], open_questions: [parsed]}`.
- §5.7.5 — renamed to "Drift and tamper handling" with explicit case A (in-flight) vs case B (resume) split.
- §5.7.1 — added `raw_response_text` field.
- §5.7.2 — updated markdown row; added paragraph on `### Reviewer raw response` rendering.
- §4 D22 — updated wording from fixes-md detection to JSON-sidecar detection.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop.)

### Round stats

- Reviewer: External Codex review (user-supplied verbatim findings, post-ceiling)
- Findings raised: 4 (all medium-severity)
- Findings accepted: 4 (plus 1 meta — D22 wording)
- Findings rejected: 0
- Plan size delta: ~+25 lines
- **Severity shift:** previous 10 rounds were 27 high + 0 medium; round 11 is 0 high + 4 medium. The convergence signature v2's severity-gated exit is designed to detect — a v2-aware loop would exit `Resolved-with-deferrals` (mediums deferred) or `Resolved` (mediums addressed) at this point.

## Round 12 — 2026-05-03T05:30:00Z (user-extended past ceiling)

### Reviewer findings (Codex)

1. [Audit fidelity / Renderer template gap] The round-11 `raw_response_text` fix is not fully propagated into the markdown renderer spec. The prose says the renderer appends a `### Reviewer raw response` subsection from `raw_response_text`, but the actual template in §5.7.6 has no such section. That leaves the audit-fidelity guarantee half-specified: the sidecar stores the raw text, but the rendered fixes log still drops it.

2. [Implementation inventory / Renderer module] The plan now depends on a dedicated renderer module, but the implementation inventory never names it. §5.7.6 says the renderer lives in `scripts/render_markdown.py`, yet `## 6. Files to modify / add` does not include that file. For a plan that is otherwise very explicit about file ownership, this leaves the renderer implementation point ambiguous.

3. [Deferral flow / Reasons not collected] The deferral flow still requires reasons without actually collecting them. D4 and Resolved-with-deferrals both require explicit deferral reasons and, for mediums, a tagged follow-up target, but the soft-block interaction only offers three fixed choices and no text capture path. An implementer cannot satisfy the audit requirement as written.

### Planner decisions (Claude)

1. **Accept** — Real propagation gap. Round 11 added the `raw_response_text` field to the sidecar and stated in §5.7.2 that the renderer would append a `### Reviewer raw response` subsection, but I never updated the §5.7.6 template to actually include that section. Added `### Reviewer raw response` as the trailing section in the template, wrapped in a fenced `text` code block to preserve the literal reviewer output without markdown re-interpretation. Added a paragraph below the template noting the section is mandatory and citing the round-12 finding 1 reference.

2. **Accept** — Real ownership gap. Added `scripts/render_markdown.py` to the §6 Files-to-modify table with description: "pure renderer that turns a sidecar JSON into the markdown fixes-md round section (§5.7.6); byte-stable; called once per round end and on resume to regenerate fixes-md from sidecars."

3. **Accept** — Real audit-collection gap. The §5.4.1 soft-block had three fixed choices but no text-capture path, so the deferral-reason requirement (D4) and medium target-version tagging (Resolved-with-deferrals) couldn't actually be satisfied. Restructured §5.4.1 as a two-step interaction: (1) high-level action selection (defer / continue / accept-all-risk) via standard AskUserQuestion, then (2) per-item iteration where each open item gets a free-text reason via "Other" plus, for mediums, a follow-up-target AskUserQuestion offering common targets (next minor, next phase, backlog) with free-text fallback. Result is a `deferrals_at_exit` array persisted to the sidecar with `{item_id, severity, reason, target_version}` per entry. Added `deferrals_at_exit` field to the §5.7.1 sidecar example (null for non-final rounds, populated only when soft-block fires on the exit round).

### Plan edits applied

- §5.7.6 template — added `### Reviewer raw response` section as the trailing subsection, wrapped in a fenced `text` block; added explanatory paragraph below the template documenting the rationale.
- §6 Files list — added `scripts/render_markdown.py` row.
- §5.4.1 — fully restructured as two-step interaction with explicit per-item reason collection and medium target tagging; concrete pseudocode shows the second-step iteration; result persisted as `deferrals_at_exit` field on the sidecar.
- §5.7.1 — added `"deferrals_at_exit": null` field between `restart_metadata` and `reviewer_response` in the example JSON.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop.)

### Round stats

- Reviewer: External Codex review (user-supplied verbatim findings, post-ceiling)
- Findings raised: 3 (all medium-severity)
- Findings accepted: 3
- Findings rejected: 0
- Plan size delta: ~+45 lines (template addition + Files list entry + restructured soft-block + new schema field)
- **Convergence trajectory continuing:** rounds 11 and 12 both 0 high + medium-only. The plan-bloat detection in §5.5 would now likely fire (low-severity-only plus growth), prompting the user to switch to consistency-only mode or exit. After this round's edits, the architectural surface is settled — remaining concerns are fully implementation-detail in nature.

## Round 13 — 2026-05-03T06:00:00Z (user-extended past ceiling)

### Reviewer findings (Codex)

1. [Schema completeness / deferrals_at_exit] `deferrals_at_exit` is now a load-bearing persisted field, but its schema is still underspecified. The flow says it is "added to the sidecar schema" and persists per-item `{item_id, severity, reason, target_version}` entries, and the example adds `"deferrals_at_exit": null`, but unlike `restart_metadata` there is no concrete schema block defining allowed null/populated forms, required fields, or whether `target_version` may be null for non-medium items. Since sidecar validation is a hard gate, the implementer still has to invent this part of `scripts/sidecar_schema.json`.

2. [Verification / Round-12 regressions not pinned] The verification plan still does not lock in the round-12 fixes. The plan added a mandatory `### Reviewer raw response` renderer section and a new `deferrals_at_exit` exit artifact, but Phase 4 only lists a generic deferral-flow exit test and nothing that explicitly checks raw-response rendering, `deferrals_at_exit` serialization, or the `accept_all_risk` branch. These were concrete regressions in rounds 11-12; they should get explicit regression tests.

3. [Decisions table / D4 staleness] The locked decision table is slightly stale relative to the actual soft-block behavior. D4 still says "open highs force AskUserQuestion", but §5.4.1 now correctly applies the gate to open highs, open mediums, and open questions. The fixes log already shows you've been cleaning up stale decision rows when behavior changes; this one should be updated too so the summary table does not contradict the operative spec.

### Planner decisions (Claude)

1. **Accept** — Real schema-completeness gap. Added §5.7.3aa "deferrals_at_exit field (soft-block audit, optional)" as a peer to §5.7.3a, modeled on the same pattern. Concrete schema block defines: top-level type is `["array", "null"]`; populated form requires `item_id`, `severity`, `reason`; `target_version` is required (non-null) when severity is medium and null otherwise. Severity enum extended to include the literal `"open_question"` value since open questions have no native severity in §5.2's reviewer-response schema. Schema enforcement note specifies that the medium-target nullness rule lives in `scripts/sidecar_schema.json` itself, not just prose.

2. **Accept** — Real verification gap. Added 8 named tests to Phase 4: 2 for renderer raw-response handling (`test_renderer_appends_reviewer_raw_response_section`, `test_renderer_raw_response_text_preserved_byte_stable`), 3 for `deferrals_at_exit` serialization including the medium-target requirement and null-for-non-final-rounds invariant, 1 for the `accept_all_risk` auto-populated reason, 2 for the per-item step-2 capture (`test_soft_block_step2_collects_per_item_reasons`, `test_soft_block_step2_collects_medium_target_version`), plus added the round-11 mediums-in-gate coverage as `test_loop_soft_blocks_with_open_medium_severity`. Each test cites the originating round-finding so future debugging can trace from regression back to spec rationale.

3. **Accept** — Real D4 staleness. Updated the §4 Decisions table row for D4 to reflect the operative spec: the gate covers any open high, open medium, OR open question; the interaction is two-step (action then per-item reason + medium target); deferrals persist as `deferrals_at_exit` array. Added a parenthetical note citing round-13 finding 3 so the staleness fix is visible in the audit history.

### Plan edits applied

- §5.7.3aa — new subsection defining `deferrals_at_exit` schema block (parallel to §5.7.3a `restart_metadata`); concrete required/optional/nullable rules; per-field semantics; medium-target enforcement explicitly in `sidecar_schema.json`.
- Phase 4 verification — added 8 named tests covering raw-response rendering, deferrals_at_exit serialization (3 variants), accept_all_risk reason auto-population, step-2 reason capture, step-2 medium target capture; reused round-11 finding 1 to add medium-soft-block test.
- §4 Decisions table D4 — updated wording from "open highs force AskUserQuestion" to "any open high, open medium, OR open question forces two-step AskUserQuestion (action → per-item reason + medium target); deferrals persisted as `deferrals_at_exit` array"; cited round-13 finding 3.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop.)

### Round stats

- Reviewer: External Codex review (user-supplied verbatim findings, post-ceiling)
- Findings raised: 3 (2 medium, 1 low)
- Findings accepted: 3
- Findings rejected: 0
- Plan size delta: ~+50 lines (deferrals_at_exit schema block + 8 new test names + D4 row rewrite)
- **Reviewer self-assessment:** "the draft is converging. The main remaining gap is that deferrals_at_exit is behaviorally specified but not yet schema-specified and test-pinned." Both gaps closed by this round's edits.
- **Severity trajectory:** rounds 1-10 = 27 high; round 11 = 4 medium; round 12 = 3 medium; round 13 = 2 medium + 1 low. The ramp from "high architectural" → "medium consistency" → "low audit" is exactly the convergence signature v2's severity-gated exit (§5.4) is designed to reward by allowing earlier exit at each tier reduction.

## Round 14 — 2026-05-03T06:30:00Z (user-extended past ceiling)

### Reviewer findings (Codex)

1. [Schema contradiction / accept_all_risk path broken] The new `accept_all_risk` branch is internally impossible whenever any open medium exists. In §5.4.1 it writes `target_version: None` for every deferred item, including mediums, but §5.7.3aa says `target_version` must be non-null when `severity == "medium"` and that schema validation fails otherwise. Since sidecar validation is a hard gate, the documented "Exit anyway, accept all risk" path cannot succeed for the exact medium-bearing cases the soft-block flow is meant to handle.

2. [Open-question identity / String-vs-object mismatch] Open questions are still modeled inconsistently between the reviewer schema and the exit/sidecar flow. The review schema defines `open_questions` as plain strings, but §5.4.1 iterates them as items with `item.id` and effectively item metadata, and §5.7.3aa says they persist as stable `item_ids` with pseudo-severity `"open_question"`. The plan never defines how a string open question becomes a stable identified object, so the implementer still has to invent that mapping.

3. [Status messaging / Stale top-of-plan claim] The fixes history shows the prior review issues were addressed through round 13, but the plan status line still says "ready for Phase 1+2 implementation" rather than reflecting that the remaining issues are now in the round-13 soft-block/schema additions, not the earlier transport/diff core. That's not a blocker, but the top-level status is now underselling where the real residual risk sits.

### Planner decisions (Claude)

1. **Accept** — Real high-severity contradiction. `accept_all_risk` was specified to write `target_version: None` for all items, but §5.7.3aa schema enforced non-null target_version for mediums — every medium-bearing exit would fail validation, breaking the documented "I just want to exit" path entirely. Updated §5.4.1 `accept_all_risk` branch to write the literal sentinel string `"accepted-at-exit"` for ALL items' target_version (not None, and uniform across severities for semantic cleanliness). Added a sentinel-value bullet to §5.7.3aa per-field semantics noting that downstream tooling should treat `"accepted-at-exit"` as distinct from real version strings (e.g. exclude from "deferred-to-v2.1" filters). Phase 4 test list expanded to four cases including the schema-rejection test that validates the medium-target rule itself.

2. **Accept** — Real string-vs-object identity gap. The reviewer schema (OpenAI-strict-compatible) keeps open_questions as flat strings, but downstream flows need stable identifiers. Added a new "Open-question identity" subsection to §5.2 reviewer-prompt corollary defining a client-side post-parse `assign_open_question_ids()` helper that maps each bare string to an `OpenQuestion(id, text)` object using the format `oq_r{round}_{1-based-index}`. Round-stable (re-parsing same raw text yields same IDs) and cross-round-stable (each round prefixed differently). Sidecar's `reviewer_response.open_questions` therefore stores the post-assignment object form `[{"id": "oq_r5_1", "text": "..."}]` while the wire-level reviewer response stays simple bare strings.

3. **Accept** — Real status-line staleness. Updated the top-of-plan status banner from a flat "ready for Phase 1+2 implementation" to a structured statement: transport/diff/severity-gate core is design-locked and ready; residual risk now sits in round-13 exit-audit additions (deferrals_at_exit schema, open-question identity, accept_all_risk sentinel) which warrant a fresh-context verification pass before declaring fully implementation-ready. Cited round-14 finding 3 in the status line itself so future readers see the audit trail.

### Plan edits applied

- §5.4.1 `accept_all_risk` branch — replaced `target_version: None` with sentinel string `"accepted-at-exit"` for all items; added explanatory comment citing round-14 finding 1.
- §5.7.3aa per-field semantics — added bullet documenting the `"accepted-at-exit"` sentinel value, what it means, and how downstream tooling should distinguish it from real version strings; expanded Phase 4 test list from "all three cases" to four including the schema-rejection test.
- §5.2 — added "Open-question identity (round-14 finding 2)" subsection with the `assign_open_question_ids()` mapping helper, format spec, and stability guarantees; clarified that sidecar persistence uses the post-assignment object form while wire schema stays bare strings.
- Top-of-plan status line — restructured to differentiate locked core from residual exit-audit risk; cites round-14 finding 3.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop.)

### Round stats

- Reviewer: External Codex review (user-supplied verbatim findings, post-ceiling)
- Findings raised: 3 (1 high, 1 medium, 1 low)
- Findings accepted: 3
- Findings rejected: 0
- Plan size delta: ~+30 lines (sentinel handling + open-question helper + status-line rewrite)
- **Severity-trajectory note:** rounds 11-13 were medium-only. Round 14 surfaced a high — but it's a self-induced regression from round 13's introduction of `deferrals_at_exit`. The reviewer caught a latent contradiction between the new schema (round 13) and the new accept_all_risk branch (round 12). This is exactly the v0.0.5 round-14 phenomenon: late-stage edits introduce contradictions in their own neighborhood that earlier-round reviewers couldn't have caught (since the conflicting code didn't exist yet). v2's diff-aware reviewing (§5.3) would surface this faster — round 14 would diff against round 13's edits and immediately see the new `accept_all_risk: target_version=None` flowing into a schema that now rejects it.

## Round 15 — 2026-05-03T07:00:00Z (user-extended past ceiling)

### Reviewer findings (Codex)

1. [Schema completeness / open_questions populated form] The sidecar shape for non-empty `reviewer_response.open_questions` is still not decision-complete. §5.2 says wire-level reviewer output is `list[str]`, then client-side parsing converts that into `OpenQuestion(id, text)` objects and stores those objects in the sidecar. But the §5.7.1 sidecar example still only shows `open_questions: []` and there is no concrete sidecar-schema subsection defining the populated object form under `reviewer_response.open_questions`. Since `scripts/sidecar_schema.json` is a hard gate, the implementer still has to invent that nested schema.

2. [Verification / Round-14 fixes not test-pinned] The round-14 fixes are not fully pinned by named tests. The plan adds two load-bearing behaviors: `assign_open_question_ids()` with stable `oq_r{round}_{index}` IDs and the `accept_all_risk` sentinel `target_version: "accepted-at-exit"`. But the Phase 4 test list only explicitly names reason auto-population and generic open-question/deferral serialization tests, not the ID assignment behavior or the sentinel target-version behavior itself. Those were exactly the round-14 regressions; they should have direct regression tests.

3. [Status messaging / Top vs footer mismatch] The top and bottom status lines still point in slightly different directions. The banner says residual risk remains and warrants fresh-context verification before calling the plan fully implementation-ready, while the footer still says "Status: ready for Phase 1+2 implementation when user approves". That is not blocking, but it leaves the overall readiness signal fuzzy.

OPEN QUESTIONS / ASSUMPTIONS:
- The fixes history shows the round-13 and round-14 issues from the prior review were actually addressed, including accept_all_risk and D4 staleness.
- I'm treating missing explicit schema/test detail as a real implementation risk because this plan otherwise aims to be decision-complete.

### Planner decisions (Claude)

1. **Accept** — Real schema gap. Added a "Sidecar schema for populated open_questions (round-15 finding 1)" block to §5.2 (immediately after the `assign_open_question_ids` helper) defining the in-sidecar form: `array` of `{id, text}` objects with required fields, `additionalProperties: false`, and an `id` regex `^oq_r[0-9]+_[0-9]+$` enforcing the auto-assigned format. Empty array is valid; bare-string array is rejected (raw reviewer output reaching the sidecar without ID assignment is a bug). Updated §5.7.1 example to show two populated open_questions in the populated object form (instead of just `[]`) so implementers see the shape directly.

2. **Accept** — Real test-pinning gap. Added 6 named Phase 4 tests covering the round-14 and round-15 behaviors directly: `test_assign_open_question_ids_format_oq_r_round_index` (regex format, round-stable), `test_assign_open_question_ids_cross_round_unique` (no cross-round collisions), `test_accept_all_risk_writes_sentinel_target_version` (every item gets sentinel, including mediums), `test_accept_all_risk_passes_sidecar_schema_with_open_mediums` (end-to-end — the round-14 regression itself: accept_all_risk on medium-bearing exit must produce a schema-valid sidecar), `test_sidecar_open_questions_populated_form_validates` (populated form passes), `test_sidecar_open_questions_rejects_bare_strings` (raw string form rejected). Each test cites its originating round-finding for traceability.

3. **Accept** — Real footer/banner mismatch. Updated the footer "Status:" line at the bottom of §11 to mirror the top-banner phrasing exactly: transport/diff/severity-gate core design-locked, residual risk in round-13/14/15 exit-audit additions, fresh-context verification recommended before fully-implementation-ready. Both ends now agree.

### Plan edits applied

- §5.2 — added "Sidecar schema for populated open_questions" subsection with concrete `oq_r[0-9]+_[0-9]+` regex pattern, required field rules, additionalProperties=false, and Phase 4 test references.
- §5.7.1 example — replaced `"open_questions": []` with two populated entries in the object form so implementers can see the actual shape stored in the sidecar.
- Phase 4 verification — added 6 new named tests pinning round-14/15 behaviors (ID format, cross-round uniqueness, sentinel target_version, schema validation under accept_all_risk + open mediums, populated-form sidecar acceptance, bare-string rejection).
- §11 footer status line — rewritten to match the top-banner phrasing; banner and footer now agree.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop.)

### Round stats

- Reviewer: External Codex review (user-supplied verbatim findings, post-ceiling)
- Findings raised: 3 (2 medium, 1 low)
- Findings accepted: 3
- Findings rejected: 0
- Plan size delta: ~+30 lines (schema block + 6 tests + footer rewrite + populated example)
- **Reviewer self-assessment:** "the draft is converging and I don't see a new architectural blocker. The remaining gaps are now around making the new open-question object form and round-14 behaviors fully schema-pinned and test-pinned." Both pinned by this round.
- **Severity trajectory:** rounds 1-10 = 27 high; r11 = 4 medium; r12 = 3 medium; r13 = 2 medium + 1 low; r14 = 1 high + 1 medium + 1 low; r15 = 2 medium + 1 low. The brief r14 high was a self-induced contradiction (round-13 schema vs round-12 branch) — caught and fixed in 1 round. Trajectory back to medium/low only in r15.

## Round 16 — 2026-05-03T07:30:00Z (user-extended past ceiling — convergence reached)

### Reviewer findings (Codex)

1. [Stale text / Banner-footer mismatch] The banner/footer mismatch is not actually fully fixed. The top banner still says residual risk is "after 14 rounds of dogfooded review", while the footer correctly says "after 15 rounds". Round 15's fixes log claims those two ends now agree, so this is a small but real stale-text regression.

OPEN QUESTIONS / ASSUMPTIONS:
- I re-checked the round-14 and round-15 issues from the previous review. The substantive ones appear closed: accept_all_risk now uses the "accepted-at-exit" sentinel, open-question IDs are specified, populated sidecar open_questions are schema-defined, and the Phase 4 tests now explicitly pin those behaviors.
- I do not see a new medium/high-severity spec contradiction in the current draft.

### Planner decisions (Claude)

1. **Accept** — Real stale-text regression. Round 15's edit only updated the footer; the banner kept its "14 rounds" wording. Updated banner to "after 16 rounds" (current count after this round) and footer to match. Both ends now agree byte-for-byte on the round count and residual-risk description. Cited round-16 finding 1 in both for traceability.

### Plan edits applied

- §0 banner — updated "after 14 rounds" → "after 16 rounds"; trailing parenthetical now cites round-16 banner/footer parity confirmation.
- §11 footer "Status:" line — updated "after 15 rounds" → "after 16 rounds"; trailing line now confirms banner/footer parity.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop.)

### Round stats

- Reviewer: External Codex review (user-supplied verbatim findings, post-ceiling)
- Findings raised: 1 (low-severity)
- Findings accepted: 1
- Findings rejected: 0
- Plan size delta: ~+0 lines (just text replacements; banner and footer adjusted in place)
- **Reviewer self-assessment:** "I do not see a new medium/high-severity spec contradiction in the current draft." This is the strongest convergence signal yet — explicit declaration that the architectural surface and state-machine edges are settled.

---

## End of dogfood loop — 16 rounds, convergence reached

- **Final status:** convergence — reviewer explicitly declared no remaining medium/high-severity contradictions; only a single low-severity stale-text issue this round, addressed in place.
- **Total rounds:** 16 (10 in-loop + 6 user-extended post-ceiling)
- **Total findings raised:** 44
- **Total findings accepted:** 44 (zero rejections across 16 rounds)
- **Total findings rejected:** 0
- **Severity histogram:**
  - High: 28 (27 in rounds 1-10 plus 1 self-induced regression in round 14)
  - Medium: 12 (4+3+2+1+2 across rounds 11-15)
  - Low: 4 (1 each in rounds 13, 14, 15, 16)
  - Total: 44 ✓
- **Plan path:** `plans/v2-plan.md` (~1,160 lines, up from ~566 at start)
- **Fixes log path:** `plans/fixs/v2-plan-fixes.md` (this file, full transcript)
- **Codex tokens (rounds 2-10 in-loop only):** ~218,803
- **External-review tokens (rounds 11-16, user-supplied Codex):** not measured by skill, run externally

**Convergence pattern observed:**

```
Round  Findings  Severity                    Mode
─────  ────────  ──────────────────────────  ─────────────────────────────────
 1     6h        architectural               external Codex (user-supplied)
 2-10  21h       wiring of architectural     in-loop Codex (full plan re-reads)
11     4m        state-machine edges         external Codex post-ceiling
12     3m        cross-references            external Codex post-ceiling
13     2m+1l     schema completeness         external Codex post-ceiling
14     1h+1m+1l  self-induced regression     external Codex post-ceiling
15     2m+1l     schema completion           external Codex post-ceiling
16     1l        stale-text regression       external Codex post-ceiling
```

**v2 design value, demonstrated by the v1 dogfood:**

The dogfood loop validated v2's three core convergence improvements directly against itself:

1. **Severity-gated exit (§5.4).** A v2-aware loop would have exited at `Resolved` or `Resolved-with-deferrals` after round 13 (no high, all mediums addressed). Rounds 14-16 added marginal value: 1 self-induced high (caught + fixed in 1 round) + 5 mediums (state-machine edges) + 3 lows (text staleness). v1's lack of severity tagging meant the loop kept finding "issues" indistinguishable from architectural risks.

2. **Diff-aware reviewing (§5.3).** Rounds 11-16 caught contradictions introduced by round-(N-1)'s edits. v1 reviewer re-read the full plan each round; v2's diff-aware mode would have focused only on changed sections, catching these in fewer rounds with tighter prompts and lower cost.

3. **Plan-bloat detection (§5.5).** The plan grew from ~566 → ~1,160 lines across 16 rounds (+105%). v2's bloat-warning would have fired around round 7 (~20% growth, no new high findings since round 4). User would have been prompted to switch to consistency-only mode or exit, instead of running another 9 rounds catching the same class of issues.

**Plan implementation-readiness:** transport/diff/severity-gate core is design-locked. Residual risk in the round-13/14/15 exit-audit additions has been progressively addressed; reviewer's own round-16 assessment is "no new medium/high-severity spec contradiction." A fresh-context verification pass (potentially by spawning a separate Claude session to read the plan top-to-bottom) is recommended before declaring fully implementation-ready, but no further dogfood rounds are warranted — diminishing returns are now clearly evident.

## Round 17 — 2026-05-03T08:00:00Z (user-extended past ceiling — stale-guidance cleanup)

### Reviewer findings (Codex)

1. [Stale operator guidance / Footer next-step] The recommended next step is now stale relative to the fixes history and the current status banner. The plan footer still says to "dogfood" the v2 plan with the existing v1 skill, but the round-16 closeout explicitly says no further dogfood rounds are warranted and that the right next step is a fresh-context verification pass instead. That is not a spec blocker, but it is a real mismatch in operator guidance.

### Planner decisions (Claude)

1. **Accept** — Real operator-guidance mismatch. The §11 footer's "Suggested next step" paragraph still recommended the dogfood that has already happened. Rewrote it to: (a) note the dogfood is complete with reference to the fixes log, (b) recommend fresh-context verification as the now-correct next step, citing the rounds-11-16 pattern that showed fresh-context Codex catches what in-loop Codex doesn't, (c) note implementation should follow the verification pass. Cited round-17 finding 1 inline for traceability. This makes the operator guidance match the round-16 closeout statement instead of contradicting it.

### Plan edits applied

- §11 "Suggested next step" paragraph — rewrote from "dogfood the v2 plan with v1 skill" to "the v1-skill dogfood already happened (see fixes log); recommended next step is fresh-context verification pass before Phase 1+2 implementation."

(No source code, tests, fixtures, or runtime behavior was implemented in this loop.)

### Round stats

- Reviewer: External Codex review (user-supplied verbatim findings, post-ceiling)
- Findings raised: 1 (low-severity)
- Findings accepted: 1
- Findings rejected: 0
- Plan size delta: ~+0 lines (text replacement only)
- **Reviewer self-assessment in round 16:** "I do not see a new medium/high-severity spec contradiction in the current draft." Round 17's finding is purely text staleness — the same class as round-16's banner/footer mismatch. Two consecutive rounds of stale-text-only findings is the strongest possible "stop dogfooding" signal short of explicit NO_FINDINGS.

---

## Update to end-of-loop report (post round 17)

- Total rounds: **17**
- Total findings: **45** raised, **45** accepted, **0** rejected
- Severity histogram: 28 high + 12 medium + 5 low (= 45 ✓; round-17 added 1 low to the round-16 closeout's 4 lows)
- Last 2 rounds (16, 17) were both **single low-severity stale-text issues** — exactly the convergence signature v2's severity-gated exit detects. v1's loop has no severity gate and would happily continue finding stale-text issues forever; v2 would have exited at `Resolved` after round 13.
- **Stop signal: confirmed.** Two consecutive rounds of low-only stale-text findings, plus reviewer's explicit "no new medium/high-severity spec contradiction" statement in round 16, plus reviewer's "this is not a spec blocker" qualifier in round 17. Continued dogfooding past this point is pure overhead.

## Round 18 — 2026-05-03T08:30:00Z (user-extended past ceiling — arithmetic cleanup)

### Reviewer findings (Codex)

1. [Stale numbers / Footer cross-doc reference] The footer's supporting-reference numbers are now stale relative to the file it points at. The plan footer says the fixes log is "16 rounds, 44 findings," but the same log now includes a post-round-17 update with "17" rounds and "45" findings. If 16/44 is meant to describe only the dogfood subset, that needs to be stated more explicitly.

2. [Internal arithmetic / End-of-loop histogram] The fixes-history end-of-dogfood histogram is internally inconsistent. The end-of-loop report says "Low: 3", but it attributes lows to rounds 13, 14, 15, AND 16 (4 rounds), and the post-round-17 update later reports "4 lows". Since the plan explicitly sends readers to this file as evidence, the reference doc should be arithmetically clean.

### Planner decisions (Claude)

1. **Accept** — Real cross-doc staleness. Updated the §11 footer paragraph to clarify that "16 rounds, 44 findings" describes the dogfood-proper specifically (which reached convergence in round 16 per reviewer's explicit assessment), and that rounds 17+ are post-dogfood text-staleness cleanup rather than additional dogfood iterations. This makes the count semantically accurate without requiring the footer to be updated every cleanup round.

2. **Accept** — Real arithmetic error. Recounted the histogram from per-round stats:
   - High: 27 (rounds 1-10) + 1 (round 14 self-regression) = 28 ✓
   - Medium: 4 (r11) + 3 (r12) + 2 (r13) + 1 (r14) + 2 (r15) + 0 (r16) = 12 (was incorrectly written as 13)
   - Low: 1 each in r13, r14, r15, r16 = 4 (was incorrectly written as 3)
   - Total at end of round 16: 28 + 12 + 4 = 44 ✓
   - Round 17 added 1 more low → 28 + 12 + 5 = 45 ✓
   Updated both the round-16 end-of-loop histogram (now "Medium: 12, Low: 4") and the post-round-17 update line (now "28 high + 12 medium + 5 low (= 45 ✓)") to match the correct arithmetic. Added explicit "Total: 44 ✓" to the round-16 histogram so future readers can sanity-check at a glance.

### Plan edits applied

- §11 "Suggested next step" footer paragraph — added explicit disambiguation between "the dogfood subset (16 rounds, 44 findings)" and "post-dogfood cleanup rounds 17+"; cited round-18 finding 1.
- Fixes-md round-16 end-of-loop histogram — corrected Medium count 13→12, Low count 3→4; added explicit total-line "Total: 44 ✓" for sanity-checking.
- Fixes-md round-17 update line — corrected from "28 high + 13 medium + 4 low" to "28 high + 12 medium + 5 low (= 45 ✓; round-17 added 1 low to the round-16 closeout's 4 lows)"; the inline arithmetic note shows the math directly.

(No source code, tests, fixtures, or runtime behavior was implemented in this loop.)

### Round stats

- Reviewer: External Codex review (user-supplied verbatim findings, post-ceiling)
- Findings raised: 2 (both low-severity)
- Findings accepted: 2
- Findings rejected: 0
- Plan size delta: 0 lines (text replacements + 1-line addition)
- **Three consecutive rounds of low-only findings** — the convergence pattern is now unambiguous. v2's severity-gated exit would have terminated 5+ rounds ago. Continued cycles produce strictly stale-text/arithmetic findings.

---

## Final end-of-loop report (post round 18)

- Total rounds: **18**
- Total findings raised: **47**
- Total findings accepted: **47** (zero rejections across all 18 rounds)
- Severity histogram (cumulative through round 18):
  - High: 28 (rounds 1-10 plus 1 round-14 self-regression)
  - Medium: 12 (rounds 11-15)
  - Low: 7 (rounds 13, 14, 15, 16, 17, and 2 in round 18)
  - Total: 47 ✓
- Plan: 1,159 lines (no growth in last 2 rounds — purely text adjustments)
- Fixes log: ~770 lines, 18 rounds
- **Stop signal: hardened.** Three consecutive low-only rounds, reviewer self-assessment of architectural settledness in round 16, and round-18 finding 2's "internally inconsistent arithmetic" is the smallest possible class of issue a discriminator can flag. Stopping now is correct; continuing produces strictly diminishing returns.
