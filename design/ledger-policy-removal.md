# Design record: the ledger keeps no policy about the caller's data

Status: proposed, not built. Decision taken by Akshay on 2026-09-07.

Measured against `e27ad9d`. Sibling records: [`compliance-removal.md`](compliance-removal.md),
[`format-agnostic-intake.md`](format-agnostic-intake.md). Same principle, third surface.

## What the ledger writes today

Arming it with `OPENREADING_LEDGER=<dir>` and running one 117 KB PDF through a strategy produces
356 KB across six files:

```
<run_id>.header.json                the run header
<run_id>.jsonl                      the journal, one line per step
keys/<run_id>.key                   32-byte AES-256-GCM key, mode 0600
blobs/<run_id>/e69d9ae….bin         225,680 on disk
blobs/<run_id>/f5c675c….bin         117,365 on disk
retention/<run_id>.json             {"expires_epoch_ms": …, "zdr": false}
```

Decrypting both blobs identifies them:

```
225,680 on disk ->  225,651 plaintext   JSON, the RESPONSE BODY      (inline.py:438)
117,365 on disk ->  117,336 plaintext   PDF, the INPUT DOCUMENT      (api.py:680, api.py:692)
```

117,336 is `examples/schedule_a_2024.pdf` byte for byte. The ledger holds a copy of the caller's
document and a copy of the full response.

## What goes

### 1. Retention

`ledger/retention.py`, 192 lines, plus the reaper, the expiry stamp, `PayloadExpired`,
`OPENREADING_LEDGER_RETENTION_HOURS`, `OPENREADING_RETENTION_SWEEP_S` and the server's sweep loop.
247 source references, 155 test lines.

The feature is a destructor. Its only job is deleting the caller's data on a timer, on the
caller's own disk. Demonstrated by expiring a stamp and running the reaper:

```
reaped runs: ['587faaa6…']   keys left: []   blobs left: []
$ openreading resume 587faaa6…
[resume] run '587faaa6…': key destroyed, payload unrecoverable
```

Three reasons it does not belong here.

**It is not core's call.** This runs on the operator's laptop or their own server. Retention of
files on that disk is their policy, expressed with their tools. A 24-hour default that silently
ends the ability to resume is core making a decision about someone else's data.

**The number is not ours to know.** `retention.py:66` derives the ceiling from
`min(max_retention_hours)` across hosted descriptors, and the stamp carries a `zdr` flag lifted
from `zdr_flag`. Both come from the vendor table [`compliance-removal`](compliance-removal.md)
deletes. An unverifiable claim about a vendor's servers currently decides when files on the
caller's laptop are shredded.

**The default was never chosen.** `DEFAULT_RETENTION_HOURS = 24.0` carries the comment
`# placeholder`.

### 2. Encryption at rest

AES-256-GCM in `localfs.py` (46 crypto lines) and `cryptography>=50.0`, which is a **base**
dependency imported by that one file and nothing else in the package.

Locally it protects against very little. `keys/<run_id>.key` sits in the same directory tree as
`blobs/<run_id>/*.bin`, so anyone who can read the ciphertext can read the key. The package
docstring states the threat model it actually serves (`__init__.py:445`): the key directory is
one "operators must exclude from whatever backs up" the root. That is a single narrow scenario,
bought with a native dependency on every install.

Disk encryption is the operator's job and their OS already does it better. Encryption earns its
keep when the ledger runs on storage the operator does not control, which is the commercial
product's problem, not core's.

### 3. The `zdr` storage branch

`inline.py:265` skips `blobs.put` entirely for a backend whose descriptor carries `zdr_flag`. That
is vendor compliance data changing what core writes to the caller's disk. It goes with the table.
After this, every armed run stores its blobs, uniformly, with no vendor-conditional behaviour.

## What stays

The ledger's actual job: the journal, the header, the blobs, replay and resume. It writes where
`OPENREADING_LEDGER` points and then leaves what it wrote alone. Nothing expires, nothing sweeps,
nothing is encrypted. `rm -rf` is the retention policy, and it belongs to the person whose disk it
is.

Blobs stay because resume needs them. The response blob is what lets a resumed run skip a step
that already succeeded; the document blob is what lets a run resume when the source file has moved
or been deleted.

## The one thing worth adding, not removing

`OPENREADING_LEDGER=/some/dir` currently means "copy every document I process, and every full
response, into this directory". The variable's name does not say that, and the comment at
`inline.py:428` records that the response blob ignores whatever `include_backend_raw`,
`typed_fields` and `image` settings the request asked for. So the ledger can hold data the caller
deliberately excluded from their own response.

Removing encryption and retention makes that disclosure more important, not less. The honest fix
is not a knob, it is a sentence: the arming path should say what it is about to start copying, and
the env var's documentation should lead with it rather than with the journal. That is the one
change here that actually protects a user, and it costs nothing.

Whether blobs should be opt-in is a separate question this record does not decide. Resume is the
feature that needs them, so the two are one decision, and nobody has asked for resume-without-
blobs yet.

## Migration

- `OPENREADING_LEDGER_RETENTION_HOURS` and `OPENREADING_RETENTION_SWEEP_S` are removed. A
  deployment setting either gets no behaviour rather than a silent change of behaviour, so both
  should be named in the `CHANGELOG.md` `Removed` section with the one-line replacement: delete
  the directory on whatever schedule you already use.
- `PayloadExpired` leaves the error taxonomy. Nothing raises it once the reaper is gone.
- Existing ledger roots written by an older build contain encrypted blobs this build cannot read.
  Reading them is not worth a compatibility path: the ledger is arming-gated, pre-release, and
  the content is reproducible by re-running. Say so in the changelog rather than writing a
  decryptor.
- `cryptography` leaves `[project.dependencies]`. Check `scripts/check_extras_parity.py` and the
  lock after removing it.

## Test strategy

- A test asserting an armed run writes no `retention/` directory and no `keys/` directory.
- A test reading a blob straight off disk with `open()` and finding the document bytes, which is
  the plainest possible statement of what the ledger stores and would have made this obvious
  earlier.
- A test asserting `grep -rn "cryptography" src/` returns nothing.
- The 155 existing retention test lines are deleted, not migrated.

## Order

Independent of the other two records, except that `retention.py`'s use of `max_retention_hours`
and `zdr_flag` means it must land with or before
[`compliance-removal`](compliance-removal.md) PR 2, which deletes those fields. Landing this
first is simpler: it removes two of the compliance table's consumers before the table goes.
