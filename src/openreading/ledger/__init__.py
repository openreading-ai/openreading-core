"""Record strategy runs for replay and resume.

The ledger sits between strategy execution and adapters. Each backend call writes an attempted
record, then writes its terminal result. A resumed run replays terminal results and executes the
first step without one. Recorded failures are reconstructed through the normal adapter taxonomy.

Set ``OPENREADING_LEDGER`` to a writable directory to arm recording. An armed strategy run writes
``<run_id>.header.json``, ``<run_id>.jsonl``, and content-addressed files below
``blobs/<run_id>/``. Direct backend runs and native batch submissions do not arm the ledger.
Unset or empty ``OPENREADING_LEDGER`` disables recording. Resume then fails as unavailable.

Ledger files contain documents and full responses in plaintext. The package does not encrypt,
expire, sweep, or delete them. The operator chooses a protected directory and applies their own
retention policy. Blob reads verify the recorded SHA-256 digest, so modified content cannot replay
as the recorded result.

The header pins the request projection, compiled plan, schema version, and adapter descriptors.
Resume refuses when a hard identity field changed. Secrets and secret-class fields stay out of
the header and journal. Input bytes and URL values travel through the blob store instead.

``Executor`` is the strategy engine's dispatch port. ``Journal`` is an append-only ordered record
of ``StepResult`` values. ``BlobStore`` holds content by run identifier and digest. Core provides
``InlineExecutor``, ``JsonlJournal``, and ``LocalFsBlobStore``. Their implementations live in the
sibling modules of this package.

The CLI reports exit code 6 when an armed strategy run is interrupted and can be resumed. Replay
refusal, an unknown run identifier, or an unavailable ledger uses exit code 3. The full CLI
contract lives in ``openreading.cli``.
"""
