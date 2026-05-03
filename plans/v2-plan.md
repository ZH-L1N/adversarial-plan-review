# Adversarial Plan Review — v2 Plan

> Status: transport/diff/severity-gate core is design-locked and ready for Phase 1+2 implementation. Residual risk after 16 rounds of dogfooded review sits in the round-13/14/15 exit-audit additions (`deferrals_at_exit` schema, open-question identity, `accept_all_risk` sentinel, sidecar-form schema for populated open_questions) — all addressed but warrant a fresh-context verification pass before declaring fully implementation-ready. (Banner/footer parity confirmed in round 16 finding 1.)
> Author: Claude (planner), with decisions from user (zlin@exowatt.com).
> Source context: real failure mode observed on `Optical-LCOE/plans/fixs/v0.0.5-torque-sizing-bom-decomp-fixes.md` (16 rounds, 47 findings, plan-bloat-driven reviewer hallucination in round 14).

---

## 1. Goal

Replace v1's round-count-and-rejection-driven exit with a **convergence signal** the loop can actually measure (severity-gated), and prevent the **plan-bloat reviewer drift** observed in long runs (>10 rounds) by giving the reviewer a diff + prior-decision context instead of a fresh full-plan read each round.

Add an **OpenAI Responses API transport** as the default reviewer path (enables structured outputs and severity tags), with the existing Codex CLI as a fallback for users without an OpenAI API key. **First-run prompt** asks the user which path to configure if neither is available.

## 2. Non-goals

- Replacing Codex CLI entirely. It stays as a supported fallback.
- Adding code-review or PR-review capability. This skill remains plan-only.
- Auto-fixing the plan. Planner (Claude) still drives every edit.
- Switching from markdown plans to a structured plan format.
- Committing on behalf of the user. Git-write prohibition from v1 carries over unchanged.
- Multi-reviewer parallel mode. Two OpenAI models (Codex + OpenAI direct) have correlated blind spots; deferred to v3 with the requirement that any second reviewer come from a different model family (Gemini, DeepSeek, etc.) for genuine independence.
- CI / GitHub Actions integration. Deferred to v3.

## 3. Observed v1 weaknesses being addressed

From the v0.0.5 transcript (real run, not synthetic):

| Weakness | Symptom in transcript | v2 mitigation |
|---|---|---|
| Reviewer sees full plan every round, not the diff | Round 14 finding 1 hallucinated stale architecture that no longer existed in plan; rejected as misread | **Diff-aware reviewing** (§5.3) |
| No severity tags from reviewer | User had to inject external "High:/Medium:" tags in rounds 13-16 manually | **Structured outputs with severity** (§5.2) |
| No convergence metric | Loop terminated on round count (10) or planner-rejects-all, not on risk resolution | **Severity-gated exit** (§5.4) |
| Plan-bloat causes review drift | By round 14 reviewer was scrubbing for stale cross-references, not finding new architectural risks | **Plan-bloat detection** (§5.5) |
| No transport choice | Codex CLI ChatGPT-OAuth-only, no JSON schema enforcement | **Dual-transport, OpenAI default** (§5.1) |

## 4. Decisions captured

Locked decisions from design Q&A (this document supersedes the prior draft):

| # | Decision | Choice |
|---|---|---|
| D1 | Implementation phasing | Ship Phase 1+2 together (transport + structured outputs); then Phase 3+4 (diff-aware + severity-gated exit + bloat detection) |
| D2 | OpenAI API surface | Responses API (`client.responses.create` with `text.format.json_schema`) |
| D3 | Default reviewer model | `gpt-5.5` (newer than v1's `gpt-5.4`; same Codex CLI compatibility; $5 in / $30 out per 1M tokens) |
| D4 | Severity-gated exit strictness | Soft-block: any open high, open medium, OR open question forces a two-step `AskUserQuestion` (action selection → per-item reason + medium target). Deferrals are persisted as `deferrals_at_exit` array on the final-round sidecar (§5.4.1, §5.7.3aa). Lows do not trigger the gate. (Round-13 finding 3: previous wording said "open highs" only — stale relative to round-3's medium-blocking expansion and round-11's gate update.) |
| D5 | Bloat warning response | Interrupt with `AskUserQuestion` (continue / exit / consistency-only mode) |
| D6 | Transport config check timing | At skill start, before any work (fail fast) |
| D7 | API key storage | Local `.env` file, gitignored |
| D8 | Severity levels | 3: `high` / `medium` / `low` |
| D9 | Consistency-only mode | Keep as user-selectable option when bloat warning fires |
| D10 | Cost cap | `ADVERSARIAL_MAX_COST_USD` env var, default `$5` per run; tracks cumulative across rounds; soft-pause for user decision when exceeded |
| D11 | Diff context to reviewer | Unified diff (`git diff` format) + full plan text for cross-reference |
| D12 | Prior rejections to reviewer | Yes — pass full prior decisions including rejection reasons |
| D13 | Snapshot location for uncommitted plans | `.scratch/<slug>-<version>-plan-snapshot-r{N}.md` — gitignored, auto-cleaned on loop exit |
| D14 | Planner edits-applied bullets to reviewer | Yes — alongside diff; reviewer flags disagreement between stated intent and actual edit as a finding |
| D15 | Open-questions exit gate | Yes — unresolved open-questions block exit; user must answer or explicitly defer |
| D16 | v1 prompt builder | Keep, renamed `build_reviewer_prompt_v1.py` for Codex-only fallback debugging |
| D17 | NO_FINDINGS edge case | Approved exit; reviewer's clean review is authoritative regardless of any earlier deferrals |
| D18 | OpenAI client install | Document in README: `pip install openai>=1.0`; skill checks `import openai` at startup, prints install command if missing |
| D19 | Round-history truncation | Keep last 3 rounds verbatim; older rounds summarized to 1-line per round (round number, finding count, severity histogram, exit-relevant decisions) |
| D20 | Malformed-JSON handling | Retry once with same prompt; if still bad, fail the round and surface raw response to user |
| D21 | Round stats in fixes-md | Yes — per-round subsection with cost, tokens, severity histogram, duration |
| D22 | Resume capability | Yes — at skill start (step 4 of §5.0a), detect existing round-N JSON sidecars (the authoritative audit artifacts), validate via schema + hash, then `AskUserQuestion` to resume from round N+1 or start over destructively. Markdown fixes-md is regenerated from sidecars on resume, never read for state. (Round-11 meta-question: previous wording said "detect prior fixes-md" which was stale relative to the JSON-authoritative model.) |
| D23 | Codex severity inference (fallback path) | Keyword-based heuristic: `silent`, `data loss`, `breaks compat`, `silently` → high; `gap`, `ambiguous`, `unclear`, `missing` → medium; default low |
| D24 | CI integration | Out of scope for v2; deferred to v3 |
| D25 | README scope | Full guide: install both transports, env vars, workflow walkthrough, cost estimation, troubleshooting |
| D26 | Plan-bloat thresholds | Default 20% growth over 3 rounds; configurable via `ADVERSARIAL_BLOAT_THRESHOLD` and `ADVERSARIAL_BLOAT_WINDOW` |

## 5. Design

### 5.0 Allowed write boundary (v2) — replaces v1 contract

v1 SKILL.md restricted writes to `plans/<slug>-<version>.md` and `plans/fixs/<slug>-<version>-fixes.md` only. v2 expands the permitted-write set explicitly to enable the new transport, snapshot, and JSON-sidecar machinery. Anything outside this list remains read-only for the duration of the loop. The git-write prohibition (no `commit`, `add`, `push`, `merge`, `rebase`, `branch`, `reset`, `stash`, `checkout`, `restore`) carries over from v1 unchanged — only read-only git commands (`status`, `log`, `diff`, `ls-files`) are allowed.

| Path | When written | Tracked in git? |
|---|---|---|
| `plans/<slug>-<version>.md` | Per-round plan edits (planner) | ✅ Yes |
| `plans/fixs/<slug>-<version>-round-{N}.json` | End of each round (atomic write); **source of truth** per §5.7 | ✅ Yes |
| `plans/fixs/<slug>-<version>-fixes.md` | Rendered from JSON sidecar after each round; not authoritative | ✅ Yes |
| `.scratch/<slug>-<version>-plan-snapshot-r{N}.md` | Per-round plan snapshot for diff (§5.3.1) | ❌ Gitignored, auto-cleaned on loop exit |
| `.env` | First-run only, when user provides API key (§5.6) | ❌ Gitignored |

The implementer must enforce this set at runtime (e.g. wrap file writes in a guard that rejects paths outside the allowed list). Code, tests, configs, and any other repo content remain read-only.

**Destructive operations within the slug/version artifact set.** The "Start over" branch of the resume flow (§5.9) needs to delete prior round sidecars and the rendered fixes-md before round 1 begins anew. This is explicitly permitted, scoped to the current `<slug>-<version>` triple, and gated behind an explicit user confirmation listing every file to be deleted:

| Operation | Permitted target | Required gate |
|---|---|---|
| Delete sidecars | `plans/fixs/<slug>-<version>-round-{N}.json` (any N) | `AskUserQuestion` showing exact file list |
| Delete fixes-md | `plans/fixs/<slug>-<version>-fixes.md` | Same prompt as above |
| Delete snapshots | `.scratch/<slug>-<version>-plan-snapshot-r{N}.md` (any N) | Implicit — gitignored, no audit value |
| Delete plan markdown | `plans/<slug>-<version>.md` | **NEVER permitted.** The plan itself is user-authored and out of scope for any skill-driven destructive op. |

Any deletion must log the intended file list to stderr before prompting and to the new round-1 sidecar's `restart_metadata` field after confirmation, so the audit trail records what was wiped. If the user wants to preserve prior history without forking the version, the alternative is to bump version (e.g. `<version>-rerun-1`), which avoids deletion entirely; the resume UX should suggest this as the default.

This closes round-4 finding 3: §5.9's "Start over" option is now explicitly authorized within §5.0's write boundary, with confirmation gating and audit logging.

### 5.0a Startup ordering — locked sequence

The skill executes its startup steps in this exact order. Mixing steps risks ambiguous state (e.g. resume-detect before transport-check would prompt the user about a fixes-md they cannot continue without an API key).

1. **Pre-flight git check** — read-only `git status --porcelain`. Refuse to run if uncommitted changes outside `plans/`.
2. **Transport check + first-run UX** (§5.6) — confirm `OPENAI_API_KEY` or Codex CLI is available. Runs before slug/version because the user may need to configure transport before naming a plan.
3. **Slug/version prompt** — interactive `AskUserQuestion`. Resolves plan path `plans/<slug>-<version>.md` (must already exist) and fixes-md path `plans/fixs/<slug>-<version>-fixes.md`.
4. **Resume detection** (§5.9) — needs slug/version to locate prior round-N JSON sidecars. Prompts user to resume from round N+1 or restart.
5. **Begin round 1** (or round N+1 on resume).

### 5.1 Transport abstraction

A new `scripts/reviewer.py` exposes a single function:

```python
def invoke_reviewer(prompt: str, *, model: str | None = None) -> ReviewResult:
    """Returns parsed ReviewResult. Selects transport based on env."""
```

Transport selection priority (D6):

1. `ADVERSARIAL_TRANSPORT=openai` → OpenAI Responses API direct
2. `ADVERSARIAL_TRANSPORT=codex` → Codex CLI via `codex-companion.mjs`
3. **Auto-detect** at skill start:
   - If `OPENAI_API_KEY` set → OpenAI (D7)
   - Else if Codex CLI available → Codex
   - Else → fail fast with first-run prompt (§5.6)

#### 5.1.1 OpenAI Responses API path (default — D2, D3)

```python
import openai

client = openai.OpenAI()  # reads OPENAI_API_KEY from env
response = client.responses.create(
    model=os.environ.get("OPENAI_REVIEWER_MODEL", "gpt-5.5"),  # D3
    input=prompt,
    text={
        "format": {
            "type": "json_schema",
            "name": "review",
            "schema": REVIEW_SCHEMA,  # see §5.2
            "strict": True,
        }
    },
    max_output_tokens=int(os.environ.get("OPENAI_MAX_TOKENS", "8000")),
)
parsed = json.loads(response.output_text)
return ReviewResult.from_openai(parsed, usage=response.usage)
```

#### 5.1.2 Codex CLI path (legacy fallback)

The v2 prompt can exceed 50KB (full plan + diff + prior rounds). Windows command-line limits (~32KB on cmd.exe; 8KB historical floor) make argv-based prompt passing unsafe. **Pass the prompt via stdin instead.**

```python
result = subprocess.run(
    ["node", os.environ["CLAUDE_PLUGIN_ROOT"] + "/scripts/codex-companion.mjs",
     "task", "--model", "gpt-5.5"],
    input=prompt,             # stdin — bypasses argv length limits
    capture_output=True,
    check=True,
    text=True,
)
return ReviewResult.from_codex_prose(result.stdout)  # uses keyword heuristic per D23
```

`codex-companion.mjs` must be updated alongside this change to read its prompt from stdin when no positional `prompt` arg is provided. Add it to the Files-to-modify list (§6) and a regression test that runs the Codex fallback with a >40KB prompt on Windows.

The Codex path lacks JSON schema enforcement; severity is inferred via keyword heuristic (D23). Documented as a known degradation in README.

### 5.2 Structured outputs schema (D2, D8)

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "findings", "open_questions"],
  "properties": {
    "status": {"enum": ["NO_FINDINGS", "FINDINGS_PRESENT"]},
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["severity", "category", "where", "what_can_go_wrong", "concrete_fix"],
        "properties": {
          "severity": {"enum": ["high", "medium", "low"]},
          "category": {"type": "string"},
          "where": {"type": "string"},
          "what_can_go_wrong": {"type": "string"},
          "concrete_fix": {"type": "string"}
        }
      }
    },
    "open_questions": {
      "type": "array",
      "items": {"type": "string"}
    }
  }
}
```

**Field semantics (NOT in the runtime schema):**

| Field | Meaning |
|---|---|
| `where` | Plan section or line reference (e.g. `§5.3`, `plans/v2-plan.md:257`). |
| `what_can_go_wrong` | One-paragraph description of the failure mode. |
| `concrete_fix` | Specific suggested edit, file, or rewrite. |

These descriptions live HERE in markdown, not as `description` keys in the schema constant — round-6 finding 2 closed by stripping all annotation keywords (`description`, `comment`, `$comment`, `title`, `examples`) from `REVIEW_SCHEMA`. Static test `test_review_schema_constant_uses_only_strict_safe_keywords` walks the dict at every nesting level and asserts only the safe-subset keys appear.

**Cross-field invariants (post-parse, NOT in the runtime schema):**

OpenAI's strict structured-output mode supports a conservative subset of JSON Schema. To avoid the round-5 finding 2 risk that conditional keywords like `allOf` / `if` / `then` / `const` fail at reviewer invocation before any review happens, we keep the runtime `REVIEW_SCHEMA` constant in `scripts/parse_review.py` strictly limited to the well-supported subset (`object`, `properties`, `required`, `additionalProperties: false`, `type`, `enum`, `items`, `array`). Cross-field invariants are enforced in client-side Python after the response is parsed:

```python
def validate_review_invariants(parsed: dict) -> None:
    """Post-parse invariant check. Raises ReviewSchemaError on violation.
    Runs after `json.loads(response.output_text)` and JSON Schema validation.
    """
    status = parsed["status"]
    findings = parsed["findings"]
    open_qs = parsed["open_questions"]

    if status == "NO_FINDINGS":
        # NO_FINDINGS must be a true clean review — no findings AND no open questions.
        # Closes the D15/D17 ambiguity. (Round-1 finding 3, round-3 finding 2.)
        if findings or open_qs:
            raise ReviewSchemaError(
                f"NO_FINDINGS with non-empty findings ({len(findings)}) or "
                f"open_questions ({len(open_qs)}) — schema invariant violated"
            )
    elif status == "FINDINGS_PRESENT":
        # FINDINGS_PRESENT must carry actionable content: at least one finding
        # OR at least one open question. Pure-open-question response is OK.
        if not findings and not open_qs:
            raise ReviewSchemaError(
                "FINDINGS_PRESENT with empty findings AND empty open_questions"
            )
```

`ReviewSchemaError` triggers the same retry-once-then-fail logic as malformed JSON (D20). Phase 2 verification:
- `test_validate_review_invariants_rejects_no_findings_with_open_questions`
- `test_validate_review_invariants_rejects_findings_present_with_nothing`
- `test_validate_review_invariants_accepts_findings_present_with_only_open_questions`
- `test_review_schema_constant_uses_only_strict_safe_keywords` — static check that `REVIEW_SCHEMA` contains no conditional keywords; introspects the dict and asserts only the safe-subset keys appear at any level.
- `test_review_schema_accepted_by_openai_responses_api` — contract test sending the exact constant through the live OpenAI client (or recorded fixture); fails the build if OpenAI rejects the schema. Marked as a Phase 2 prerequisite — implementation cannot proceed past Phase 2 if this test fails.

**Reviewer-prompt corollary:** the prompt must instruct the reviewer that returning `NO_FINDINGS` requires *both* zero findings *and* zero open questions. If the reviewer wants to flag uncertainty, it must use `FINDINGS_PRESENT` with `open_questions` populated. Codex prose path uses the keyword sentinel `NO FINDINGS` (v1-compat); when the prose contains `NO FINDINGS` AND a co-located `OPEN QUESTIONS:` block, the parser coerces the response to `{status: "FINDINGS_PRESENT", findings: [], open_questions: [parsed list]}`. The post-parse invariant (§5.2.2) explicitly permits this shape (round-11 finding 2: previously the parser synthesized fake low-severity findings to surface the inconsistency, but that was redundant once the schema was relaxed in round 3 and only polluted counts/histograms with artifacts). Empty findings + non-empty open_questions is now a first-class valid state.

**Open-question identity (round-14 finding 2):** the reviewer schema models `open_questions` as a flat array of strings. Downstream consumers (§5.4.1 deferral flow, §5.7.3aa `deferrals_at_exit`) treat each open question as an item with a stable `item_id`. The mapping happens in client-side post-parse:

```python
def assign_open_question_ids(open_questions: list[str], round_n: int) -> list[OpenQuestion]:
    """Assign stable IDs to open questions in the order the reviewer emitted them.

    Format: oq_r{round}_{1-based-index}. Round-stable: re-running the parser on
    the same raw_response_text produces identical IDs. Cross-round-stable: each
    round produces a fresh prefix, so collisions across rounds are impossible.
    """
    return [
        OpenQuestion(id=f"oq_r{round_n}_{i+1}", text=question_text)
        for i, question_text in enumerate(open_questions)
    ]
```

The `OpenQuestion` dataclass carries `id` and `text` fields. Stored in the sidecar's `reviewer_response.open_questions` as objects (not bare strings) once IDs are assigned — the wire-level reviewer schema stays simple (bare strings, OpenAI-strict-compatible).

**Sidecar schema for populated `open_questions` (round-15 finding 1):** the in-sidecar form is fully specified in `scripts/sidecar_schema.json`:

```json
"open_questions": {
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "required": ["id", "text"],
    "properties": {
      "id": {
        "type": "string",
        "pattern": "^oq_r[0-9]+_[0-9]+$"
      },
      "text": {"type": "string"}
    }
  }
}
```

The `id` regex enforces the `oq_r{round}_{index}` format. Empty array is valid (when reviewer returned no open questions). The populated form is exactly the post-parse object shape from `assign_open_question_ids()` — no other shape is accepted in the sidecar. Phase 4 test `test_sidecar_open_questions_populated_form_validates` covers a populated case; `test_sidecar_open_questions_rejects_bare_strings` covers the schema-rejection case (raw reviewer output reaching the sidecar without ID assignment is a bug).

Severity definitions (in reviewer prompt):

- **high** — silent failures, data loss, backwards-compat breaks, security risks, contract violations between modules
- **medium** — verification gaps, ambiguous specifications that could lead to mis-implementation, missing edge-case coverage
- **low** — clarity/wording issues that won't change implementation behavior but reduce auditability

#### 5.2.1 Codex CLI severity inference (D23)

For the Codex prose path, parse each numbered finding and apply this heuristic in order:

```python
def infer_severity(finding_text: str) -> str:
    lower = finding_text.lower()
    if any(kw in lower for kw in ["silent", "silently", "data loss", "breaks compat",
                                   "backwards-compat", "security", "leak", "expose"]):
        return "high"
    if any(kw in lower for kw in ["gap", "ambiguous", "unclear", "missing test",
                                   "verification", "edge case", "may not"]):
        return "medium"
    return "low"
```

Documented as best-effort; OpenAI path is recommended for severity-critical work.

#### 5.2.2 Markdown rendering of findings in fixes-md

After parsing, the planner renders findings with severity prefix:

```markdown
1. **[HIGH]** [Pipeline / Workbook Consistency] The plan still has a hard interface gap...
2. **[MEDIUM]** [Verification / Compare Path] Test coverage too weak to prove...
3. **[LOW]** [Naming / Clarity] Field name `tco_per_actuator` is ambiguous...
```

This format is regex-parseable for round-state machine (§5.4) and human-readable.

### 5.3 Diff-aware reviewing (D11, D12, D14)

After round 1, the reviewer prompt changes shape. New script: `scripts/build_reviewer_prompt_v2.py`. The v1 builder is renamed to `build_reviewer_prompt_v1.py` (D16).

For round N > 1:

```xml
<role>...adversarial reviewer...</role>

<prior_rounds_summary>
Round N-1 raised K findings. Planner accepted A, rejected R, deferred D to user.
Severity histogram: high=X, medium=Y, low=Z
Cumulative cost so far: $C.CC
</prior_rounds_summary>

<prior_decisions>
<!-- Last 3 rounds verbatim (per D19); older rounds 1-line summary -->
<round n="N-3" summary="6 findings (h=2,m=3,l=1); accepted=4, rejected=2"/>
<round n="N-2" verbatim="...">...</round>
<round n="N-1" verbatim="...">...</round>
</prior_decisions>

<accepted_findings_to_verify>
For each accepted finding from round N-1, the planner stated this fix:
- Finding 1 (high, [Pipeline]): <verbatim text>
  Planner's stated edit: <verbatim from "Plan edits applied">
- Finding 2 (medium, [Verification]): ...
</accepted_findings_to_verify>

<rejected_findings_for_context>
The planner rejected these findings; do NOT re-raise unless new evidence:
- Finding 3 (was high, [Backwards-compat]): <verbatim>
  Rejection reason: <verbatim>
</rejected_findings_for_context>

<plan_diff>
<!-- unified diff between plan_v_n-1 and plan_v_n -->
diff --git a/plans/<slug>-<version>.md b/plans/<slug>-<version>.md
@@ -42,6 +42,12 @@
 ...
</plan_diff>

<full_plan>
<!-- full plan text, for cross-reference only -->
</full_plan>

<instructions>
Two-pass review:
1. VERIFY each accepted finding from prior round was actually addressed by the
   diff. Check the planner's stated edit matches what's in the diff. Disagreement
   between stated intent and actual edit is itself a finding. Re-raise as a
   finding if the fix is incomplete or introduces new risk.
2. ADVERSARIAL pass on the diff specifically. Look for new contracts, fields,
   or function signatures introduced this round. Find risks the planner
   missed in their own edits.

Do NOT re-raise findings already rejected in prior rounds unless new evidence
emerges from the diff. Do NOT scrub the unchanged sections — that's
plan-bloat-checking, not adversarial review.
</instructions>
```

#### 5.3.1 Diff generation

**Snapshot is the primary diff source after round 1.** Git is NOT used to compute the round-N-1→N delta during a normal run, because v1's git-write prohibition (carried over to v2 per §5.0) means no commits happen between rounds — `git diff HEAD~N` against an uncommitted plan would compute the cumulative diff from the loop's starting commit, not the round-by-round delta the reviewer needs.

```python
def compute_round_diff(plan_path: Path, round_n: int, slug: str, version: str) -> str:
    """Diff between prior-round snapshot and current plan. Snapshots are authoritative.

    Snapshots are namespaced by slug+version (round-6 finding 1) so a stale
    .scratch/ from a different plan can't poison this loop's diff. Even with
    the right name, the snapshot's content is hash-validated against the
    sidecar's plan_content_sha256 before use; a mismatch routes to sidecar
    recovery instead of trusting the file blindly.

    Round-7 finding 2: prior_snapshot_path is immutable; a separate
    prior_snapshot_valid flag controls the recovery branch. Helpers take slug
    and version explicitly — no closure over module-level state.
    """
    snapshot_dir = Path(".scratch")
    snapshot_dir.mkdir(exist_ok=True)
    prefix = f"{slug}-{version}-plan-snapshot-r"
    prior_snapshot_path = snapshot_dir / f"{prefix}{round_n - 1}.md"
    current_snapshot_path = snapshot_dir / f"{prefix}{round_n}.md"

    prior_snapshot_valid = False
    if prior_snapshot_path.exists():
        # Validate snapshot content against the prior-round sidecar's hash before
        # trusting it. This catches the edge case where `.scratch/` was preserved
        # but the sidecar was hand-edited (or vice versa).
        expected_hash = _read_sidecar_plan_hash(round_n - 1, slug, version)
        actual_hash = hashlib.sha256(prior_snapshot_path.read_bytes()).hexdigest()
        prior_snapshot_valid = bool(expected_hash) and actual_hash == expected_hash

    if not prior_snapshot_valid:
        # Snapshot missing or hash-mismatched — try sidecar-content recovery FIRST,
        # git only as last resort. Per round-4 finding 1, the §5.7.4a recovery
        # procedure must be in the executable path, not just documented.
        # Order: snapshot → sidecar → git.
        recovered = _recover_snapshot_from_sidecar(round_n - 1, slug, version)
        if recovered is not None:
            prior_snapshot_path.write_text(recovered, encoding="utf-8")
            prior_snapshot_valid = True
        else:
            return _recover_diff_from_git(plan_path, round_n)
    # prior_snapshot_path is now valid (either was, or we just recovered it)

    # `git diff --no-index` exits 0 when files are identical and 1 when they
    # differ — 1 is the COMMON case here. Do NOT use check_output / check=True,
    # which treats exit 1 as an exception and blocks every non-empty round
    # delta. Accept 0 and 1; reject other codes (real errors).
    result = subprocess.run(
        ["git", "diff", "--no-index", "--no-color",
         str(prior_snapshot_path), str(plan_path)],
        check=False, capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"git diff --no-index failed (exit {result.returncode}): {result.stderr}"
        )
    diff = result.stdout
    # Write current plan state as the snapshot for next round (namespaced).
    current_snapshot_path.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
    return diff


def _take_initial_snapshot(plan_path: Path, slug: str, version: str) -> None:
    """Called once before round 1 starts. Writes the BASELINE snapshot at
    index r1 — the plan state that round-1 edits will diverge from.

    Snapshot index semantic (round-8 finding 1): r{N} = plan state at START of
    round N. So r1 is the pre-loop baseline, r2 = post-round-1 (= start of
    round 2), etc. compute_round_diff(round_n=N) reads r{N-1} as 'before this
    round started' and diffs against the current plan, which is now r{N}.
    """
    snapshot_dir = Path(".scratch")
    snapshot_dir.mkdir(exist_ok=True)
    path = snapshot_dir / f"{slug}-{version}-plan-snapshot-r1.md"
    path.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")


def _recover_snapshot_from_sidecar(snapshot_index: int, slug: str, version: str) -> str | None:
    """Recover the snapshot at the given index from the sidecar audit trail.

    Snapshot semantic (per `_take_initial_snapshot`): r{N} = plan at START of
    round N. Sidecar M's `plan_content` field is the plan at END of round M,
    which equals r{M+1}. So recovering r{N} (for N>=2) requires reading
    sidecar-{N-1}'s plan_content.

    Special case (round-9 finding 1): r1 is the pre-loop baseline. It is
    persisted in the round-1 sidecar as `baseline_plan_content` /
    `baseline_plan_content_sha256` so that resume after `.scratch/` deletion
    can still reconstruct the round-2 diff accurately. For N==1, read those
    fields instead of `plan_content`.

    Round-7 finding 2: slug/version are explicit parameters, not closed-over
    module state.
    """
    if snapshot_index == 1:
        # Baseline lives in the round-1 sidecar's baseline_* fields.
        sidecar = Path("plans/fixs") / f"{slug}-{version}-round-1.json"
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        plan_content = data.get("baseline_plan_content")
        expected_hash = data.get("baseline_plan_content_sha256")
    elif snapshot_index >= 2:
        source_round = snapshot_index - 1
        sidecar = Path("plans/fixs") / f"{slug}-{version}-round-{source_round}.json"
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        plan_content = data.get("plan_content")
        expected_hash = data.get("plan_content_sha256")
    else:
        return None  # invalid index (< 1)

    if not plan_content or not expected_hash:
        return None
    actual_hash = hashlib.sha256(plan_content.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        # Tampering detected; refuse the recovery and let git fallback take
        # over (with banner). Resume flow §5.9 surfaces this to the user.
        return None
    return plan_content


def _read_sidecar_plan_hash(snapshot_index: int, slug: str, version: str) -> str | None:
    """Read the expected plan_content_sha256 for the snapshot at the given index.

    For snapshot_index >= 2: read sidecar-(N-1)'s `plan_content_sha256`.
    For snapshot_index == 1 (baseline): read round-1 sidecar's
    `baseline_plan_content_sha256`. (Round-9 finding 1: previously this returned
    None for the baseline, which made every round-2 r1 snapshot fail validation
    even on the happy path.)
    """
    if snapshot_index == 1:
        sidecar = Path("plans/fixs") / f"{slug}-{version}-round-1.json"
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data.get("baseline_plan_content_sha256")
    if snapshot_index >= 2:
        source_round = snapshot_index - 1
        sidecar = Path("plans/fixs") / f"{slug}-{version}-round-{source_round}.json"
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data.get("plan_content_sha256")
    return None  # invalid index
```

#### 5.3.2 Git as recovery, not primary

`_recover_diff_from_git()` is only invoked when snapshots are missing (resume path, or `.scratch/` was wiped between sessions). Even there, it doesn't try to reconstruct round-by-round deltas — it produces a "best-effort" diff against the last committed plan version, with a banner in the reviewer prompt noting "diff is recovered from git, not snapshot-accurate." The reviewer then knows to apply the verify-then-attack pass against the cumulative diff rather than expecting round-N-1→N precision.

`.scratch/` is in `.gitignore` (D13). Snapshots auto-cleaned on loop exit (approved / planner-locked / ceiling / cancelled). The initial snapshot at round 1 captures the "round 0" baseline so round 2 has something to diff against.

### 5.4 Severity-gated exit (D4, D15, D17)

Exit conditions, evaluated in priority:

1. **Approved** — reviewer returns `status: NO_FINDINGS`. The schema (§5.2) guarantees this implies zero findings AND zero open_questions in the same response, so the D15/D17 ambiguity is gone. End report may still list user-deferred items from prior rounds (those don't block this exit since the planner already explicitly accepted the deferral with reasons logged).
2. **Resolved** — current round has (a) zero unresolved high-severity findings, (b) zero unresolved open-questions, AND (c) every medium finding either Accepted (and edited) or Rejected with stated reason — i.e. no medium is left in unresolved/uncertain state. Lows do not block exit. This closes the round-3 finding-1 gap: mediums (verification gaps, ambiguous specs) cannot silently leak past `Resolved`.
3. **Resolved-with-deferrals** — open highs OR open questions OR open mediums remain, but the user explicitly deferred them via `AskUserQuestion` with stated reasons (D4). End report prominently lists deferred items + reasons, broken out by severity. Deferred mediums must be tagged with the implementation phase or follow-up plan version that will address them (e.g. "deferred to v2.1 verification work").
4. **Planner-locked** — every finding rejected this round AND no open highs from prior rounds (kept for parity with v1).
5. **Ceiling hit** — round count >= `ADVERSARIAL_MAX_ROUNDS` (default 20, was 10).
6. **Cost-capped** — cumulative OpenAI spend exceeds `ADVERSARIAL_MAX_COST_USD` (default $5; D10) AND user declines to extend.

#### 5.4.1 Soft-block flow

When the loop would otherwise exit (ceiling, planner-locked, cost-cap) but unresolved highs OR open-questions OR open mediums remain, surface to user (round-11 finding 1: previously this gate omitted mediums, allowing them to leak through non-success exits and reintroducing the round-3 ambiguity).

The flow is a **two-step interaction**: first pick the high-level action, then for each open item collect the specific deferral reason and (for mediums) target version. The two-step shape is required by D4 + Resolved-with-deferrals: those decisions mandate that every deferred item carries an explicit reason, and every deferred medium carries a tagged follow-up target. Without the second step, the audit requirement cannot be satisfied as written (round-12 finding 3).

```python
# Step 1: high-level action
action = ask_user(
    f"Loop is exiting ({exit_reason}) but there are still:\n"
    f"  - {len(open_highs)} unresolved high-severity findings\n"
    f"  - {len(open_mediums)} unresolved medium-severity findings\n"
    f"  - {len(open_questions)} open questions\n"
    "How to proceed?",
    options=[
        "Defer all (collect reasons + targets in next step)",
        "Continue looping despite the exit condition",
        "Exit anyway, accept all risk (no per-item input)",
    ]
)

if action == "defer":
    # Step 2: per-item reason + (for mediums) follow-up target.
    # AskUserQuestion supports up to 4 sub-questions per call; chunk
    # if open items exceed 4. For each item, "Other" allows free-text.
    deferrals = []
    for item in open_highs + open_mediums + open_questions:
        sub_answer = ask_user_per_item(item)  # collects reason text
        target = None
        if item.severity == "medium":
            # Mediums need a tagged follow-up; UI offers common targets
            # plus "Other" for free-text version specifier.
            target = ask_user(
                f"Follow-up target for medium finding {item.id}?",
                options=[
                    f"v{current_version}.1 (next minor)",
                    "Next implementation phase (Phase X)",
                    "Backlog (no scheduled version)",
                    # "Other" auto-added by AskUserQuestion for free-text
                ]
            )
        deferrals.append({
            "item_id": item.id,
            "severity": item.severity,
            "reason": sub_answer.text,        # required free-text
            "target_version": target,         # required for mediums, null otherwise
        })
    sidecar["deferrals_at_exit"] = deferrals  # persisted for audit
elif action == "accept_all_risk":
    # Round-14 finding 1: target_version must be non-null for mediums per
    # §5.7.3aa, otherwise schema validation rejects the sidecar and the
    # "accept all risk" path cannot complete. Force a sentinel string for
    # ALL items (not just mediums) to keep the field semantically uniform.
    sidecar["deferrals_at_exit"] = [
        {"item_id": item.id, "severity": item.severity,
         "reason": "accepted at exit", "target_version": "accepted-at-exit"}
        for item in open_highs + open_mediums + open_questions
    ]
# elif action == "continue": no exit; loop continues
```

The `deferrals_at_exit` field is added to the sidecar schema (§5.7.1) as an optional top-level array, present only when a soft-block action of "defer" or "accept_all_risk" was taken on the final round. Round-end report renders the deferrals into the markdown end-of-loop section so future readers see exactly what was deferred and why. Lows are intentionally NOT in this gate — they are clarity/wording issues that don't change implementation behavior, and forcing user attention on every low at every exit would be high-friction without proportional value.

### 5.5 Plan-bloat detection (D5, D26)

Track plan size per round. After round N >= `ADVERSARIAL_BLOAT_WINDOW` (default 3):

```python
growth = (size_now - size_n_minus_window) / size_n_minus_window
new_high_findings = sum(
    1 for f in current_round.findings
    if f.severity == "high" and f.id not in prior_round_finding_ids
)

if growth > ADVERSARIAL_BLOAT_THRESHOLD and new_high_findings == 0:
    answer = ask_user(
        "Plan-bloat warning: plan grew >20% over last 3 rounds with no new "
        "high-severity findings. Reviewer may be scrubbing for cross-reference "
        "consistency rather than finding new architectural risks.",
        options=[
            "Continue normally",
            "Switch to consistency-only mode (narrow reviewer prompt; exit on next clean round)",
            "Exit now with bloat note in end report",
        ]
    )
```

#### 5.5.1 Consistency-only mode (D9)

When user picks consistency-only, swap the reviewer prompt's `<instructions>` block:

```xml
<instructions>
You are now in CONSISTENCY-ONLY MODE. Your task this round is narrow:
1. Find stale cross-references introduced by prior edits (e.g., section A
   references field X that was renamed to Y in section B).
2. Find duplicated specs (two sections both claim authority over the same
   contract with conflicting wording).
3. Find dangling references (mentions of files / sections that no longer exist).

Do NOT raise new architectural concerns. Do NOT re-evaluate design decisions.
If you find no consistency issues, return NO_FINDINGS to exit the loop.
</instructions>
```

Once consistency-only mode is set, it persists for remaining rounds. Exits as soon as reviewer returns NO_FINDINGS (likely 1-2 rounds).

### 5.6 First-run UX (D6, D7)

This is **step 2** of the locked startup ordering (§5.0a) — runs after the git pre-flight check and before the slug/version prompt.

```python
has_openai = bool(os.environ.get("OPENAI_API_KEY"))
has_codex = is_codex_cli_available()

if not has_openai and not has_codex:
    answer = ask_user(
        "No reviewer transport configured. How would you like to proceed?",
        options=[
            "I have an OpenAI API key (recommended — enables severity tagging)",
            "I have Codex CLI installed (legacy fallback)",
            "I need help setting one up",
        ]
    )
    if answer == "openai":
        key = ask_user("Paste your OpenAI API key:", input_type="text")
        save_to_env_file(".env", "OPENAI_API_KEY", key)
        # Reload .env, retry transport detection
    elif answer == "codex":
        run("/codex:setup")
    else:
        print_setup_guide_and_exit()
```

Setup guide content:
- OpenAI: link to https://platform.openai.com/api-keys, suggest creating a key with $10/month limit, paste into `.env`
- Codex: link to https://github.com/openai/codex install instructions
- Either way: re-run the skill once configured

Cached: once `OPENAI_API_KEY` is in `.env`, subsequent runs skip the prompt (D7).

### 5.7 File-gated artifacts — JSON authoritative, markdown rendered

Each round produces **two** persisted artifacts. The JSON sidecar is the **authoritative source of truth**; the markdown fixes-md is rendered from it.

1. **JSON sidecar** — `plans/fixs/<slug>-<version>-round-{N}.json`. Schema-validated structured record. This is the source of truth for the loop state machine, cost tracker, severity histograms, and any future tooling.
2. **Markdown fixes-md** — `plans/fixs/<slug>-<version>-fixes.md`. Append-only human-readable transcript, **rendered from the per-round JSON sidecars** at the end of each round (§5.7.6). Humans read this; nothing programmatic depends on it.

Both files are **committed to the repo** alongside the plan. They are NOT gitignored — both are part of the audit trail.

**Authoritativeness rule:** if markdown and JSON disagree on any field, JSON wins. Hand-editing the markdown does NOT change the loop's state — the next round will regenerate the markdown from JSON, overwriting any manual edits. (To preserve a typo fix, edit the JSON instead, or accept that the next render will overwrite. The markdown is a rendered view, not a source.)

#### 5.7.1 JSON sidecar schema

```json
{
  "schema_version": "2.0.0",
  "round": 5,
  "started_at": "2026-05-03T14:22:11Z",
  "completed_at": "2026-05-03T14:22:58Z",
  "transport": "openai",
  "model": "gpt-5.5",
  "raw_response_text": "{\"status\":\"FINDINGS_PRESENT\",\"findings\":[...],\"open_questions\":[]}",
  "plan_content_sha256": "a3f2c9e1...",
  "plan_content": "# Adversarial Plan Review — v2 Plan\n\n> Status: ...\n... (full plan markdown text at end of round) ...",
  "baseline_plan_content_sha256": "b1e2f8d3...",
  "baseline_plan_content": "# Adversarial Plan Review — v2 Plan\n\n... (full plan markdown text at the BASELINE — pre-round-1, only present in round-1 sidecar; null in subsequent rounds) ...",
  "restart_metadata": null,
  "deferrals_at_exit": null,
  "reviewer_response": {
    "status": "FINDINGS_PRESENT",
    "findings": [
      {
        "id": "f5.1",
        "severity": "high",
        "category": "Pipeline / Workbook Consistency",
        "where": "§5.3 Drive selection",
        "what_can_go_wrong": "...",
        "concrete_fix": "..."
      }
    ],
    "open_questions": [
      {"id": "oq_r5_1", "text": "Should the deferral target_version field accept arbitrary strings or be enum-restricted to known release labels?"},
      {"id": "oq_r5_2", "text": "Is it acceptable for the consistency-only reviewer prompt to skip the `<accepted_findings_to_verify>` block entirely?"}
    ]
  },
  "planner_decisions": [
    {
      "finding_id": "f5.1",
      "decision": "accept",
      "rationale": "...",
      "stated_edit": "Section §5.3 paragraph 2: replace ..."
    }
  ],
  "plan_edits_applied": [
    {"section": "§5.3", "summary": "Reworded selection-as-data-transformation"}
  ],
  "stats": {
    "tokens_input": 12438,
    "tokens_output": 2194,
    "cost_usd": 0.13,
    "cumulative_cost_usd": 0.41,
    "duration_seconds": 47.2,
    "plan_size_chars": 8213,
    "plan_size_delta": 312,
    "severity_histogram": {"high": 2, "medium": 3, "low": 1}
  }
}
```

#### 5.7.2 Why two artifacts under one authority

| Artifact | Primary consumer | Role |
|---|---|---|
| **JSON sidecar (authoritative)** | `loop_state.py` state machine, cost tracker, severity histogram, future v3 analytics, resume detection | Schema-validated structured data. All loop decisions read from here. |
| Markdown fixes-md (rendered) | Humans reading audit trail; PR reviewers; debugging | Generated from JSON via templating after each round. Renders structured fields plus the literal `raw_response_text` for audit fidelity (round-11 finding 4). |

The markdown alone cannot losslessly carry transport, model, exact token usage, schema_version, or finding-ID stability across renderings. Going JSON-first eliminates the regex-parsing brittleness v1 had.

**`raw_response_text` field (audit fidelity):** the JSON sidecar stores the literal unparsed reviewer output as `raw_response_text`. For OpenAI path, this is the raw JSON string before parsing. For Codex prose path, this is the raw markdown-style text the reviewer emitted. The renderer (§5.7.6) appends this verbatim under a `### Reviewer raw response` subsection in the markdown so v1-style debugging parity is preserved — a reader can see exactly what the reviewer said, not just the parsed projection. Round-11 finding 4 closed: previous prose claimed "verbatim Codex prose" but the JSON didn't carry it; now it does.

#### 5.7.3 Loop gating

The loop refuses to advance to round N+1 if:
- Round N's JSON sidecar is missing OR fails schema validation, OR
- Round N's markdown section is missing OR fails to match the expected structure

Sidecar is written first (atomic write to `.tmp` extension, then rename). Markdown is rendered second, from the just-written JSON. If round N partially completes (Claude session interrupted between sidecar write and markdown render), the loop detects the gap on resume (§5.9) and re-renders the markdown from the JSON without re-running the round.

#### 5.7.3aa `deferrals_at_exit` field (soft-block audit, optional)

The `deferrals_at_exit` field is `null` for all rounds except the final round of a loop where the soft-block flow (§5.4.1) chose `defer` or `accept_all_risk`. Its schema:

```json
"deferrals_at_exit": {
  "type": ["array", "null"],
  "items": {
    "type": "object",
    "additionalProperties": false,
    "required": ["item_id", "severity", "reason"],
    "properties": {
      "item_id": {"type": "string"},
      "severity": {"enum": ["high", "medium", "low", "open_question"]},
      "reason": {"type": "string"},
      "target_version": {"type": ["string", "null"]}
    }
  }
}
```

**Per-field semantics:**

- `item_id` — finding ID for findings, or open-question identifier for open questions. Stable across the loop.
- `severity` — copies the source finding's severity, OR the literal string `"open_question"` for open questions (which have no native severity in §5.2's schema).
- `reason` — required free-text deferral reason, collected via the §5.4.1 step-2 per-item AskUserQuestion. Cannot be empty string. For `accept_all_risk` action, the reason is auto-populated as `"accepted at exit"`.
- `target_version` — required (non-null) when `severity == "medium"`; null otherwise. For mediums this captures the user's tagged follow-up version (e.g. `"v2.1"`, `"phase 4 implementation"`, `"backlog"`) per the Resolved-with-deferrals contract. Validation: if severity=medium and target_version is null, schema validation fails.
- **Sentinel value for `accept_all_risk` (round-14 finding 1):** when the user picks `accept_all_risk` instead of per-item deferral, the literal string `"accepted-at-exit"` is written for **every** item's `target_version` — including mediums. This satisfies the medium-target non-null requirement without forcing the user to provide individual targets in the "I just want to exit" UX. Tooling that aggregates target_versions across runs should treat `"accepted-at-exit"` as a special value distinct from real version strings (e.g. exclude from "deferred-to-v2.1" filters).

The strict null/populated split with the medium-target requirement is enforced in `scripts/sidecar_schema.json`. Phase 4 tests cover all four cases: defer with mediums + real targets, defer with highs/open_questions only (target_version null), accept_all_risk auto-populating reasons + `"accepted-at-exit"` sentinel, and the schema-rejection case where a medium has null target_version (validates the schema rule itself).

#### 5.7.3a `restart_metadata` field (start-over audit, optional)

The `restart_metadata` field is `null` for normal rounds. It is populated **only on the first sidecar (round 1) that follows a "Start over" destructive operation** (§5.0). The schema definition for the populated form:

```json
"restart_metadata": {
  "type": ["object", "null"],
  "additionalProperties": false,
  "required": ["timestamp", "deleted_files", "user_decision"],
  "properties": {
    "timestamp": {"type": "string"},
    "deleted_files": {
      "type": "array",
      "items": {"type": "string"}
    },
    "user_decision": {"type": "string"},
    "previous_run_summary": {
      "type": "object",
      "additionalProperties": false,
      "required": ["last_round", "last_status"],
      "properties": {
        "last_round": {"type": "integer"},
        "last_status": {"type": "string"}
      }
    }
  }
}
```

`null` is the universal default; only round-1 sidecars produced by a destructive restart populate it. The schema gate (§5.7.3) accepts both the null and the populated forms — round-8 finding 2 closes the contradiction where §5.0 mandated the field but §5.7.1 schema didn't allow it. Phase 4 verification adds `test_start_over_round_1_sidecar_carries_restart_metadata` and `test_normal_round_1_sidecar_has_null_restart_metadata`.

#### 5.7.4 Schema versioning

The JSON sidecar carries a top-level `schema_version` field (string, semver-ish — starts at `"2.0.0"`). It is **required** in `scripts/sidecar_schema.json` and present at the top of the §5.7.1 example. When v3 adds fields or changes shape, schema_version bumps. The parser reads `schema_version` first and dispatches to the right reader; sidecars with a missing or unsupported version fail validation and the loop refuses to advance (per §5.7.3). Tests in Phase 4 cover both a missing field and an unsupported value (e.g. `"3.0.0"` from a future v3 run).

#### 5.7.3b Baseline fields (round-1 sidecar only)

The round-1 sidecar carries two extra fields that subsequent sidecars set to `null`:

- `baseline_plan_content` — the full plan markdown text **before any round-1 edits** (i.e., the r1 baseline, captured at `_take_initial_snapshot()` time).
- `baseline_plan_content_sha256` — SHA-256 of the above.

These fields exist solely to make the baseline (r1) recoverable from the audit trail when `.scratch/` is wiped. Without them, `_recover_snapshot_from_sidecar(1, ...)` would always return None (round-9 finding 1) and round-2 diffs would silently fall through to cumulative-against-HEAD git recovery — defeating diff-aware review on the very first iteration. With them, the baseline is durably persisted and validatable.

**Capture-timing requirement (round-10 finding 1):** the baseline values MUST be captured at `_take_initial_snapshot()` time (BEFORE round 1 starts and before any planner edit touches the plan), stored in loop state, and persisted verbatim into the round-1 sidecar at end of round 1. Capturing them at sidecar-write time would write the POST-round-1 plan into `baseline_plan_content`, defeating the field's purpose. Concretely:

```python
# At skill start — step 5 of §5.0a (begin round 1):
baseline_bytes = plan_path.read_bytes()
baseline_hash = hashlib.sha256(baseline_bytes).hexdigest()
loop_state.baseline_content = baseline_bytes.decode("utf-8")
loop_state.baseline_sha256 = baseline_hash
_take_initial_snapshot(plan_path, slug, version)  # writes r1 = baseline

# Round 1 runs, planner makes edits...

# End of round 1 — write round-1 sidecar:
sidecar = {
    ...
    "plan_content": post_round_1_plan_text,             # NEW: end-of-round-1 plan
    "plan_content_sha256": hashlib.sha256(...).hexdigest(),
    "baseline_plan_content": loop_state.baseline_content,  # captured BEFORE edits
    "baseline_plan_content_sha256": loop_state.baseline_sha256,  # captured BEFORE edits
    ...
}
```

Phase 4 test `test_round_1_baseline_differs_from_plan_content_after_edits` asserts that `baseline_plan_content_sha256 != plan_content_sha256` whenever the planner actually edited the plan in round 1 — catches an implementation that incorrectly captures both fields at sidecar-write time.

For round 2+ sidecars: both fields are `null`. Schema permits both populated and null forms (both fields nullable strings in `sidecar_schema.json`). Phase 4 verification adds `test_round_1_sidecar_carries_validated_baseline` and `test_round_n_sidecar_has_null_baseline_fields_for_n_gt_1`.

#### 5.7.4a Plan content embedded in sidecar (resume-accuracy guarantee)

The sidecar carries the **full plan markdown text at end of round N** in the `plan_content` field, plus its SHA-256 hash in `plan_content_sha256` for integrity. This makes round N-1→N diff reconstruction possible after `.scratch/` is wiped, without committing snapshots to git.

| Resume scenario | Source for prior plan state |
|---|---|
| `.scratch/<slug>-<version>-plan-snapshot-r{N-1}.md` exists AND hash matches sidecar | Use snapshot file. Validation hash source depends on snapshot index (round-10 finding 2): r1 → round-1 sidecar's `baseline_plan_content_sha256`; rN for N≥2 → sidecar-(N-1)'s `plan_content_sha256`. Always hash-validated, never "used directly" (round-7 finding 1). |
| Snapshot missing OR hash-mismatched, sidecar content validates | Call `_recover_snapshot_from_sidecar(N-1, slug, version)`; materialize recovered text to `.scratch/<slug>-<version>-plan-snapshot-r{N-1}.md` for downstream snapshot use. Recovery source mirrors validation: r1 from round-1 sidecar's `baseline_plan_content`; rN for N≥2 from sidecar-(N-1)'s `plan_content`. |
| Both missing | Last-resort: cumulative-against-HEAD diff via `_recover_diff_from_git()` with banner; otherwise refuse to resume |

This closes round-3 finding 3: the sidecar is now self-sufficient for accurate round-N-1→N diff reconstruction, regardless of `.scratch/` state. Per-round sidecar size grows by the plan text size (~10-30KB per round for typical plans), which is fine — sidecars are committed to the audit trail anyway and reads are infrequent.

The `plan_content_sha256` field lets the resume flow detect tampering: if `plan_content` was hand-edited but the hash wasn't updated, the loop refuses to resume and tells the user to either fix one or the other.

#### 5.7.5 Drift and tamper handling

Two distinct corruption scenarios with different policies (round-11 finding 3 — previously these were ambiguous):

**A. In-flight hash mismatch on a single artifact (graceful degrade).** During an active loop, when `compute_round_diff()` finds a snapshot whose hash doesn't match the corresponding sidecar field, OR finds a sidecar whose `plan_content` hash doesn't match `plan_content_sha256`, the loop falls through to the next-best source: snapshot → sidecar → git fallback (with the cumulative-against-HEAD banner in the reviewer prompt). This is the in-flight degraded mode and the user is informed but the loop continues. Recovery from a single corrupt artifact does not block forward progress.

**B. Resume validation hash mismatch (refuse to resume).** At skill startup during step 4 of §5.0a (resume detection), the loop walks every prior sidecar and runs schema + hash validation. If ANY sidecar has a `plan_content` whose hash doesn't match `plan_content_sha256`, OR a round-1 sidecar has a `baseline_plan_content` whose hash doesn't match `baseline_plan_content_sha256`, the loop refuses to resume. The user is shown which round failed validation and asked to (a) hand-fix the sidecar (then re-run resume), (b) start over (destructive op per §5.0), or (c) cancel. Resuming on tampered audit state is never silently allowed — a corrupted audit trail is a strong signal of either user error or external interference, neither of which should be papered over.

Markdown is always rebuilt on resume by re-rendering each sidecar through the §5.7.6 template; manual edits to markdown are silently overwritten with a warning log line. The user can hand-edit JSON if they need to (e.g. correct a typo in a verbatim finding). The next round will render markdown from the corrected JSON. Editing JSON requires schema validity — invalid JSON fails the gate (§5.7.3) and the loop won't advance.

#### 5.7.6 Markdown rendering template

The markdown for round N is templated from the JSON sidecar:

```markdown
## Round {round} — {started_at}

### Reviewer findings ({transport})
{render each finding as: "{n}. **[{severity.upper()}]** [{category}] {what_can_go_wrong}\n   *Concrete fix:* {concrete_fix}"}

{if open_questions: render as bullet list under "OPEN QUESTIONS:" header}

### Planner decisions
{render each decision verbatim}

### Plan edits applied
{render each edit verbatim}

### Round stats
- Reviewer: {transport} ({model})
- Tokens: {tokens_input:,} input / {tokens_output:,} output
- Cost: ${cost_usd:.2f} (cumulative: ${cumulative_cost_usd:.2f})
- Severity histogram: high={h}, medium={m}, low={l}
- Duration: {duration_seconds:.1f}s
- Plan size: {plan_size_chars:,} chars (Δ {plan_size_delta:+,} from round {N-1})

### Reviewer raw response
```text
{raw_response_text}
```
```

The trailing `### Reviewer raw response` subsection is mandatory (round-12 finding 1) — without it the audit-fidelity guarantee from §5.7.2 is half-specified: the sidecar would store the raw text but the rendered fixes log would silently drop it. The fenced `text` code block preserves whatever literal content the reviewer emitted (raw JSON for OpenAI path, prose for Codex path) without markdown re-interpretation.

The renderer is implemented in `scripts/render_markdown.py` (named in §6 Files list) and is pure: same JSON in → same markdown out, byte-stable. This makes drift detection (§5.7.5) trivial.

### 5.8 Round stats subsection (D21)

Each round's fixes-md section gains a `### Round stats` subsection:

```markdown
### Round stats

- Reviewer: OpenAI Responses API (gpt-5.5)
- Tokens: 12,438 input / 2,194 output
- Cost: $0.13 (cumulative: $0.41)
- Severity histogram: high=2, medium=3, low=1
- Duration: 47.2s
- Plan size: 8,213 chars (Δ +312 from round N-1)
```

This makes convergence trajectory readable at a glance: a healthy run should show `high` counts trending toward 0.

### 5.9 Resume support (D22)

This is **step 4** of the locked startup ordering (§5.0a) — runs after slug/version is known, since it needs the path to look for prior round JSON sidecars.

**JSON sidecars are the only source consulted for resume state.** Markdown fixes-md is *not* read; it is regenerated from the sidecars during the resume flow.

```python
fixs_dir = Path("plans/fixs")
sidecar_glob = f"{slug}-{version}-round-*.json"
sidecars = sorted(fixs_dir.glob(sidecar_glob), key=parse_round_number)

if sidecars:
    last_round = parse_round_number(sidecars[-1])
    if last_round < ADVERSARIAL_MAX_ROUNDS:
        answer = ask_user(
            f"Found {len(sidecars)} prior round sidecars (last: round {last_round}).",
            options=[
                f"Resume from round {last_round + 1}",
                "Start over (delete all sidecars and fixes-md, begin from round 1)",
                "Cancel",
            ]
        )
```

**On resume:**
1. Validate every sidecar against `scripts/sidecar_schema.json`. Any sidecar that fails validation aborts the resume — the user is shown which round failed and asked to either fix the JSON manually (re-validate on next start) or start over.
2. Recover cumulative cost from the latest sidecar's `stats.cumulative_cost_usd` field — unlike v1, no cost continuity is lost on resume.
3. **Regenerate the markdown fixes-md from scratch** by rendering each sidecar in order through the §5.7.6 template. Any manual edits to the markdown are overwritten without prompt; if the user wants to preserve narrative tweaks, they must edit the sidecars (which are schema-validated).
4. Restore snapshot files in `.scratch/` in this exact priority order (matches §5.3.1's `compute_round_diff()` so resume and round-N+1 diff agree on the source of truth):
   1. **Namespaced snapshot present AND hash matches sidecar** — use it. The hash check is mandatory (round-7 finding 1). Hash source by snapshot index (round-10 finding 2):
      - **r1 (baseline):** validate against round-1 sidecar's `baseline_plan_content_sha256` field, **not** `plan_content_sha256` (which is the post-round-1 plan, a different value).
      - **rN for N ≥ 2:** validate against sidecar-(N-1)'s `plan_content_sha256` (which represents end-of-round-(N-1) = start-of-round-N).

      A namespaced snapshot whose content does not match the appropriate hash is treated as missing and routes to step 4.2.
   2. **Snapshot missing or hash-mismatched, but sidecar's `plan_content` validates** — call `_recover_snapshot_from_sidecar(N, slug, version)`, materialize the recovered text to `.scratch/<slug>-<version>-plan-snapshot-r{N}.md`, then proceed normally.
   3. **Both snapshot and sidecar content unavailable** — fall back to `_recover_diff_from_git()` with the cumulative-against-HEAD banner in the round-N+1 reviewer prompt. This is a degraded mode and the user is warned.

   Git fallback is the **last resort**, not an automatic preference whenever snapshots are absent. Round-5 finding 1 closes the contradiction between §5.3.1 (which already had the right order) and §5.9 (which previously said "git fallback on missing snapshot"). Round-7 finding 1 closes the "use existing snapshot as-is" loophole — every snapshot use is hash-gated. Phase 4 verification adds `test_resume_recovers_snapshot_from_sidecar_when_scratch_deleted` and `test_resume_rejects_stale_snapshot_via_hash_mismatch` to lock in the priority order against future regressions.

If a sidecar is missing for any round in the middle of the sequence (e.g. round 3 sidecar exists but round 4 doesn't, then round 5 does), the resume flow refuses to proceed — the gap implies state corruption that can't be safely papered over. The user is shown the gap and asked to either delete subsequent sidecars (truncate to last contiguous round) or start over.

**The markdown is never authoritative for resume.** Hand-edited markdown is silently re-rendered. This eliminates the §5.9 vs §5.7 contradiction Codex flagged in round 2 finding 2.

## 6. Files to modify / add

| File | Change |
|---|---|
| `SKILL.md` | Rewrite Setup, Loop, Termination sections per §5; add first-run UX (§5.6); raise ceiling default to 20; document new env vars |
| `scripts/reviewer.py` | NEW — transport abstraction (§5.1), `invoke_reviewer()` |
| Codex plugin's `scripts/codex-companion.mjs` | UPDATE — read prompt from stdin when no positional arg given (§5.1.2 fix for Windows command-line length); add long-prompt regression test |
| `scripts/parse_review.py` | NEW — `ReviewResult` dataclass, OpenAI structured parser, Codex prose parser with severity heuristic (§5.2) |
| `scripts/render_markdown.py` | NEW — pure renderer that turns a sidecar JSON into the markdown fixes-md round section (§5.7.6); byte-stable; called once per round end and on resume to regenerate fixes-md from sidecars |
| `scripts/build_reviewer_prompt_v2.py` | NEW — diff-aware prompt builder (§5.3) |
| `scripts/build_reviewer_prompt.py` | RENAME to `build_reviewer_prompt_v1.py` (kept for Codex-only fallback per D16) |
| `scripts/loop_state.py` | NEW — state machine: track findings across rounds, severity histogram, exit gates (§5.4), plan-bloat metric (§5.5), resume detection (§5.9), atomic write of markdown + JSON sidecar per round (§5.7) |
| `scripts/sidecar_schema.json` | NEW — JSON schema for round sidecars (§5.7.1); validated by `loop_state.py` on read/write |
| `scripts/cost_tracker.py` | NEW — token-cost estimation per model + cumulative tracker (§5.4 cost-cap, §5.8 stats) |
| `scripts/first_run.py` | NEW — first-run UX flow (§5.6), `.env` file creation/update |
| `.env.example` | UPDATE — add `ADVERSARIAL_TRANSPORT`, `ADVERSARIAL_MAX_ROUNDS`, `ADVERSARIAL_MAX_COST_USD`, `ADVERSARIAL_BLOAT_THRESHOLD`, `ADVERSARIAL_BLOAT_WINDOW`, `OPENAI_REVIEWER_MODEL` (default `gpt-5.5`) |
| `.gitignore` | UPDATE — add `.scratch/` |
| `tests/test_reviewer.py` | NEW — unit tests for transport selection, prompt building, parser |
| `tests/test_loop_state.py` | NEW — unit tests for exit gates, severity tracking, bloat detection |
| `tests/fixtures/` | NEW — synthetic plan + 3-round canned reviewer transcripts for regression |
| `README.md` | NEW (D25) — full guide: install both transports, env var reference, workflow walkthrough, cost estimation example, troubleshooting |

## 7. Implementation phases (D1)

Two milestones:

### Milestone A: Phases 1+2 (transport + structured outputs)
**Tag: `v2.0.0-alpha.1`**

- Phase 1: Transport abstraction
  - `scripts/reviewer.py` with both paths
  - `scripts/first_run.py` with first-run UX
  - `scripts/cost_tracker.py` for OpenAI cost tracking
  - SKILL.md pre-flight section updated to call first-run UX
  - `.env.example` updated
  - **Behavior:** v1 loop logic still in place; transport abstraction is just a swap-in for the existing Codex-only call. Severity not yet enforced.

- Phase 2: Structured outputs + parser
  - `scripts/parse_review.py` with schema + parsers for both transports
  - SKILL.md fixes-md output updated to render findings with severity prefix `**[HIGH]** [Category]`
  - Round stats subsection (§5.8) lands here
  - **Behavior:** severity tags appear in fixes-md, but exit logic still v1-style (round count + planner-rejects-all). Visible signal without behavior change.

### Milestone B: Phases 3+4 (diff-aware + severity-gated exit)
**Tag: `v2.0.0`**

- Phase 3: Diff-aware reviewing
  - `scripts/build_reviewer_prompt_v2.py`
  - Snapshot mode for uncommitted plans
  - SKILL.md uses v2 builder by default
  - **Behavior:** review prompt much tighter; round-N>1 reviews are diff-focused

- Phase 4: Severity-gated exit + bloat detection + resume
  - `scripts/loop_state.py` with all gates
  - SKILL.md raises ceiling to 20, adds resolved/bloat exit reasons, resume flow
  - First-run UX final polish
  - README.md ships
  - **Behavior:** full v2 — convergence-driven exit, bloat detection, resume, README documentation

Each milestone is a tagged release. Milestone A unlocks the OpenAI transport for users; Milestone B unlocks the convergence improvements.

## 8. Verification

Phase-tagged tests:

### Phase 1 (transport)
- `test_reviewer_selects_openai_when_key_set`
- `test_reviewer_selects_codex_when_only_cli_available`
- `test_first_run_prompts_user_when_neither_available`
- `test_first_run_saves_openai_key_to_env_file`
- `test_cost_tracker_increments_on_openai_response`
- `test_cost_tracker_records_per_round_cost_in_sidecar` (replaces the loop-gating test, which moves to Phase 4 since cost-cap pause is gating behavior per §5.4)

### Phase 2 (structured outputs)
- `test_openai_response_parses_to_review_result`
- `test_openai_strict_schema_rejects_invalid_severity`
- `test_codex_prose_parses_with_inferred_severity`
- `test_codex_severity_keyword_high_for_silent_failure`
- `test_codex_severity_keyword_medium_for_gap`
- `test_fixes_md_round_section_includes_round_stats`
- `test_fixes_md_findings_have_severity_prefix`

### Phase 3 (diff-aware)
- `test_round_2_prompt_includes_diff_and_prior_decisions`
- `test_round_2_prompt_includes_planner_rejection_reasons`
- `test_round_2_prompt_includes_planner_edits_applied_bullets`
- `test_uncommitted_plan_falls_back_to_snapshot_mode`
- `test_round_history_truncates_after_3_rounds`
- `test_malformed_json_retries_once_then_fails_round`

### Phase 4 (severity-gated exit + bloat + resume)
- `test_loop_exits_resolved_when_no_high_remaining`
- `test_loop_soft_blocks_with_open_high_severity`
- `test_loop_soft_blocks_with_open_medium_severity` (round-11 finding 1: mediums included in gate)
- `test_loop_exits_resolved_with_deferrals_after_user_defer`
- `test_no_findings_schema_rejects_non_empty_open_questions` (per round-1 finding 3 fix)
- `test_no_findings_exits_approved_with_zero_open_questions`
- `test_open_question_blocks_exit_until_resolved`
- `test_plan_bloat_warning_fires_on_20pct_growth_no_new_high`
- `test_consistency_only_mode_narrows_reviewer_prompt`
- `test_resume_detects_existing_json_sidecars_and_prompts_user`
- `test_cost_cap_pauses_loop_when_exceeded` (moved here from Phase 1; loop-gating per §5.4)
- `test_renderer_appends_reviewer_raw_response_section` (round-12 finding 1: §5.7.6 mandatory section)
- `test_renderer_raw_response_text_preserved_byte_stable` (no markdown re-interpretation; round-trip identity)
- `test_deferrals_at_exit_serialization_with_medium_targets` (round-13 finding 1 schema; medium severity must carry non-null target_version)
- `test_deferrals_at_exit_serialization_open_questions_no_target` (open_question entries have null target_version, schema accepts)
- `test_deferrals_at_exit_null_for_non_final_rounds` (only populated on final round when soft-block fired)
- `test_accept_all_risk_branch_auto_populates_reason` (round-12 finding 3: accept_all_risk uses literal "accepted at exit" reason)
- `test_soft_block_step2_collects_per_item_reasons` (round-12 finding 3: per-item AskUserQuestion captures free-text reason)
- `test_soft_block_step2_collects_medium_target_version` (round-12 finding 3: mediums get follow-up target tag)
- `test_assign_open_question_ids_format_oq_r_round_index` (round-14 finding 2: IDs match `oq_r{round}_{index}` regex; round-stable)
- `test_assign_open_question_ids_cross_round_unique` (round-14 finding 2: re-running parse on round-N text never collides with round-M IDs)
- `test_accept_all_risk_writes_sentinel_target_version` (round-14 finding 1: every deferred item, including mediums, gets `target_version: "accepted-at-exit"`)
- `test_accept_all_risk_passes_sidecar_schema_with_open_mediums` (round-14 finding 1: end-to-end — accept_all_risk on a medium-bearing exit must produce a sidecar that passes schema validation, NOT a target_version=null failure)
- `test_sidecar_open_questions_populated_form_validates` (round-15 finding 1: sidecar accepts `[{id, text}]` shape per `oq_r{round}_{index}` regex)
- `test_sidecar_open_questions_rejects_bare_strings` (round-15 finding 1: raw reviewer string-form reaching sidecar without ID assignment is a schema rejection)

End-to-end smoke test (per milestone):

- **Milestone A:** synthetic plan + frozen reviewer responses → loop runs to completion with severity tags visible, costs tracked, exit on v1 conditions
- **Milestone B:** synthetic plan with 3 known issues (1 high, 1 medium, 1 low) → loop converges in <= 3 rounds via severity-gated exit; second smoke test where bloat warning fires

## 9. Backward compatibility

- Existing v1 fixes-md transcripts are read-compatible (parser handles missing severity tags by inferring via D23 heuristic on legacy prose)
- Codex CLI path produces v1-format output if user has no `OPENAI_API_KEY` — no regression for current users; degraded severity inference flagged in README
- Plans drafted under v1 unchanged — v2 adds review machinery, not plan format
- SKILL.md trigger phrases unchanged (frontmatter `description` field stays identical)
- v1 prompt builder (`build_reviewer_prompt_v1.py`) kept for debugging the Codex path

## 10. Open questions (post-Q&A)

The major design Qs were resolved during interview. Remaining minor ones:

- **Q1.** Should `gpt-5.5-pro` be a documented alternative model in the README (for users who want to spend more for higher accuracy)? Recommend: yes, in cost-estimation section. Default stays `gpt-5.5`.
- **Q2.** When resume picks up at round N+1, should the reviewer get an explicit hint that "this is a resumed session, prior context is from a previous Claude session"? Recommend: no — the prior-rounds context already carries everything needed; the reviewer doesn't need session metadata.
- **Q3.** For users on Windows specifically (target deployment), are there any path-handling edge cases in `scripts/reviewer.py`'s subprocess calls to Codex? Recommend: Phase 1 testing should run on Windows; mark as a verification-task line item.

## 11. Out of scope (deferred to v3)

- **Cross-family multi-reviewer** — run a non-OpenAI reviewer (Gemini 2.5 Pro, DeepSeek V3, etc.) in parallel with OpenAI. Requires research on which families give genuinely independent perspectives vs. correlated blind spots. Two OpenAI models (Codex + OpenAI direct) explicitly excluded since their training data overlaps too heavily.
- **CI / GitHub Actions integration** — automatic plan review on PRs touching `plans/`. Requires designing a non-interactive mode (no AskUserQuestion), output formatting for PR comments, and handling secrets in Actions context.
- **Cost dashboard / historical analytics** — query "all my plan reviews this month, total cost, average rounds, severity distribution"
- **Streaming reviewer output** — show partial findings as the reviewer is generating
- **Plan-format-aware review** — markdown table consistency, section dependency tracking, broken-link detection
- **Programmatic invocation** — using the loop machinery from outside Claude Code (e.g. as a standalone Python CLI)

---

**Status:** transport/diff/severity-gate core is design-locked and ready for Phase 1+2 implementation. Residual risk after 16 rounds of dogfooded review sits in the round-13/14/15 exit-audit additions (`deferrals_at_exit` schema, open-question identity, `accept_all_risk` sentinel, sidecar-form schema for populated open_questions) — all addressed but warrant a fresh-context verification pass before declaring fully implementation-ready. Banner and footer text confirmed identical in round 16.

**Suggested next step:** the v1-skill dogfood already happened — see `plans/fixs/v2-plan-fixes.md`. The dogfood-proper ran 16 rounds (44 findings, all addressed; convergence reached in round 16 per reviewer's explicit "no new medium/high-severity spec contradiction" assessment); rounds 17+ are post-dogfood text-staleness cleanup, not additional dogfood iterations. That dogfood validated the v2 design's three core convergence improvements directly against this plan. Recommended next step is now a **fresh-context verification pass** — spawn a separate Claude session and have it read the plan top-to-bottom looking for contradictions. The rounds-11-16 pattern showed that fresh-context Codex caught contradictions the in-loop Codex did not, so one additional fresh-context read is the highest-leverage remaining check before declaring fully implementation-ready. After that, proceed to Phase 1+2 implementation. (Round-18 finding 1: clarified that the "16 rounds, 44 findings" count describes the dogfood subset specifically, not the full file contents which now include post-dogfood cleanup rounds.)
