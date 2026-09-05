> **Migrated from the private company repo on 2026-09-05, verbatim.** This record describes work
> `openreading-core` has not shipped. AGENTS.md keeps an unbuilt design record here, next to the
> code it proposes to change, so it is reviewed in the open. It was written before the monorepo
> split and predates protocol v2, adapter-descriptor v0.7 and the current `.env.example` posture,
> so verify every fact against this repository's code before implementing from it. Delete this
> file in the pull request that finishes the work, moving its durable facts into the module
> docstrings.

# The decider executor — Design ("Decider", target v0.9)

**Status:** DESIGN — no code changes in this document's scope. A coding agent executes the
§10 work breakdown in order against the §9 test harness.
**Provenance:** extracted **verbatim in technical content** from `agentic.md` revision 1 §7
(same research briefs, candidates, and judge panel — see that document's header). Extraction
rationale: `agentic.md` §7 (stub). This milestone is the **only path in the product that can
spend money on an LLM call**; it ships alone so the spend-safety review happens on a small
diff.
**Sequencing:** builds after v0.8 "Agentic" lands, on branch `agentic` (or its successor).
Two cross-milestone touches, both named: (a) the MCP `backends` tool gains the additive
decider-posture field (§3); (b) once armed, the v0.8 MCP `parse`/`parse_batch` tools become
an LLM-spend path when a strategy hits a decision point — so DE-P2 updates docs/mcp.md's
key-spending warning, and DT5 checks it. Everything else is independent of the
MCP/triage/contract surfaces.
**Date:** 2026-08-02 (extracted from revision 1 of 2026-07-31).

---

## 0. Thesis and the one law

v0.7 shipped the decision rails: three decision points (gate-band review, `decide:` nodes,
`pick: best` judging) with compliance invisible and un-overridable, a closed downgrade
taxonomy, `decisions[]` records with deterministic ids, and offline `replay`. But the
`DeciderPort` is a Protocol nobody implements: nothing constructs a port on any shipped
surface, so **every decision point today resolves to its engine default with
`decider_downgraded: unavailable`**. This milestone ships the brain: a wire executor behind
the existing ports.

**The one law: no upgrade-triggered spend (DE-1).** A deployment that today resolves
`decider_downgraded: unavailable` — even one with `OPENREADING_LLM_DECIDER` set, a
`decider:` block, `ANTHROPIC_API_KEY` in the env, and the anthropic extra installed (which
`[all]` installs) — must keep resolving `unavailable` after upgrading, until the operator
flips the new explicit arm-switch (§2). This is non-negotiable; two candidate designs died
on it in the original judge panel.

Everything else inherits the standing laws: strictly optional and additive (bare install
byte-identical), compliance never widened (the decider backend is itself
compliance-checked via `Router.check_eligible`; a compliance-dropped decider backend
downgrades `compliance` with the port never called), and the v0.7 cost posture (report
honestly, label the basis, never enforce).

## 1. Scope

One deliverable: `strategies/executor.py` — `AnthropicDecider`/`AnthropicJudge` behind the
existing ports, armed by a third key — plus the engine carve-outs that make its failure
modes honest, posture observability, and honest per-call accounting. No new extra: the
executor rides `anthropic-claude = ["anthropic>=0.39"]` (pyproject.toml:39).

## 2. The arm-switch (DE-1, the load-bearing decision)

`build_ports` constructs ports **only when `OPENREADING_DECIDER_EXECUTOR` is truthy**
(`'1'|'true'|'yes'|'on'`, decider.py's existing token grammar) **in addition to** the existing
two keys (decider block + `OPENREADING_LLM_DECIDER`). Unset ⇒ ports `None` ⇒
`decider_downgraded: unavailable`, byte-for-byte today. Rationale: auto-wiring on the existing
two-key gate flips every pre-armed deployment into real spend on upgrade — the judge panel
unanimously fatal-flagged it in both candidates that chose it. The switch is deployment
posture (env, never a wire field). A future release may fold it back after an announced flip;
that is a DECISIONS entry, not code.

## 3. Observability before spend (DE-2)

An all-judges graft, with split semantics: the **env half** of the gate (third key ×
`OPENREADING_LLM_DECIDER` × SDK importability × `ANTHROPIC_API_KEY` presence) is global —
that is what the MCP `backends` tool reports, as the **additive** field this milestone adds
to its v0.8 output (`decider: armed (anthropic-claude, claude-haiku-4-5)` / `decider:
dormant (executor not armed)` / `decider: dormant (no OPENREADING_LLM_DECIDER)`). The
**config half** (which strategies carry a `decider:` block, per-strategy judge backends)
belongs to `openreading strategy validate`, which has the config in hand. Posture
derivation is **pure inspection** — env membership + `importlib.util.find_spec` — and must
never construct a `DeciderClient` or port (no `anthropic` import on the backends path).

The dormant reasons are a **closed string set** (vocabulary budget — the build agent coins
nothing): `decider: armed (<backend>, <model>)` / `decider: dormant (executor not armed)` /
`decider: dormant (no OPENREADING_LLM_DECIDER)` / `decider: dormant (anthropic SDK not
installed)` / `decider: dormant (no ANTHROPIC_API_KEY)` — one per false leg of the env
gate, first false leg in that order wins. **One env source**: the MCP server evaluates both
the `backends` posture line and `build_ports` arming from live `os.environ` (never a
startup snapshot — posture must agree with what a request would actually do in the same
process; DT3 asserts the pairing per permutation).

## 4. Module and client seam

`src/openreading/strategies/executor.py` — strategies/ placement is T10-safe (api.py imports
it only inside `_run_strategy_request`, where strategies is already imported). It owns a
**self-owned** `DeciderClient` Protocol whose `create(...)` carries a **per-call `timeout`
kwarg** — verified to be the only seam that makes `min(configured, budget_remaining)`
enforceable, where `budget_remaining` is what remains of the strategy run's configured time
budget at call time, passed per call by the engine (the adapter's shared `AnthropicClient`
Protocol has no timeout param; `_RealAnthropicClient.__init__` takes only `api_key` — reuse
plans in two candidates died on this). `_RealClient` lazy-imports the `anthropic` SDK
(`pragma: no cover`), honors ambient `ANTHROPIC_API_KEY`. Pricing stays single-source:
`_MODEL_PRICE` in `adapters/anthropic_claude/adapter.py` is promoted to public
`MODEL_PRICE`; the executor imports it — and that module (like `executor.py` itself) must
remain SDK-import-free at module level, since `executor.py` is imported on **every**
strategy run once wired (DT1's subprocess guard is the tripwire).

## 5. Ports

`AnthropicDecider(DeciderPort)` and `AnthropicJudge(JudgePort)` share `_AnthropicPortBase`
(client, model, timeout, cost math). Both carry a **`bound_backend`** advisory attribute
checked in `resolve_decider_status` / `resolve_judge_status` (a judge graft, replacing
rails' coarser all-or-nothing judge-uniformity refusal): a node whose resolved judge backend
≠ the port's bound backend resolves `unavailable` for that node — the consulted model is
provably the compliance-checked slug, per node, honestly.

- Model: default **`claude-haiku-4-5`** (a 280-char strict-tool answer does not need opus;
  opus-class latency under a 5s timeout means chronic `timeout` downgrades), overridden by
  `OPENREADING_DECIDER_MODEL` — never `ANTHROPIC_MODEL`, which belongs to the parse adapter.
- `max_tokens=256`; `tool_choice {"type": "any"}` (normative per decider.md; yields a natural
  refusal signal). The decider sends `build_decider_tool(dp.candidates)` **verbatim** — the
  action enum IS the candidate list. A new `build_judge_tool()` (DE-P0) mirrors it for A/B.
- Prompts are built ONLY from `DecisionPoint`/`JudgeCandidate` fields — the H6 masking canary
  holds by construction (masked fields never reach the port input).
- Mapping: no `tool_use` block or refusal stop ⇒ `action None` (engine traces `refusal`);
  missing/invalid action or truncation ⇒ raise `ValueError` (engine traces `malformed`);
  SDK exception class name `APITimeoutError` ⇒ raise **`DeciderTimeout`** — a new
  platform-owned exception in `strategies/decider.py`. Engine carve-outs, per path:
  `engine._decide` already has a blanket except (every port exception → `malformed`) and
  gains one additive `except DeciderTimeout` arm before it, tracing `timeout`. **The judge
  path has NO except today** — `_judge_pair`'s `compare()` calls are bare, so any port
  exception would kill the whole run; DE-P0 adds a try/except around the pair's two compare
  calls catching `DeciderTimeout` (→ `timeout`) and `Exception` (→ `malformed`), resolving
  that pair via the deterministic engine comparator (`_tie_break_pair`) and recording the
  downgrade reason on the judge decision record. This finally makes the closed taxonomy's
  `timeout` member reachable and honest on both paths. (Engine-side asyncio wrapping was
  rejected: it collides with the coordinated-virtual-clock determinism make verify depends
  on.) `_duration_ms` relocates from engine.py to `decider.py` as public
  `parse_duration_ms` (pure move; engine re-imports).
- Degradation is always soft: SDK not installed or `ANTHROPIC_API_KEY` absent ⇒ `build_ports`
  returns `None` ⇒ `unavailable`. Never an ImportError, never a raise on the run path.

## 6. Honest accounting on verdicts (DE-3)

Additive optional fields on both verdict dataclasses: `input_tokens`, `output_tokens`,
`cost_basis`; `DecisionVerdict` also gains `prompt_hash` (sha256 of canonical JSON of
`{model, system, messages, tools, tool_choice}` — decider.md promises it; today it is
absent). For the avoidance of drift with agentic.md §5's decisions[] branch: the complete
llm-extras set on a decision record is `model_id` and `rationale` (**shipped v0.7 verdict
fields**, decider.py:96-97, already merged by the engine) plus this milestone's additive
`input_tokens`, `output_tokens`, `prompt_hash`.
The **executor** computes `cost_usd` from the public dated `MODEL_PRICE` table and sets
`cost_basis="estimated"`; the **engine passes `cost_basis` through** from the verdict to the
`decider_call`/`judge_call` Attempt — the engine never stamps a basis it cannot know (a judge
fatal-flag: engine-side stamping would lie for fake or future billed-cost ports and silently
change existing fake-port test output). Token counts + prompt_hash merge into the decision
record's llm-only extras. This is the v0.7 cost posture exactly: report honestly, label the
basis, never enforce.

Construction and wiring: `build_ports(compiled, env) -> tuple[DeciderPort | None,
JudgePort | None]` lives in `executor.py`; `api._run_strategy_request` calls it (lazily,
inside the function) and passes the ports to `run_strategy` — closing the injection gap
(nothing passes `decider_llm` today, so CLI/server can never reach LLM mode).
`send_document_content` remains **reserved, not wired** (named deferral §7): the executor
never sees the flag; `strategies validate` warns when a config sets it true.

## 7. Named deferrals

| Deferral | Why now is wrong |
|---|---|
| `send_document_content` wiring | needs a DecisionPoint/engine change; executor-side-only would be dishonest. `strategies validate` warns it is reserved. |
| Non-Anthropic executors | the port seam is the extension point; a second executor earns a registry when it exists. |
| Folding the arm-switch back into the two-key gate | only after an announced flip, as a DECISIONS entry (§2). |

## 8. Invariants (CHANGELOG-named, tested)

- **DE-1 No upgrade-triggered spend** (was AG-1 in agentic.md revision 1): executor
  construction requires the explicit third-key arm-switch; arm-switch-off is byte-identical —
  including every `decisions[]` record — to the pre-executor `unavailable` output (DT1).
- **DE-2 Observability before spend**: posture is derivable (MCP `backends` field +
  `strategy validate` lines) by pure inspection, before any key is spent (DT3).
- **DE-3 Honest accounting**: `cost_basis` is stamped by the party that knows it (the
  executor), never the engine; tokens and `prompt_hash` land in the decision record (DT2).

## 9. Test harness

- **DT1 Arm-switch byte-identity** (was agentic revision 1's T1 executor half). The proof
  needs a fixed right-hand side or it proves nothing — a same-code A/B (third-key-unset vs
  no-keys) passes trivially even when unarmed output regressed on both sides. So: **DE-P0's
  first commit-item, before any executor code exists, captures the baseline golden**
  `tests/golden/orchestration/unavailable-baseline.json` — the full envelope including every
  `decisions[]` record from a deterministic two-key-armed, port-less strategy run at branch
  tip. DT1 then diffs the armed-but-third-key-unset run of the NEW code against that
  **committed** golden, byte-for-byte. Import guards, both layers: the T10 subprocess
  guardrail still proves no `anthropic` import on a named-backend run, and a **new
  subprocess strategy-run guard** proves (a) a two-key-armed, third-key-unset strategy run
  finishes with `decider_downgraded: unavailable` and `anthropic` ∉ `sys.modules` (executor
  is imported on every strategy run once wired — laziness is load-bearing, §4), and (b)
  with the SDK masked/absent, the same run still completes soft — never an ImportError
  (§5's degradation law).
- **DT2 Executor offline** (was T5): fake DeciderClient replaying captured-shape Messages
  responses — strict tool echo, refusal path, malformed path, truncation, `APITimeoutError`
  ⇒ `DeciderTimeout` ⇒ traced `timeout`; cost/tokens/prompt_hash land in the decision
  record; H6 masking canary appears nowhere in built prompts.
- **DT3 Arming + compliance** (was T6): 8-way key permutation matrix (two keys × third key ×
  SDK/key presence) each resolving to the exact downgrade reason or llm mode;
  compliance-dropped decider backend ⇒ `compliance`, port never called; `bound_backend`
  mismatch ⇒ per-node `unavailable`; posture lines match reality per permutation.
- **DT4 Live lane** (`make verify-live`, skips cleanly; was T8's decider half): one keyed
  decider round-trip (gate_band, real haiku call, verdict recorded).
- **DT5 Docs-truth**: decider.md posture lines match `strategy validate` output; docs/mcp.md
  backends-tool description gains the posture field AND its key-spending warning now states
  that an armed decider adds per-decision-point LLM spend inside `parse`/`parse_batch`
  strategy runs (the v0.8 wording "spend via parse tools only, compare spends nothing" must
  not survive unqualified).

## 10. Work breakdown (execute in order; one phase, one commit, `make verify` green each)

**DE-P0 — Baseline + rails additions + engine carve-outs.** FIRST, before any executor
code: capture and commit DT1's baseline golden
(`tests/golden/orchestration/unavailable-baseline.json`, §9) from the pre-executor code.
Then the additive decider.py changes
(`DeciderTimeout`, `build_judge_tool`, `parse_duration_ms` relocation, verdict fields,
`bound_backend` checks in the two resolve functions); engine carve-outs per §5 (the
`_decide` timeout arm, the NEW judge-path try/except with `_tie_break_pair` resolution,
extras merge, `cost_basis` pass-through); `MODEL_PRICE` promotion. Tests: DT2's engine-side
halves, DT1's guardrail extension.

**DE-P1 — Executor + arm-switch + wiring.** `executor.py` (client seam, two ports,
`build_ports` with the third-key gate + soft degradation); `api._run_strategy_request`
wiring; posture line in `strategy validate`; the MCP `backends` posture field (additive);
`send_document_content` reserved-warning. Tests: DT1 byte-identity, DT2 remainder, DT3.

**DE-P2 — Milestone close.** decider.md Status flip + the three-key story +
prompt_hash/timeout/model; user-guide §6 decider paragraph updated (wire executor ships,
armed by three keys); llms.md decider posture note; **docs/mcp.md key-spending warning
updated per DT5** (armed decider ⇒ parse tools can additionally spend decider-LLM money;
`backends` shows the posture); PENDING.md deferrals; DECISIONS.md entries (arm-switch, DE-1
lineage from AG-1); CHANGELOG v0.9 "Decider" with DE-1…DE-3. Tests: DT4 (live, skipping),
DT5. Acceptance: §11.

## 11. Acceptance

`make verify` green on a clean clone with no keys; DT1 byte-identity passes; `make
verify-live` skips cleanly without keys and passes with `ANTHROPIC_API_KEY` (one real
haiku-class decision recorded with tokens, `cost_basis: estimated`, and `prompt_hash`);
arm-switch-off deployments are provably unchanged; CHANGELOG carries DE-1…DE-3; every §7
deferral appears in PENDING.md.
