# Policy Enforcement Follow-up

**Status:** DESIGN, revision 1. This work is not built.
**Product intent:** `product/specs/policy-enforcement-follow-up.product-spec.md`, revision 1.
**Reviewed against:** `feat/policy-one-yaml` at `5978a04`, 2026-09-06.

The one-file change gives policy one authoring format and one schema. This record closes six
enforcement gaps found after implementation. It does not restore any removed policy container.

## 1. Laws

- **PF1. Neither source can weaken the other.** File and request constraints form an
  intersection. A backend must satisfy both sources before receiving document content.
- **PF2. Public calls are safe in isolation.** `compile_strategy`, `config.apply`, and calibration
  refuse or enforce a policy without relying on an earlier loader call.
- **PF3. Resume compares policy provenance.** Removing or changing the live file cannot be hidden
  by policy values persisted inside the ledger request echo.
- **PF4. One batch uses one snapshot.** A Python batch reads and validates its configuration once.
  Every item uses that immutable snapshot.
- **PF5. Preferences never masquerade as constraints.** `optimize_for` has an explicit precedence
  rule and participates in replay identity when it can change execution order.
- **PF6. Evaluation honors the policy.** Leaderboard cases and publisher resume identities use
  the same complete configuration that normal execution uses.

## 2. Findings that require this work

### 2.1 Scalar constraints currently use request precedence

`openreading.config.union_compliance` ORs boolean constraints correctly. It chooses the request
value for `data_region` and `max_retention` whenever the request supplies one.

That rule can weaken the deployment file. A file containing `max_retention: zero` and a request
containing `max_retention: 48h` produce an effective ceiling of 48 hours. A backend retaining data
for 24 hours then survives, although it fails the file alone.

A file containing `data_region: eu` and a request containing `data_region: us` similarly produces
`us`. A US-only hosted backend survives a deployment policy that required Europe.

### 2.2 Public strategy compilation ignores `StrategyConfig.policy`

`StrategyConfig` and `compile_strategy` are public exports. A caller may build a
`StrategyConfig` directly and pass it to `compile_strategy`, as repository tests already do.

Compilation now assumes another surface already applied the block. A direct call with
`policy: {require_local: true}` can compile a hosted backend as eligible. The same bypass applies
to malformed widening values passed directly to `config.apply` or `config.router_config`.

### 2.3 Resume persists the effective request without policy provenance

The API applies the file before `_arm_ledger` writes `slim_request`. The stored request therefore
contains file-derived compliance and routing values without recording which source supplied them.

On resume, the live file is applied to that effective request. Removing `require_local` from the
file leaves the stored `require_local` value in place. The recomputed `config_hash` stays equal,
so the change is not detected.

`optimize_for` has the same provenance problem. It changes the ordered routing result, but the
current configuration hash does not include the effective preference or ordered eligible ids.

### 2.4 Python batch configuration is not one snapshot

`run_batch` loads the file before intake. Its platform path then calls `run(config=config)` for
each item, which loads the same path again. A two-item batch currently calls the loader three
times.

The file can change between items. Items from one returned batch can therefore run under
different policies, and a later parse error can appear after earlier documents reached backends.

### 2.5 Leaderboard drops the five compliance constraints

`cmd_leaderboard` loads the file but passes only a derived `RouterConfig` into
`run_leaderboard`. Each dataset request therefore lacks `require_baa`, `no_train_on_data`,
`data_region`, `require_local`, and `max_retention` from the file.

The evaluator still checks `req.compliance`, but the command never places the file constraints
there. Attestation keys take effect while the requirements they qualify do not.

### 2.6 Publisher pipeline identity hashes the path, not its content

The ParseBench and ExtractBench bridge uses the literal config path in its pipeline name. Editing
policy or strategy content at that path leaves the name unchanged.

The publisher uses that name for artifact directories and resume. A benchmark can therefore
reuse results produced under an earlier policy while labeling them as the current run.

## 3. Constraint algebra

`config.apply` combines each field according to its domain. The implementation must not use one
generic request-first rule.

| Field | Combination |
|---|---|
| `require_baa`, `no_train_on_data`, `require_local` | Boolean OR. |
| `max_retention` | Parse both ceilings and keep the lower duration. An unparseable non-empty value fails closed before dispatch. |
| `data_region` | Keep the one supplied value. Equal values remain equal. Different non-empty values raise a typed compliance conflict before dispatch. |
| `optimize_for` | The explicit request value wins. The file fills an absent request value. |
| `allow_unverified_compliance` | Boolean OR, because either trusted deployment source may attest that unknown vendor facts are allowed. |
| `train_optout_confirmed`, `baa_tier_confirmed` | Set union of confirmed backend ids. |

The region conflict is not represented as a fabricated region string. `data_region` has no
ordering, and the request schema cannot express two simultaneous regions. A typed refusal keeps
the intersection closed without teaching the router a fake value.

The retention parser in `openreading.router.compliance` remains the meaning authority. The merge
must reuse it, or move it to a lower shared module, so comparison and enforcement cannot differ.

## 4. Validated policy boundary

Add a strict `Policy` Pydantic model beside `StrategyConfig`. Its fields mirror the closed policy
object in `strategy-config.v0.3.json`, and its model configuration forbids extras and coercion.

`LoadedFile.policy` and `StrategyConfig.policy` become `Policy | None`. Schema validation remains
the file contract. The typed model protects Python callers who construct objects without reading
a file.

`config.apply` accepts `Policy | None`. If compatibility requires accepting a mapping, it must
strictly validate that mapping into `Policy` before reading any field. `router_config` follows the
same rule and removes its inaccurate claim that an unvalidated dictionary was validated.

`compile_strategy` applies `config.policy` defensively before routing. Applying an already-applied
policy is idempotent. This guard keeps the public function safe when called without `api.run`.

`calibrate_strategy` uses the same typed policy and the same application function. It must not
reconstruct compliance or router configuration with its own field list.

Parity tests compare the policy schema, `Policy.model_fields`, request compliance fields, and
`RouterConfig` attestation fields. A new policy key cannot reach only one representation.

## 5. Ledger and resume

The ledger must distinguish the caller request from its effective request. Store the original
request echo before file policy application, or store equivalent source provenance explicitly.

At resume, reconstruct the original request and apply the current file exactly once. Compilation
then compares the resulting execution identity with the original header.

The hard identity includes:

- effective compliance;
- effective `RouterConfig` attestations;
- effective `optimize_for`;
- ordered eligible backend ids;
- the compiled strategy tree and existing descriptor digests.

Changing file formatting alone must not refuse resume. Changing a policy value that cannot alter
the effective request may retain the same identity. Removing a file value that supplied an
effective constraint or preference must change identity and raise `HeaderMismatch`.

If the header schema needs a new field, evolve its schema and journal version deliberately. Old
headers must either migrate deterministically or fail with a clear version mismatch.

## 6. Batch snapshot

`run_batch` loads and validates `config` before intake. It passes a private loaded snapshot through
the native and platform paths. No item calls discovery, reads the path, or validates the file
again.

The public `run` signature remains unchanged. A private helper may accept `LoadedFile`, or the
batch may build all effective requests from the snapshot before worker submission.

The snapshot covers policy, defaults, strategies, limits, and decider configuration. Treating only
the policy as fixed would still let strategy behavior change during one batch.

## 7. Evaluation surfaces

Leaderboard receives the validated `Policy`, not only `RouterConfig`. Each case request passes
through `config.apply` before `evals.runner.run_case` evaluates the selected backend.

Policy refusal remains a scored case error under the existing evaluation contract. The command
must not submit the adapter first or remove an ineligible backend from only the final report.

Publisher pipeline identity includes a canonical digest of the complete parsed configuration.
Equivalent mappings have one digest despite YAML formatting and key order. Any meaningful policy,
strategy, limits, defaults, or decider change produces a different identity.

The publisher loads this snapshot before registering its pipeline. Execution receives the same
snapshot, so the name and the backend calls cannot describe different configuration content.

## 8. Required tests

1. A file ceiling of zero plus a request ceiling of 48 hours keeps zero and drops a 24-hour
   backend.
2. A file ceiling of 48 hours plus a request ceiling of zero keeps zero.
3. Conflicting file and request regions refuse before adapter construction and name both values.
4. Equal regions and single-source regions retain current behavior.
5. Direct `compile_strategy` enforces a valid `StrategyConfig.policy` against a hosted backend.
6. Direct model construction rejects an unknown key, a quoted boolean, and a bare attestation
   string.
7. Direct `config.apply` and `router_config` cannot turn the string `"false"` into permission.
8. Removing a file-supplied compliance constraint between arm and resume raises `HeaderMismatch`.
9. Removing or changing file-supplied `optimize_for` between arm and resume raises
   `HeaderMismatch` when routing order changes.
10. Changing file formatting without changing parsed content does not refuse resume.
11. A two-item platform batch invokes file discovery and validation once.
12. Mutating the file after batch preflight cannot change later item policy or strategy behavior.
13. Native and platform batches produce the same policy verdict from the same loaded snapshot.
14. Leaderboard applies all five file compliance constraints to every dataset request.
15. Leaderboard attestations qualify those applied requirements and never operate alone by
   accident.
16. Publisher pipeline identity stays stable for equivalent parsed configuration content.
17. Publisher pipeline identity changes when policy, strategy, limits, defaults, or decider
   content changes at the same path.

Every regression test follows the repository's red-green requirement. Break the fix, observe the
test fail, restore it, and run `make verify`.

## 9. Delivery

Ship this as one compatibility-preserving pull request. Update the `openreading.config`,
`openreading.api`, `openreading.strategies`, and ledger module docstrings where each contract is
read. Update the changelog for observable conflict and resume behavior.

Delete this design record and its product spec in the implementation pull request. Move durable
facts into module docstrings before deletion, as required by `AGENTS.md`.
