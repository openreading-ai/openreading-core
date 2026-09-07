# Routing and keys: name your backends, bring your own key

<sub>[Docs home](../README.md) · [← The command line](../cli/README.md) · [Strategies →](../strategies/README.md)</sub>

> **In one sentence.** You name the backends this deployment permits, in the order you want them
> tried, and `route` prints that chain before anything runs.

## What this gives you

A backend is one parser, whether a local library, a self-hosted model, or a hosted API, and you
have fifteen to choose from. Which of them may see your documents is a decision only you can make,
so you write it down once: the `policy:` block of your `openreading.yaml` holds `backends`, a list
of ids in preference order. `openreading route sample.pdf` prints the resulting chain without
running any backend at all.

A fallback is the next backend tried when one fails, and your list is the fallback chain. A
strategy is a named plan over backends, and it can only reach backends your list already permits.
Keys are read from your environment per request and go nowhere but the provider. You need
`sample.pdf` and an `openreading.yaml`, and no key.

## Mental model

Selection is a lookup with no inference in it. Three rules, in order:

1. **The backend you named.** `--backend pymupdf` is a chain of one.
2. **Else `policy.backends`, in written order.** That list is the chain.
3. **Else `pymupdf`.** It needs no key and no config, so a fresh clone reads a document with no
   setup at all.

An empty list permits nothing and refuses with `scope_denied`. An absent list is not an empty one:
absent means no restriction from that source.

Every source of an allow-list intersects and none widens. The file's list, a caller's own
argument, and the server's API-key scope are all restrictions, so what survives is what all of
them permit.

### Why there is no filter

The router used to have three stages: a compliance hard-filter reading a twelve-field profile off
every descriptor, a capability gate reading `input_formats` and five feature flags, and a scorer
weighing "quality" against price.

Every input to all three was a claim this package could not verify. Whether a vendor signs a
business associate agreement, trains on customer data, or retains a document for so many hours is
published on a page that changes without notice, and nothing here could detect drift. A wrong
entry did not fail loudly: it routed a document to a backend the operator believed was excluded,
and the run succeeded. The "quality" the scorer ranked by was this project's own P0/P1/P2 build
priority, and `optimize_for: latency` read no latency figure because no descriptor carried one.

So the rule this package now keeps: **core holds no fact it cannot verify. A constraint core
cannot check is a constraint core must not appear to enforce.**

A backend that cannot read a document refuses first-hand, and the chain moves to the next one.
Being wrong about a capability costs one round trip. Being wrong about a vendor claim cost a
silent exclusion nothing recovered from.

## Walkthrough

Build the sample and write a list:

```bash
uv run python -c "from openreading.testing.sample_pdf import build_sample_pdf; open('sample.pdf','wb').write(build_sample_pdf())"

cat > openreading.yaml <<'YAML'
version: 1
policy:
  backends: [pymupdf, tesseract]
YAML

uv run openreading route sample.pdf
```
```json
{
  "chosen": "pymupdf",
  "fallbacks": ["tesseract"],
  "dropped": {},
  "terminal_reason": null
}
```

Reorder the list and the chain reorders with it. `dropped` stays empty unless something you
declared excluded a backend, and then it names that backend with `scope_denied`.

`--run` executes the chain and puts the response in the plan's `result`:

```bash
uv run openreading route sample.pdf --run > plan.json
jq '.result.backend.id' plan.json
```

### Configured is not reachable

`openreading backends` reports whether this machine has the variables a backend declares. It does
not prove the vendor will answer:

```bash
uv run openreading backends
```

A backend in your list whose key is missing fails when it is reached, and the trail names the
variable. That is the intended behaviour: if you listed it, you meant it.

## Recipes

**Local only.** Name only backends that run on this machine:

```yaml
policy:
  backends: [pymupdf, tesseract]
```

**A hosted escalation behind a local first pass.** Order is preference:

```yaml
policy:
  backends: [pymupdf, aws-textract]
```

**Refuse everything.** An empty list is a real answer:

```yaml
policy:
  backends: []
```

## How it decides

`openreading.router.router.Router` resolves the three rules above and returns a `RoutePlan`:
`chosen`, `fallbacks`, `dropped`, `terminal_reason`. `openreading.router.executor.execute_plan`
walks the chain, and any failure from a backend, taxonomy or not, is appended to the trail before
the next one is tried.

`routing.fallback` on a request reorders within the resolved set. It never adds a backend the
chain did not already contain, so it reorders a restriction rather than widening one.

## Reference

- `uv run python -m pydoc openreading.router.router` prints the three rules and the law behind
  them.
- `uv run python -m pydoc openreading.router.executor` prints the chain, the skips, and
  `fallback_used`.
- `uv run python -m pydoc openreading.credentials` prints the precedence, `.env`, and the
  per-backend variables.
- `uv run python -m pydoc openreading.readiness` and `openreading.liveness` print the status
  ladder.
- `uv run openreading route --help`, `uv run openreading backends --help`, `.env.example`.

## Not built yet

- Server-side `deadline_ms` does not exist, so `/v1/parse` and `/v1/jobs` cannot raise the 120 s
  budget (`openreading.server`, "Timeouts").
- Webhook wait mode has no push path and degrades to polling (`openreading.router.driver`).
- `MISSING` stays `-` for `anthropic-claude` and `aws-textract` when unconfigured ([Not built
  yet](../adapters/README.md#not-built-yet)).

## See also

- [Docs home](../README.md)
- [The command line](../cli/README.md)
- [Strategies](../strategies/README.md) runs cascades and races over the same resolved chain.
- [The HTTP server](../server/README.md) puts the same resolution behind `POST /v1/parse`.
- [Backend adapters](../adapters/README.md) · [JSON Schemas](../schemas/README.md)

<sub>[Docs home](../README.md) · [← The command line](../cli/README.md) · [Strategies →](../strategies/README.md)</sub>
