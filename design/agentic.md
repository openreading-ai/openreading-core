> **Migrated from the private company repo on 2026-09-05, verbatim.** This record describes work
> `openreading-core` has not shipped. AGENTS.md keeps an unbuilt design record here, next to the
> code it proposes to change, so it is reviewed in the open. Delete this file in the pull request
> that finishes the work, moving its durable facts into the module docstrings.
>
> **Superseded in part by the removal set, 2026-09-07. Re-scope before implementing.** That set
> deleted the router this record was written against: the three stages are one lookup now (the
> backend the caller named, else `policy.backends` in written order, else `pymupdf`), and with
> them went the compliance filter, the capability gate, stage-3 scoring, `optimize_for`, `auto`,
> and every cost figure. Any passage here that routes on a vendor claim, scores candidates, or
> spells `backend: auto` describes machinery that no longer exists. The governing rule is now
> `openreading.router.router`: core holds no fact it cannot verify. Check `CHANGELOG.md` under
> Unreleased and the module docstrings before taking any line of this as current.

# Agentic readiness — Design ("Agentic", target v0.8)

**Status:** DESIGN, revision 2 — no code changes in this document's scope. A coding agent
executes the §12 work breakdown in order against the §13 test harness.
**Inputs:** five deep research briefs over the decider/MCP/contract/optionality/triage surfaces
(file:line-cited ground truth); three independent full design candidates (rails-first,
contract-first, agent-seat-first); a three-lens judge panel (optionality & safety, repo-fit &
executability, agent value & vocabulary) whose scores, grafts, and fatal-flaw list this
synthesis obeys; a legacy private user-guide triage table; and the superseded v0.7 cost
milestone.
**Revision 2** (2026-08-02, maintainer review): (a) the decider executor is **extracted to its
own milestone** — v0.9 "Decider", [`decider-executor.md`](decider-executor.md) — quarantining
the one real-spend path and restoring the repo's one-feature-per-version release grammar; §7
here is now a stub, its invariant AG-1 moves to DE-1, its tests T5/T6 move to DT2/DT3; (b) the
**MCP `compare` tool is grafted in** (revision 1 deferred it; the deferral's premise — "no
driving agent story" — was wrong, see §9.1a) with a new purity invariant AG-9; (c) phases
renumber to P0–P3. Section and test/invariant IDs otherwise keep revision-1 numbering — moved
items are stubbed in place, never renumbered.
**Date:** 2026-07-31 (revision 1), 2026-08-02 (revision 2).

---

## 0. Thesis and the one law

OpenReading's core thesis is that **agents, not humans, act on the output**. v0.7 already
delivers the machine-decidable envelope (closed `status.state`, honest error taxonomy, the
orchestration trace) and the decision rails (the decider contract, replay). What is missing is
the last mile *for the agent seat*: an agent-native surface (MCP — including the second-opinion
move, `compare`), contracts an agent can load instead of prose (warnings registry,
orchestration schema), and an executable verdict (triage). The fourth gap revision 1 named — a
shipped brain for in-run decisions (the wire executor) — is real but belongs to the
*strategy* feature, not the agent seat, and is the only piece that spends money; it is now its
own milestone (v0.9 "Decider", [`decider-executor.md`](decider-executor.md)). **This milestone
contains no path that can call an LLM.** Everything in it is deterministic and offline.

**The one law over everything here: the agentic layer is strictly optional and additive.**

- A bare `pip install openreading` (or a checkout without the new extras) behaves
  **byte-identically** to v0.7 on every existing response and decision path. No new import on
  the default path, no new file emitted, no changed response-envelope byte. **One deliberate,
  named additive exception**: failed batch items gain `error.class` (§5, AG-4) — legal because
  `batch-result.v0.1.json` is still `_UNRELEASED` (editable in the milestone it ships in), and
  its goldens regenerate in P0. T1's byte-identity suite carves out exactly this and nothing
  else.
- **Compliance is never widened via an agent surface.** No MCP tool argument or triage
  recommendation may re-admit a pruned backend or set a widening knob in-band. Widening
  attempts fail loudly, never silently (the rule: §8; its proof: §9.4).
- (Revision 1's third law — **no upgrade-triggered spend** — moves with the executor: it is
  DE-1 in [`decider-executor.md`](decider-executor.md). It is trivially satisfied here: v0.8
  constructs no ports, imports no SDK, and leaves every `decider_downgraded: unavailable`
  path byte-identical.)

Milestone identity: **v0.8 "Agentic"**, per the CHANGELOG codename sequence (Canon v0.5 →
Manifest v0.6 → Plain v0.7), built on branch `agentic`; **v0.9 "Decider"** follows it.
(ROADMAP §3's stale numbering is reconciled in P3, anchored to the CHANGELOG, not the other
way around.)

## 1. Scope

Three deliverables plus their shared seams:

| # | Deliverable | New surface |
|---|---|---|
| 1 | **Contracts** (§4–§5) | `warnings.v0.1.json`, `orchestration.v0.1.json`, `BatchItem.error.class`, error-class lift into `types/errors.py` |
| 2 | **Triage** (§6) | `openreading.triage()`, `openreading triage`, `triage-report.v0.1.json` |
| 3 | **MCP server** (§8–§9) | `openreading mcp` — six tools, three resource families (§9.2), `run-receipt.v0.1.json` |

Revision 1's third deliverable — the decider executor (§7) — is extracted to **v0.9
"Decider"** ([`decider-executor.md`](decider-executor.md)); §7 below is a stub explaining why.

Named deferrals (§10) are part of the design: what we deliberately do NOT do this milestone,
so the build loop never improvises.

## 2. Vocabulary budget

Per the house rule (user-facing surfaces get a tiny judgment-word vocabulary; internals stay
behind an advanced boundary), v0.8 adds exactly:

- **Four verdict words**: `accept` | `retry` | `escalate` | `reject` (§6.1). Nothing else
  user-facing is new vocabulary — MCP tools reuse existing verb names (`parse`, `parse_batch`,
  `route`, `backends`, `triage`, `compare`), and the compare tool's verdict words
  (`equivalent` / `mixed` / `divergent`, plus corpus `unpaired`) are the shipped v0.5/v0.6
  vocabulary, not new coinage.
- Internal/advanced names (rule ids, env knobs, schema families) live behind the docs'
  advanced boundary and never appear in a happy-path CLI line.

## 3. Optionality mechanics (how "strictly optional" is enforced, not promised)

- **Extras**: `mcp = ["mcp>=1.9"]` in `pyproject.toml` (the `server` precedent, pyproject.toml:41).
  `mcp` is a *surface* extra and therefore **not** in `[all]` (which bundles adapter runtime
  deps only — `server` is likewise excluded). No `agentic` umbrella extra: "agentic" is a mood,
  not a capability; the granular extras compose. This milestone adds **no other dependency**.
- **Dev-group mirror**: the `mcp` SDK joins `[dependency-groups].dev` exactly as fastapi does
  (pyproject.toml:67, "TestClient drives the ASGI app offline") so `make verify` always
  exercises the MCP surface offline. Forgetting this either breaks the coverage floor or
  invites an exclusion — the P3 checklist makes it explicit.
- **Import guards**: `openreading mcp` clones `cmd_serve`'s guard (cli/app.py):
  `try: import mcp / except ImportError: print("[mcp] mcp needs the mcp extra: pip install
  'openreading[mcp]'", file=sys.stderr); return 3`. Neither `mcp_server` nor `executor` nor the
  `anthropic` SDK is imported at module level of `cli/app.py`, `api.py`, or
  `openreading/__init__.py` — the T10 subprocess guardrail (tests/test_strategy_surface.py:46-62)
  is **extended** to assert a named-backend run imports none of `openreading.mcp_server`,
  `openreading.triage`, `anthropic`, or `mcp`.
- **Byte-identity proof**: the optionality suite (§13 T1) asserts a bare install's default
  paths are unchanged — no new imports on a named-backend run, no new files emitted, response
  bytes identical — with §0's single named `BatchItem.error.class` carve-out and nothing else.
  (The arm-switch byte-identity proof of revision 1 moves with the executor: DT1 in
  [`decider-executor.md`](decider-executor.md).)

## 4. Contract: the warnings registry (`warnings.v0.1.json`)

**Problem**: `warnings[]` codes are an open set by schema declaration; ~21 codes exist across
~28 call sites; docs/schema/response.md documents 4, one of them wrong-layer
(`unsupported_feature` is an error class, never a warning). Agents cannot switch on what they
cannot enumerate.

**Design** — the registry is a **genuine JSON Schema** (not a bare data file wearing the
schema filename — a judge fatal-flag), vendored at `src/openreading/schemas/warnings.v0.1.json`:

```jsonc
{
  "$id": ".../warnings.v0.1.json",
  "title": "Known response-warning codes (v0.1)",
  "type": "object",                      // validates ONE warning object {code,message,field}
  "properties": {
    "code":   { "enum": [ /* the 21 codes, verbatim */ ] },
    "message": { "type": "string" },
    "field":   { "type": "string" }
  },
  "x-codes": {                           // the machine-readable registry, one entry per enum member
    "<code>": { "layer": "platform|adapter|batch", "meaning": "<one line>" }
  }
}
```

- **Grandfathered verbatim, zero renames**: the 22 existing codes ship untouched (platform:
  `fallback_used`, `quality_escalated`, `quality_below_threshold`,
  `strategy_overrides_fallback`, `idempotent_replay`, `channel_unsupported`,
  `baa_tier_confirmed`, `backend_warning`; adapter: `confidence_unavailable`,
  `typed_fields_unsupported`,
  `blocks_unavailable`, `channel_not_produced_by_operation`, `page_attribution_unavailable`,
  `typed_fields_unverified`, `typed_fields_empty`, `tables_unsupported`, `output_truncated`,
  `bbox_space_approximate`, `channel_unavailable_in_mode`, `markdown_unavailable`,
  `text_unavailable`; batch: `items_skipped`). Codes are load-bearing in user `warning_code`
  gates (signals.py:350-351); consolidation of the near-synonym channel codes is a **named
  deferral** (§10).
- **The response schema stays OPEN.** `response.v0.3.json`'s warnings description gains only a
  pointer sentence to the registry family. Closure is *observational*: the wire never rejects
  an unknown code (forward tolerance, C6 deliver-or-warn escape valve for third-party
  adapters), but the repo cannot *emit* an unregistered one:
  - **Forward meta-test**: AST scan of `src/` covering **both warning constructors** — every
    literal first-arg of `add_warning(` AND every literal `code=` of `BatchWarning(` ∈ enum
    (`items_skipped` is emitted only via `BatchWarning` in batch/runner.py — an
    `add_warning`-only scan is blind to the whole batch channel). The one dynamic pass-through
    (engine.py forwards `compiled.warnings`; sole producer prune.py's
    `strategy_overrides_fallback`) is traced to its producer explicitly.
  - **Reverse meta-test**: every enum member appears at ≥1 call site across both constructors
    — no dead entries.
  - **Agreement meta-test**: `enum == sorted(x-codes.keys())` (the `experimental_fields`
    precedent, schemas/__init__.py:78 + test_schema_evolution.py:154-164).
  - **Docs binding**: docs/schema/response.md's table is regenerated/bound to the registry so
    it can never lie again (fixes the current 4-codes-one-wrong drift).
- **Runtime access**: `schemas.known_warning_codes() -> dict[str, dict[str, str]]` (loads the
  vendored file, mirrors `experimental_fields`). Exposed to agents via the MCP resource
  `openreading://schemas/warnings` (§9.2) — never via a new top-level CLI noun.
- Bookkeeping: joins `_UNRELEASED` in tests/test_schema_evolution.py; freezes at milestone end.

## 5. Contract: the orchestration schema (`orchestration.v0.1.json`) + error-class lift

**Problem**: the block agents decide from is `additionalProperties: true` — defined nowhere.

**Design** — a **standalone** vendored schema, NOT inlined into `response.v0.3.json`. v0.3 is
still legally editable, but welding strategies-owned vocabulary into the core response family
just before it byte-freezes is a layer inversion (the trace shape is built in
strategies/trace.py; the response envelope must stay ignorant of it). `response.v0.3.json`'s
orchestration property keeps `additionalProperties: true` and gains one pointer sentence.

`src/openreading/schemas/orchestration.v0.1.json`:

- Top level: required `[strategy, config_hash, chosen_backend, fallback_depth, outcome,
  attempts, decisions]`; `chosen_backend` nullable; `outcome: enum[ok, degraded]`;
  `additionalProperties: true` (young family; conditional keys `dropped`, `pages`, `merge`,
  `webhook_dropped`, `candidates` specified when present).
- `attempts[]`: required `[backend, category, node]`; `category` is a **string** whose
  description enumerates the 13 CATEGORIES (trace.py:15-31) plus the `error(<class>)` pattern
  — enum-plus-pattern is the real vocabulary (trace.py:63) and an enum would break on the
  pattern half. Optional: `duration_ms`, `cost_usd`, `cost_basis`, `detail`, `disagreement`,
  `gates[]` with required `[predicate, threshold, observed, fired]` + optional `skipped`,
  `source`.
- `decisions[]`: a **discriminated union on `point`** (a judge graft — it matches the two real
  record shapes exactly): the route record (`point: "route"`, `chosen: int|"default"`,
  `rules[]`) vs the decision-point record (decision_id, node_path, label, point, eligible,
  chosen, decider `enum[engine, llm, trace]`, config_hash, strategy, downgraded) — each branch
  `additionalProperties: true` so the llm-only record fields — the shipped `model_id` /
  `rationale` (decider.py:96-97, already merged by the engine), plus v0.9's additive
  `input_tokens`, `output_tokens`, `prompt_hash`
  ([`decider-executor.md`](decider-executor.md) §6) — land without a version cut.
- **Validation points** (default path untouched): (a) `schemas.validate_orchestration(block)`
  exists as an API; (b) the strategy-engine test suite validates every trace it produces;
  (c) a **golden fixture** `tests/golden/orchestration/v0.1.json` captured from a real engine
  run including gates, a dropped record, and one llm decision record (a judge graft) — the
  llm record is produced with an **injected fake `DeciderPort`** through `run_strategy`'s
  existing `decider_llm` seam (engine.py:218): no real port, no SDK, consistent with §0's
  no-LLM-path law;
  (d) `scripts/strategy_smoke.py` (already inside `make verify`) gains one
  `validate_orchestration` call — a free continuous drift-check. The server does NOT validate
  orchestration per-request (that would be a default-path behavior change; new 500s on a
  formerly-fine path).

**Error-class lift** (shared seam for triage + batch): the code sets at engine.py:74-79 move
**verbatim** to `types/errors.py` as public frozensets, plus two classifiers:

- `classify_error_class(exc) -> str | None` — a member of `{timeout, rate_limited,
  provider_error, auth, invalid_input, unsupported_feature}`, or **None iff `exc` is outside
  the adapter taxonomy** (not a RetryableError / TerminalError / UnsupportedFeatureError).
  The None domain is deliberate: batch/runner's blanket `except Exception` must NOT stamp a
  `TypeError` as retryable `provider_error` and feed permanently-broken items into the retry
  manifest. `strategies/engine.classify_error` becomes `classify_error_class(exc) or
  "provider_error"` — behavior-identical (its fallthrough today), existing tests prove it.
- `classify_error_code(code: str | None, backend_code: str | None) -> str | None` — the same
  taxonomy from *strings*, built on the lifted frozensets. This is triage's classification
  source for a **response** envelope's `status.error` (which carries only
  code/message/backend_code — no class field; §6.2's `state_failed_*` rules are
  unimplementable without it, and triage may not import strategies).

Then:

- `batch/runner.py` stamps each failed item's error dict with `"class":
  classify_error_class(exc)` when non-None (`error.code`/`message` unchanged — today they are
  raw backend codes or Python class names, useless for retry logic).
- `schemas/batch-result.v0.1.json` (in `_UNRELEASED`, legally editable) gains the OPTIONAL
  additive `BatchItem.error.class` enum; `types/batch.py` mirrors; batch goldens regenerate.

## 6. Triage — the executable verdict

The user-guide §6 table becomes code. **Pure function, zero I/O, zero knobs, deterministic.**

### 6.1 Vocabulary and precedence (settled once, here)

Four verdicts. Precedence when multiple rules fire: **`reject` > `escalate` > `retry` >
`accept`** (two of three candidates; the judge panel required this settled in-doc). Meanings:

| Verdict | Means |
|---|---|
| `accept` | consume the result |
| `retry` | the same ask can succeed unchanged — transient failure or still processing |
| `escalate` | do not auto-consume; a materially stronger process or an operator action is required (includes auth/credential provisioning) |
| `reject` | the input itself is the problem; retrying or escalating cannot help |

`auth` / missing-credentials map to **escalate** (an operator action fixes it); `invalid_input`
and `unsupported_feature` map to **reject** (majority position; the agent-seat candidate's
`reject`-for-auth lost — a bad key is not a bad document).

### 6.2 Rules

A closed `RULES` tuple in `src/openreading/triage.py`; ids are the advanced-boundary names.
Verdict-bearing rules: `state_failed_retryable → retry`, `state_processing → retry`,
`state_failed_auth → escalate`, `state_failed_unclassified → escalate`, `state_partial →
escalate`, `outcome_degraded → escalate` (fires on warning `quality_below_threshold` too),
`state_failed_invalid_input → reject`, `state_failed_unsupported → reject`. Reason-only rules
(never flip a verdict): `fallback_used`, `quality_escalated`, `confidence_unavailable`,
`already_escalated` (fires when `orchestration.fallback_depth > 0` or attempts contain
`quality_escalated`/`review_escalated`; `observed` = the attempted backend list — **facts from
the envelope, never recommendations**), `unknown_warning_codes` (codes ∉ the §4 registry —
surfaced honestly, never verdict-flipping: an unknown code must not scare an agent off a good
result, and must not be silently invisible either).

**Every reason is auditable**: `{rule: <closed id>, path: <JSON pointer into the input>,
observed: <value>}` — the GateRecord precedent (trace.py:46-57). ALL fired rules return in
fixed table order; the verdict is the most severe fired rule. **`accept` is the verdict when
no verdict-bearing rule fires** — reason-only rules never change it. For a failed response
envelope, the `state_failed_*` rule is selected by
`classify_error_code(status.error.code, status.error.backend_code)` (§5); no match ⇒
`state_failed_unclassified → escalate`.

**Triage never names a backend** (law): a "next backend" recommendation could name one the
run's compliance pruning dropped — coaching the agent around the hard filter.

### 6.3 Input domain and output

One polymorphic entrypoint `openreading.triage(doc: dict) -> dict`, sniffing three shapes
(the `cmd_explain` precedent, cli/app.py):

1. **response** envelope → single verdict + reasons.
2. **batch-result** envelope → per-item verdicts + `rollup {accept, retry, escalate, reject}`
   + **`retry: {sources: [...]}`** — for each failed-with-retryable-class item, the
   re-ingestible identity **`item.source.path or item.source.url`** (BatchItem.source is a
   SourceRef, not a string; both fields are nullable — an item with neither is **excluded**
   from `retry.sources` and surfaced in a reason, so the count mismatch is auditable per
   AG-3). Directly feedable to `parse` / `run_batch` / the MCP `parse_batch` tool (a judge
   graft: the single most agent-valuable action in the §6 table). Item classes come from
   `error.class` (§5); older envelopes fall back to `classify_error_code(error.code, None)`.
3. **error body** — the server/tool error shape `{"error": {"category", ...}}` (a judge
   graft: single-doc failures raise and never produce an envelope; without this the failure
   path has no verdict). The complete category table (categories from `api.error_info` plus
   the server's inline `bad_request` / `unknown_backend` validation helpers,
   server/app.py:204-211 — `error_info` itself covers only the exception-mapped subset):

   | category | verdict | note |
   |---|---|---|
   | `retryable_exhausted` | `retry` | transient; the same ask can succeed |
   | `plan_exhausted` | `escalate` | every rung failed — a stronger process, not a re-run |
   | `compliance_refused` | `escalate` | policy is an operator condition, and triage must not coach widening — a forbidding policy is not a bad document (the auth precedent) |
   | `unknown_strategy` | `reject` | the ask as posed cannot succeed |
   | `unsupported_feature` | `reject` | |
   | `bad_request` | `reject` | |
   | `unknown_backend` | `reject` | |
   | `terminal` | by `backend_code`: `missing_credentials`/`auth_rejected` → `escalate`; `doc_too_large` → `reject`; else `escalate` | |

Output conforms to vendored `src/openreading/schemas/triage-report.v0.1.json` (joins
`_UNRELEASED`, golden fixture, validator registration, `types/` mirror — the batch-intake
Phase-0 checklist): `{verdict, input: {kind: enum[response, batch-result, error],
schema_version}, reasons[], items?[], rollup?, retry?}` plus `triage_version`.

### 6.4 Placement and surfaces

- `src/openreading/triage.py` is a **leaf**: imports only `openreading.types`,
  `openreading.schemas`, stdlib — enforced by an H6a-style text-scan test (no
  `openreading.strategies` / `.comparison` / `.router` text) plus the extended T10 subprocess
  guardrail. It never re-probes document content (no signals.py import — verdicts must not
  depend on optional deps or disagree with the engine's own at-run gating).
- Surfaces: Python export `openreading.triage` (joins `__all__` lazily); CLI
  `openreading triage <file> [--strict]` — **exit 0 whenever a judgment is produced**
  (judgment is data; a judge fatal-flag killed verdict-coded exits 6/7/8), `--strict` exits 4
  when the verdict is not `accept`; 2 usage / 5 unrecognized input. MCP: §9.
- HTTP `/v1/triage` is a named deferral (§10) — server surface growth is not this milestone's
  risk to take.

## 7. The decider executor — extracted to v0.9 "Decider"

Revision 1 designed the wire executor here (arm-switch, client seam, ports, honest
accounting, wiring). Revision 2 moves that design **verbatim** into
[`decider-executor.md`](decider-executor.md) (its §2–§6), as its own milestone. Why:

- **It is the only piece that spends money.** Everything else in v0.8 is deterministic and
  offline; the executor carries the no-upgrade-triggered-spend law (now DE-1), a live test
  lane, and an SDK coupling. Quarantining it means the spend-safety review happens once, on a
  small diff, not buried inside a five-schema milestone.
- **It is independent by this document's own dependency graph.** Nothing in contracts, triage,
  or MCP needs a constructed port; the §14 acceptance loop closes without it. It serves the
  *strategy* feature, not the agent seat.
- **One feature per version is the house release grammar** (v0.3 Strategies, v0.4 Compare,
  v0.5 Canon, v0.6 Manifest, v0.7 Plain). Four deliverables under one number broke it.

What v0.8 must guarantee about this seam: nothing in this milestone constructs a port,
imports the `anthropic` SDK, or alters any `decider_downgraded: unavailable` path — T1's
import guardrail keeps `anthropic` in its forbidden set. The MCP `backends` tool ships
**without** a decider-posture line (observability-before-spend ships in the same milestone as
the spend: DE-2); its output gains the posture field additively in v0.9.

## 8. MCP server — posture

**Trust posture: the HTTP server's (D7), not the CLI's.** The MCP client is an autonomous
agent holding the operator's keys at one remove:

- **Credentials**: process env / `.env` only, never tool args (the broker design everywhere).
- **Policy**: `parse`/`parse_batch`/`route` accept a `policy` arg whitelisted to exactly the
  **five narrowing compliance keys** (`require_baa`, `no_train_on_data`, `data_region`,
  `require_local`, `max_retention`) **plus the two narrowing-safe routing hints the policy
  vocabulary already carries** (`optimize_for`, `doc_type_hint` — api._apply_policy accepts
  them today; rejecting legitimate keys would make the tool contract lie about the dict it
  wraps). Anything else — above all the widening knobs `allow_unverified_compliance` /
  `train_optout_confirmed` / `baa_tier_confirmed` — ⇒ **loud tool error** `bad_request` naming the rejected key,
  never silently dropped (a judge fatal-flag: silent divergence between requested and
  effective policy on a compliance surface is a trust hazard even when the outcome is safe).
  `_COMPLIANCE_KEYS` is promoted to a public constant in api.py (today it is a private tuple
  duplicated in api.py and strategies/validate.py — P3 checklist). Widening knobs come only
  from process env via the new shared `api.router_config_from_env(env)` (lifted from
  server/app.py:114-126; the HTTP server refactors onto it behavior-identically).
- **Config**: server posture — `load_config(None, allow_cwd=False)` once at startup;
  `OPENREADING_CONFIG` is the only implicit source; snapshot, never re-sniffed. A stray
  `openreading.yaml` in whatever cwd an MCP client launches from must never change behavior
  (loader.py's own stated law; two candidates' cwd-sniffing died in judging). The loaded
  config path (or null) is visible in the `backends` tool.
- **Transport**: stdio only in v0.8. Filesystem-path acceptance (`parse` source, `compare`
  subjects/truth) and the runs store are safe **because** the transport is local stdio; any
  future non-stdio transport is a new design that must revisit both before shipping — a
  tripwire, recorded here so surface growth cannot inherit the path trust silently.
  **Stdout purity is structural**: one `_guard(fn)` wrapper applies
  `contextlib.redirect_stdout(sys.stderr)` around EVERY tool body **and every resource
  handler** (resources execute on the same stdio loop; local backends demonstrably print to
  stdout — pymupdf's layout advisory — and one forgotten redirect corrupts JSON-RPC framing)
  and maps exceptions through the new pure `api.error_info(exc)` (the dict half of
  server/app.py:140-179, lifted; the server keeps the HTTP-status half — `error_info` also
  learns to map `CompareInputError` → `bad_request` for the compare tool). A test registers
  a chatty fake backend and asserts frame purity for a tool call AND a resource read;
  another asserts every registered tool is sync `def` (the asyncio.run-cannot-nest
  constraint).
- Packaging: module `src/openreading/mcp_server/` (avoids shadowing the SDK's import name),
  `build_server() -> FastMCP`; CLI verb `openreading mcp`.

## 9. MCP server — surface

### 9.1 Six tools, one return shape each

| Tool | Construction | Returns |
|---|---|---|
| `parse(source, backend='auto', strategy=None, operation=None, policy=None, mime_type=None)` | `api.run_request` with the startup snapshot (strategy config + `router_config_from_env`) | **run-receipt** (below) |
| `parse_batch(sources: list, backend='auto', strategy=None, policy=None, jobs=1, max_items=200)` | `api.run_batch` **extended in P2 with explicit injection params** (`strategy_config=`, `router_config=`, threaded down to `run_request` per item); the MCP tool passes the startup snapshot | **run-receipt** (`kind: batch-result`) — closes the loop: `triage.retry.sources` feeds straight back into `parse_batch.sources` (judge-mandated graft) |
| `route(source, policy=None, operation=None)` | **mirrors `/v1/route`'s construction** — `Router(build_registry(), router_config_from_env(env)).route(req)` — NOT `api.route` | the RoutePlan rendering `{chosen, fallbacks[], dropped{}, terminal_reason}` — the cheap pre-flight compliance check |
| `backends()` | readiness inspection | rows `{slug, type, extra_installed, creds_found, creds_missing, ready}` (server/app.py:129-137 shape) + `{config_path}` (the decider-posture field is a v0.9 additive delta, DE-2) |
| `triage(path_or_doc)` | `openreading.triage` | triage-report v0.1 |
| `compare(subjects: list, baseline=None, truth=None)` | §9.1a — pure over stored/passed envelopes; **never executes a backend** (AG-9) | **run-receipt** (`kind: comparison-report` or `corpus-report`) |

On `parse_batch.max_items`: an agent-settable argument is a courtesy default, not a rail —
the agent can raise it. The operator-side ceiling is the env clamp
`OPENREADING_MCP_MAX_ITEMS`: when set, the tool argument may lower the effective limit but
never exceed it; an over-clamp request is a loud `bad_request` naming both numbers (T7).
Unset, the tool default (200) applies and docs/mcp.md says plainly that real spend ceilings
are the operator's provider key/plan limits.

Two of these carry a **critique-mandated correction** over the naive wrapping: `api.run_batch`
today re-resolves config per item via `api.run` → `load_config(allow_cwd=True)` — wrapped
as-is, the MCP tool would cwd-sniff `openreading.yaml` per item, violating §8's own posture;
and `api.route` builds its RouterConfig from the policy dict alone, so env widening knobs
could never reach it — `parse` and `route` would silently diverge on backend eligibility in a
widened deployment, misreporting the "pre-flight compliance check". Both fixes are P2 work
items with T7 tests (a stray `openreading.yaml` in the server's cwd changes nothing;
`route`'s dropped-set equals `parse`'s eligibility under widened env).

### 9.1a The compare tool (revision-2 graft)

Revision 1 deferred an MCP compare tool as "surface growth without a driving agent story".
The premise was wrong — the agent story is already in the shipped docs, twice over:

- **Escalate needs a landing.** Triage's `escalate` verdict means "a materially stronger
  process is required" (§6.1). For an unattended agent, the canonical materially-stronger
  process this product offers *is* a second opinion: parse with another backend, compare,
  branch on the verdict. Without a compare tool, `escalate` dead-ends at a human — the exact
  outcome the thesis (§0) exists to avoid.
- **Backend selection over a corpus** is the user-guide §6 playbook's own move
  (corpus-verdict `divergent` → route through a `compare:` strategy), and the README's 3×3
  now claims the compare×agent cell. The tool makes the claim true.

**Signature**: `compare(subjects: list[str], baseline: str | None = None,
truth: str | None = None)`. Each `subjects`/`baseline` entry is either

- **`run:<run_id>`** — the reserved-prefix identity (the `backend.id: "strategy:"` precedent):
  resolved through the store's **in-memory `{run_id: path}` index recorded at write time**
  (§9.3) — resolution never joins a client string into a filesystem path, so traversal is
  structurally impossible; the id grammar (`re.fullmatch` of 16 lowercase hex chars) is
  defense-in-depth, not the mechanism (T7). A `run:` id not present in the index is a
  **loud `bad_request` naming the id** — never a silent fallthrough to path interpretation.
  A `run:` id that resolves to a non-envelope kind (a stored comparison/corpus report) is a
  **loud `bad_request` naming the id and its kind** — compare subjects must be response or
  batch-result envelopes.
- a **filesystem path** to an envelope JSON (the `parse(source)` trust precedent — the MCP
  client already passes local paths). A path that is missing, unreadable, or not an envelope
  is a loud `bad_request` naming the path.

`truth` is a path to an evals `expected` golden (goldens are curated files, never store
artifacts) — missing/unreadable is a loud `bad_request`; `truth` is **single-doc mode only**
(corpus + `truth` ⇒ loud `bad_request`, matching the CLI, which has no corpus-truth mode).
This mirrors `openreading.compare(inputs, baseline=, truth=)` (comparison/__init__.py:20)
exactly — the tool contract must not lie about the surface it wraps.

**Mode sniff mirrors the CLI verb** (`cmd_compare`, cli/app.py — the only corpus call site
today — `openreading.compare()` itself has no corpus branch): all subjects batch-results
(`is_batch_envelope`, comparison/corpus.py:22-29) ⇒ corpus compare via the same
`corpus_pairs`/`corpus_report_dict` helpers; all single responses ⇒ `openreading.compare()`;
mixed ⇒ loud `bad_request` reusing the CLI's message body — `cmd_compare` tags every stderr
line with the file's `[<command>]` convention (so here, `[compare] `); the tool's message
strips that tag, then takes everything after the `compare: ` prefix that follows it.

**No fan-out mode — by law (AG-9).** The CLI's `compare doc.pdf --backends a,b` sugar is
deliberately NOT mirrored: the tool never executes a backend, so spend and compliance
attribution stay entirely on the `parse`/`parse_batch` calls the agent explicitly made, and
the tool needs no `policy` argument at all. The agent composes:
`parse(doc, backend=A)` → `parse(doc, backend=B)` → `compare(["run:<A>", "run:<B>"])`.
Primitives, not sugar — an agent does not need its wrists held.

**Return discipline**: the report is validated, written to the store like any envelope
(`run_id` = sha256 prefix of report bytes; fetchable via `openreading://runs/{run_id}`), and
the tool returns a run-receipt (§9.3) whose embedded summary is **verbatim from the report**:
the `headline` object for a comparison-report, the `rollup` object (plus per-run subject
labels) for a corpus-report. Verbatim is load-bearing (AG-3 posture): measured real
sizes are ~8 KB for a 2-way single-doc report (findings-dominated, unbounded) and **742 KB
for a real 4-document corpus report** — inline-full fails the same test that killed it for
envelopes (§9.3), while the headline/rollup an agent branches on is under 1 KB.

### 9.2 Resources

- `openreading://guide` — the agent briefing. Served from a **packaged copy**
  `src/openreading/mcp_server/guide.md` bound to `docs/llms.md` by an invariant-strings
  meta-test (a judge fatal-flag killed serving docs/llms.md directly: `docs/` is not in the
  wheel — hatch packages only `src/openreading` + schema force-includes).
- `openreading://schemas/{family}` — every vendored schema byte-verbatim (response,
  batch-result, corpus-report, warnings, orchestration, triage-report, run-receipt…), with a
  byte-identity round-trip test. The contracts themselves are the agent's audit surface, at
  zero token cost until read.
- `openreading://runs/{run_id}` — the full stored artifact for a receipt's `run_id`
  (response, batch-result, comparison-report, or corpus-report), served from
  `$OPENREADING_MCP_OUT` (the third family; a judge graft). This is the fallback channel for
  sandboxed clients that cannot read `envelope_path` from the filesystem: the receipt carries
  the verdict inline, and the complete artifact stays fetchable through MCP itself. It is also
  what makes the compare tool composable: the ids on parse receipts are exactly the
  `run:<run_id>` subjects compare accepts (§9.1a). Per-process, non-persistent — the /v1/jobs
  store precedent, documented as such.

Every tool description carries one pointer line ("read `openreading://guide` before
interpreting envelopes") — a description convention, not a resource; resources are cheap, but
many clients never auto-read them.

### 9.3 The run-receipt (payload discipline)

Real envelopes measure **170 KB–21 MB** (measured: 2-page pymupdf 174 KB; pulse batch
20.9 MB); real comparison reports measure ~8 KB (2-way single-doc) to **742 KB** (4-document
corpus). Inline-full is not a serious option for either (a judge fatal-flag killed uncapped
inline). Every `parse` / `parse_batch` / `compare` call:

1. Runs the pipeline; **validates the artifact** against its family
   (`schemas.validate_response` / `validate_batch_result` / the comparison-report and
   corpus-report validators).
2. Writes the full artifact to the store. Default: a fresh
   `tempfile.mkdtemp(prefix="openreading-mcp-")` per process — mode `0700`, unpredictable,
   race-free; a predictable `<tempdir>/openreading-mcp/<pid>/` path was rejected (classic
   shared-/tmp pre-creation/symlink attack against directories that hold full document
   text). A judge fatal-flag separately killed a cwd default: run artifacts never land in
   the user's project uninvited. `$OPENREADING_MCP_OUT` is the explicit opt-in override:
   created `0700` if absent; a pre-existing dir that is not owned by the current user, or is
   group/world-writable, is a **loud startup refusal** (T7 asserts the mode). Filename:
   `<run_id>.json` where `run_id` = the **first 16 hex chars** of the sha256 of the artifact
   bytes — deterministic, the key into `openreading://runs/{run_id}`, and recorded in the
   per-process `{run_id: path}` index that all `run:`/resource resolution goes through
   (§9.1a). Writes are idempotent by construction (same bytes ⇒ same id ⇒ dedupe); an id
   collision with different bytes is a loud internal error naming the id, never a silent
   overwrite.
3. Returns the **run-receipt**, vendored as `src/openreading/schemas/run-receipt.v0.1.json`
   (a judge graft — a documented-not-frozen receipt was the mcp brief's named risk):
   `{kind: enum[response, batch-result, comparison-report, corpus-report], run_id,
   envelope_path, bytes,` then per kind: for `response`/`batch-result` — `triage:
   <triage-report v0.1, embedded so sandboxed clients that cannot read the path still get the
   verdict>` (required for these kinds), `backend?`, `text_preview?: <first 2000 chars of
   document.text, verbatim truncation — never a summary, never fabricated>, items?: {total,
   succeeded, failed, skipped}`; for `comparison-report` — `headline: <the report's headline
   object, verbatim>`; for `corpus-report` — `rollup: <the report's rollup object, verbatim>`
   + `subjects: [<run labels>]`. Kind-conditional requireds are expressed with standard
   if/then blocks inside the one schema — one receipt family, never a receipt-per-kind
   (vocabulary budget, §2). Embedding mechanics: the receipt schema declares `triage` /
   `headline` / `rollup` as **open objects** — no vendored schema cross-references another
   file today (all `$ref`s are internal `#/$defs`), and inventing a cross-file resolver is
   not this milestone's job; conformance of every embedded block is enforced by T7's
   `embedded == the same object recomputed/read standalone` assertions (AG-3), not by `$ref`.

### 9.4 Testing

In-process, offline, inside `make verify`: the SDK's in-memory client session
(`pytest.importorskip("mcp")`; asyncio_mode=auto is already configured) drives tool listing,
schemas, receipt shape, envelope-on-disk validity, **the widening-policy loud rejection**,
stdout purity (tool call AND resource read), resource byte-identity, the store-dir mode
assertion (`0700`, loud refusal of an unsafe pre-existing `$OPENREADING_MCP_OUT`), the
`OPENREADING_MCP_MAX_ITEMS` clamp loud-rejection. Compare-tool coverage (T7): compare over
two stored `run:` ids (single and corpus); a missing `run:` id fails loudly naming the id; a
wrong-kind `run:` id (a stored report) fails loudly naming the id and kind; mixed
batch/single subjects fail with the CLI's message body; corpus + `truth` fails loudly;
corpus receipt `rollup` equals the on-disk report's rollup byte-for-byte; **the
no-execution proof** — a poisoned registry entry whose adapter constructor fails the test if
touched proves the compare tool never builds a backend (AG-9); id resolution is
index-only — `re.fullmatch('[0-9a-f]{16}', id)` as defense-in-depth, no path join anywhere
on the resolution path; a compare report round-trips byte-identical through
`openreading://runs/{run_id}`. A `make mcp-smoke` target spawns the real stdio
subprocess — outside `verify`, the serve-smoke precedent.

## 10. Named deferrals (deliberate, documented, not improvised)

| Deferral | Why now is wrong |
|---|---|
| `review_escalated` warning split (stop folding into `quality_escalated` in `_summarize_trail`) | re-labeling an existing warning mutates strategy-envelope semantics and silently un-fires user `warning_code` gates — additive-law violation (judge fatal-flag). Consumers read `attempts[].category` today, which works. Revisit behind a response-family version cut. |
| Warning-code consolidation (the 4 near-synonym channel codes) | renames break user gates; grandfather now, consolidate behind a v0.2 registry with aliases. |
| HTTP `/v1/triage`, jobs-over-MCP | surface growth without a driving agent story this milestone; the primitives (CLI/Python/receipt-embedded triage) cover the loop. |
| Corpus mode in `openreading.compare()` and `POST /v1/compare` | today corpus is CLI-only (`cmd_compare`, cli/app.py, is the sole call site; the library raises on batch envelopes). The MCP tool reaches corpus through the same helpers (§9.1a), so the agent story is served; promoting corpus into the library/HTTP surfaces is feature-parity growth for the *API* column, on its own clock. |
| MCP progress notifications for long batches | receipt-on-completion is v0.8; wire `on_progress` when a client demonstrably consumes it. |

(Revision 1's rows for `send_document_content` wiring and non-Anthropic executors move with
the executor to [`decider-executor.md`](decider-executor.md) §7. Revision 1's "MCP `compare`
tool" row is **withdrawn** — grafted in as §9.1a; its premise is refuted there.)

## 11. Invariants (CHANGELOG-named, tested)

- **AG-1 — moved.** Revision 1's "no upgrade-triggered spend" ships with the executor as
  **DE-1** ([`decider-executor.md`](decider-executor.md) §8). The id is retired here, never
  reused; v0.8's byte-identity/import half of it lives in T1.
- **AG-2 Narrowing-only in-band policy**: agent-facing surfaces accept only narrowing
  compliance keys; widening attempts fail loudly (T7).
- **AG-3 Receipts never fabricate**: previews are verbatim truncations; triage reasons cite
  JSON pointers into the envelope they judged; embedded triage equals standalone triage of the
  same envelope; embedded compare headline/rollup equal the on-disk report's (T7).
- **AG-4 Machine-readable batch failures**: failed items carry `error.class` from the closed
  taxonomy (T3).
- **AG-5 The trace and warnings are contracts**: orchestration + warnings registry are
  vendored schemas under the freeze machinery; the repo cannot emit an unregistered warning
  code (T2/T3).
- **AG-6 Triage never names a backend** (T4).
- **AG-7 Stdio stdout purity is structural** — every tool body and every resource handler is
  guarded (T7).
- **AG-8 Verdicts are data**: producing a judgment exits 0; `--strict` is the opt-in shell
  signal (T4).
- **AG-9 The compare tool never runs a backend**: no fan-out mode, no policy argument, no
  registry construction on the compare path — spend can only ever originate from an explicit
  `parse`/`parse_batch` call (T7's poisoned-registry proof).

## 12. Work breakdown (execute in order; one phase, one commit, `make verify` green each)

**P0 — Contracts: warnings registry, orchestration schema, error-class lift, batch
error.class.** Land `warnings.v0.1.json` (genuine schema + x-codes) + `known_warning_codes()`;
`orchestration.v0.1.json` + `validate_orchestration()` + the golden trace fixture (its llm
record via an injected fake `DeciderPort`, §5 validation point (c) — no SDK) +
`strategy_smoke` hook; both files into `_UNRELEASED`. Move the code sets into
`types/errors.py` (`classify_error_class` with its None domain + `classify_error_code`,
§5), refactor `engine.classify_error` onto them via the `or "provider_error"` shim;
`BatchItem.error.class` (schema + types mirror + runner stamp + regenerated goldens);
response.v0.3 description-only pointers. Tests: T2, T3.

**P1 — Triage leaf + CLI verb + `triage-report.v0.1.json`.** `triage.py` (RULES, polymorphic
sniff incl. error bodies, batch rollup + retry manifest), schema + golden + types mirror +
validator registration, lazy `__all__` export, `cmd_triage` (exit 0/2/5, `--strict`→4);
**restructure** user-guide §6's table (split the merged terminal row — auth vs invalid_input
land on different verdicts — and add rows for `state_processing`, `already_escalated`,
`unknown_warning_codes`), then annotate every row with its rule id. Tests: T4; T1's guardrail
gains the no-triage-import assertion.

**P2 — MCP server (six tools).** `mcp` extra + dev-group mirror; `api.router_config_from_env`
+ `api.error_info` lifts with server/app.py refactored onto them (test_server.py proves
identical); `_COMPLIANCE_KEYS` promoted public; **`api.run_batch` gains the
`strategy_config=`/`router_config=` injection params** (§9.1 — the cwd-sniff blocker fix);
`mcp_server/` (build_server, `_guard`, six tools incl. `compare` per §9.1a with the `run:`
resolver and the CLI-mirroring corpus sniff, the three resource families incl.
`runs/{run_id}`, `OPENREADING_MCP_OUT` artifact writing, packaged guide.md + binding test);
`run-receipt.v0.1.json` with the four-kind conditional shape (+`_UNRELEASED`, goldens for a
response receipt AND a corpus-compare receipt); `cmd_mcp` import-guard; `make mcp-smoke`.
Tests: T7 (incl. the stray-cwd-yaml, route-vs-parse-eligibility, and the §9.4 compare block —
poisoned-registry AG-9 proof, run-id grammar, rollup verbatim-equality); T8's keyed MCP
parse round-trip lands in the live lane (skips cleanly without keys); T1's
no-mcp_server-import assertion lands here; smoke outside verify.

**P3 — Milestone close.** docs: `docs/mcp.md` (launch config, tools/resources, receipt,
posture, key-spending warning per the cmd_serve precedent — spend via parse tools only,
compare spends nothing); user-guide §6 "now executable" + the escalate→second-opinion
playbook (§9.1a's loop); llms.md MCP + triage + compare-tool sections; **fix the
contradictory exit-code tables** (docs/cli.md vs docs/llms.md, exit 4 overloaded three ways —
reconcile against cli/app.py, document all meanings); ADDING_ADAPTERS.md gains the
registered-warning-code rule; PENDING.md gains the §10 deferral entries; DECISIONS.md entries
(milestone split + AG-1→DE-1 move, MCP posture, standalone schemas,
triage-never-names-backends, receipt discipline, compare-graft + AG-9); CHANGELOG v0.8
"Agentic" with AG-2…AG-9 and the AG-1 moved-note; ROADMAP reconciliation note covering both
v0.8 and v0.9. Tests: T9. Acceptance: §14.

## 13. Test harness

- **T1 Optionality/byte-identity** (lands incrementally: P1 triage-import, P2 mcp-imports):
  default-path byte-identity — with §0's single named carve-out (`BatchItem.error.class`);
  extended T10 subprocess guardrail (no mcp_server/triage/anthropic/mcp imports on a
  named-backend run — `anthropic` stays in the forbidden set even though the executor is now
  v0.9's, per §7); bare-install import safety for every new module. (The arm-switch-off
  byte-identity proof is DT1 in [`decider-executor.md`](decider-executor.md).)
- **T2 Warnings registry**: forward AST closure over BOTH constructors (`add_warning(` +
  `BatchWarning(code=…)`, incl. the prune.py dynamic producer), reverse no-dead-entries,
  enum==x-codes agreement, docs-table binding.
- **T3 Orchestration + batch**: golden validates; every engine-suite trace validates;
  strategy_smoke hook; `error.class` stamped for each of the six classes (fault-injected);
  schema bookkeeping (`_UNRELEASED` membership) for every new family.
- **T4 Triage**: one fixture per rule id; precedence table; determinism (same dict twice ⇒
  identical output); polymorphic sniff incl. error bodies; batch rollup + retry.sources
  round-trip into intake; leaf import text-scan; AG-6; CLI exits incl. `--strict`.
- **T5 — moved.** Executor-offline tests are DT2 in
  [`decider-executor.md`](decider-executor.md). Id retired here, never reused.
- **T6 — moved.** Executor arming/compliance tests are DT3 in
  [`decider-executor.md`](decider-executor.md). Id retired here, never reused.
- **T7 MCP in-memory**: tool list/schemas; receipt shape + artifact-on-disk validates;
  embedded-triage == standalone-triage; widening-policy loud rejection; a stray
  `openreading.yaml` in the server's cwd changes nothing (parse AND parse_batch);
  route-vs-parse eligibility parity under widened env; stdout purity with a chatty fake
  backend; all-tools-sync assertion; resource byte-identity (incl. `runs/{run_id}`); guide
  binding test; **the §9.4 compare block**: compare over two stored `run:` ids (single and
  corpus), loud miss on an absent `run:` id, mixed-subject loud error, receipt
  headline/rollup verbatim-equality vs the on-disk report, the poisoned-registry AG-9
  no-execution proof, run-id grammar (`^[0-9a-f]+$`, traversal impossible), compare-report
  resource round-trip.
- **T8 Live lane** (`make verify-live`, skips cleanly): optional keyed MCP parse round-trip.
  (The keyed decider round-trip is DT4 in [`decider-executor.md`](decider-executor.md).)
- **T9 Docs-truth** (P3): exit-code tables consistent with cli/app.py; after P1's table
  restructure, RULES ids and user-guide §6 annotations are in bijection, with named exempt
  rows (corpus-verdict and HTTP-status rows are not triage rules); docs/mcp.md's tool table
  names exactly the six registered tools and the compare row states AG-9 (never runs a
  backend).

## 14. Acceptance

`make verify` green on a clean clone with no keys (all new suites offline, coverage floor
respected, never lowered); the T1 byte-identity proofs pass; `make mcp-smoke` and
`make serve-smoke` pass locally; `make verify-live` skips cleanly without keys; every new
schema family is in `_UNRELEASED` with a golden; CHANGELOG carries AG-2…AG-9 plus the
AG-1→DE-1 moved-note; the §10 deferrals appear in PENDING.md. The agent loop closes end to
end — including the second opinion: `backends` → `parse(doc, A)` → receipt verdict → on
`escalate`: `parse(doc, B)` → `compare(["run:<A>", "run:<B>"])` → branch on the verdict; on
a batch: `parse_batch` retry of `triage.retry.sources`; then `openreading://schemas/*`
audit — with a human nowhere in it.
