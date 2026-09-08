# Resume a strategy run

<sub>[Docs home](../README.md) · [← Batch runs](../batch/README.md) · [The HTTP server →](../server/README.md)</sub>

> **In one sentence.** Set `OPENREADING_LEDGER` to record strategy steps and resume interrupted
> work without repeating completed backend calls.

## What this gives you

A ledger is an append-only record of a strategy run. It stores each attempted backend call and
its terminal result. `openreading resume <RUN_ID>` replays completed steps and executes work the
first process never reached.

The ledger contains document bytes and full responses in plaintext. OpenReading does not encrypt,
expire, sweep, or delete these files. Point `OPENREADING_LEDGER` at protected storage, then apply
the retention policy you use for other document data.

## Mental model

An armed strategy run writes three kinds of data:

- `<run_id>.header.json` identifies the request, strategy plan, and adapter descriptors.
- `<run_id>.jsonl` records attempted and terminal step results in append order.
- `blobs/<run_id>/<digest>.bin` stores documents and successful responses by SHA-256 digest.

Resume checks the header before execution. A changed plan or unsupported journal version refuses
resume. Each blob read verifies its recorded digest, so modified content cannot replay as the
recorded response.

## Walkthrough

Arm the ledger for a strategy run:

```console
$ export OPENREADING_LEDGER=.openreading
$ openreading parse examples/statement.pdf --strategy balanced
```

If the process reports an interrupted run, use the printed identifier:

```console
$ openreading resume 2eb0e9ca-8390-4c35-9279-855ff739e561
```

Recorded terminal steps produce no backend traffic. A step with no terminal record executes with
the original request projection and current credentials.

## Recipes

Set the variable only for commands you want recorded:

```console
$ OPENREADING_LEDGER=/srv/openreading-ledger \
    openreading parse examples/statement.pdf --strategy balanced
```

Unset the variable to run without a journal:

```console
$ unset OPENREADING_LEDGER
```

Inspect a run with ordinary JSON tools. The header is one JSON object, while the journal contains
one JSON object per line.

## How it decides

Only strategy execution arms the ledger. Direct backend calls and native batch submissions do not
write a resumable run. A batch can produce one strategy run per item when its items resolve to a
strategy.

The journal is a required dependency after arming. An unwritable ledger directory fails before
backend execution, because continuing would falsely imply that the run is resumable.

Secrets stay out of ledger metadata. Credential values, document passwords, and webhook URLs are
not persisted. Document bytes and source URLs use the blob store instead of the header.

## Reference

`OPENREADING_LEDGER`
: Directory used for headers, journals, and blobs. Unset or empty disables recording.

`openreading resume <RUN_ID>`
: Reconstructs and continues a recorded strategy run. It accepts no replacement request options.

Exit code `6`
: An armed strategy run was interrupted and may be resumed.

Exit code `3`
: Resume was refused, the run was unknown, or the ledger was unavailable.

The Python contracts live in `openreading.ledger.ports`. `Executor` dispatches a step, `Journal`
stores ordered results, and `BlobStore` stores verified content by digest.

## Not built yet

- Batch-level resume and durable HTTP job storage.
- A Python `ledger=` argument. Python callers use `OPENREADING_LEDGER` today.
- Distributed executor implementations and their conformance kit.
- A command that lists resumable run identifiers.

## See also

- [Docs home](../README.md)
- [Strategies](../strategies/README.md) for strategy execution and traces.
- [Batch runs](../batch/README.md) for per-item execution.
- [The HTTP server](../server/README.md) for the in-memory job API.
- [JSON Schemas](../schemas/README.md)

<sub>[Docs home](../README.md) · [← Batch runs](../batch/README.md) · [The HTTP server →](../server/README.md)</sub>
