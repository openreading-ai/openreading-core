# Document processing MCP tools

<sub>[Docs home](../README.md) · [← Artifacts](../artifacts/README.md) · [Channel contract →](../derive/README.md)</sub>

## What this gives you

Your agent can run authorized parsing and strategies, then retrieve complete normalized results through persistent jobs.
The local profiles additionally import documents into retained evidence artifacts with exact citation references.
Full-document access returns the retained JSON in bounded replies; search remains available for focused questions.
MCP is the protocol your client uses to discover these tools and call them over standard input and output.
For example, search for a renewal clause, read its passage, and cite the returned page and evidence identifier.

## Mental model

You start one process with explicit input and artifact directories before the client can call tools.
The input root grants file access, while the separate artifact root retains copies until you remove them.
The local profile exposes `openreading_import`, `openreading_get_document`, `openreading_search`, `openreading_read`, and `openreading_select_document`.
Long work uses `openreading_start_import`, `openreading_get_import`, and `openreading_cancel_import`.
Use `openreading_list_imports` to discover retained jobs after reconnecting.
Call `openreading_get_result` with an `orr1_` result identifier to retrieve a retained normalized response or comparison report.
For example, `delivery="auto"` returns intact content or a local export with its byte count and hash.
Call `openreading_compare` with at least two `result_ids` to retain the existing engine's comparison report.
For example, compare a Docling extraction with another retained response, then retrieve the report through `openreading_get_result`.
Call `openreading_backends` with `{}` to inspect the configured backend's descriptor and OCR flag.
For example, the local Docling profile reports only `docling_local`, regardless of other installed adapters.
Descriptor claims do not establish configured table output, extraction accuracy, dependency readiness or live reachability.
Discovery reports `readiness: "not_checked"`; it does not inspect credentials, read documents or contact providers.
Call `openreading_route` with `{}` to plan the default local backend without reading a source or executing extraction.
For example, `{"backend":"pymupdf"}` requests that backend only when the operator has allowed it.
Planning uses the existing router and reports its chain, excluded backends and any terminal reason.
The optional `--routing-config PATH` snapshots an explicit `openreading.yaml`; repeated `--allow-backend` flags set the independent planning scope.
The policy supplies order, while the allowed set limits access. Neither tool arguments nor ambient configuration can widen that set.
These flags do not change local imports, and static `openreading_backends` discovery continues to describe only the local import profile.
An empty plan sets `isError`. A nonempty plan does not establish backend readiness, format compatibility or completed processing.
Selection returns `selection_unavailable` unless a trusted launcher explicitly supplies a local chooser.

The general profile exposes eight tools without requiring a fixed local parser at startup.
Call `openreading_parse` with the shared request shape or `openreading_batch` with an ordered `requests` array, then use `openreading_get_job`, `openreading_list_jobs` and `openreading_cancel_job`.
For example, `backend.id` set to `strategy:local` selects an operator-authorized strategy with independently authorized backend leaves.
The other three tools are `openreading_route`, `openreading_get_result` and `openreading_compare`, using the same retained-result contracts.
The general profile grants relative paths directly and does not provide a native chooser or local evidence imports.

## Walkthrough

For general processing, install the agent extra and the adapters you intend to authorize.
Each backend reads the formats its descriptor claims in the [adapter catalog](../adapters/README.md).
Configure the client to start this process with existing input and separate artifact directories:

```sh
openreading mcp --profile general-execution-v1 \
  --input-root /absolute/documents --artifact-root /absolute/evidence \
  --execute-backend pymupdf
```

Call `openreading_parse` with `{"document":{"path":"sample.pdf"},"backend":{"id":"pymupdf"}}` for a queued job receipt.
Keep its `job_id`, poll `openreading_get_job`, and retrieve the returned `receipt.result_id` through `openreading_get_result`.
The job state `succeeded` means publication completed; `response_state` describes the parse provider outcome or batch aggregate outcome.
Select an explicit configuration with `--execution-config` and repeat `--execute-strategy` for each authorized strategy entrypoint.
Forward required existing credentials with `--execution-env NAME`, such as `--execution-env REDUCTO_API_KEY` for an authorized hosted adapter.
Only selected environment values reach workers, and the fixed private directory names listed in CLI help refuse overrides.
Optional execution deadlines include queue time, while `--execution-concurrency` defaults to one running job per grant.
Missing backend grants refuse nonempty execution, and startup does not claim the configured backends are ready.
For example, `openreading_batch` accepts `{"requests":[{"document":{"path":"sample.pdf"},"backend":{"id":"pymupdf"}}]}`.
A batch holds one operator concurrency slot and executes every item serially, including duplicate requests.
All request scopes are checked before any input is acquired; missing sources refuse acceptance before provider work.
Each accepted item uses its own isolated worker, and failures preserve input order through Core's shared batch runner.
Retrieve its complete `batch_result` through `openreading_get_result`; a succeeded item can still contain a failed extraction response.
Empty input retains a batch with `status.state=failed` and the warning code `empty_batch`.
Job `state=succeeded` means publication only, including publication of an empty batch with a failed aggregate status.
Cancellation or interruption prevents final batch publication.
Completed private attempts persist, and cancelling a batch cannot undo provider calls already completed.
This operation accepts explicit granted files; directory expansion and native provider batch dispatch are not implemented here.


Install the optional dependencies in your development environment, then configure your client to launch this command:

```sh
python -m pip install 'openreading[agent,pymupdf]'
openreading mcp --profile local-document-proof-v1 \
  --input-root /absolute/documents --artifact-root /absolute/evidence
```

Source and wheel installs identify their package version, backend version, and installed extraction code.
A core commit appears only when verified packaging metadata supplies it; an enclosing Git repository is never consulted.
Your client supplies the following tool calls through MCP; these JSON objects are their argument payloads.

```json
{"path":"agreement.pdf"}
```

Pass the receipt's `artifact_id` to `openreading_get_document` to read the entire retained normalized result.
Its import receipt retains a legacy search suggestion; you can use either retrieval route.
For example, call `openreading_get_document` with `{"artifact_id":"<returned artifact_id>","delivery":"auto"}`.
A fitting result contains intact `content.response`, with page origins and citation mappings alongside it.
Otherwise, the receipt reports a complete local JSON file, byte count, SHA-256, and warning-code summary.
The summary counts warning records, not affected pages or regions; full details remain inside the exported content.
Measured text origins count physical pages, including missing measurements separately from recorded unknown origins.
Unpaginated documents report null page counts and null physical-page summaries.
Their evidence uses exact JSON locations instead of physical pages; always pair evidence identifiers with their artifact identifier.
The empty-text preview lists up to 16 physical pages with neither page text nor block text.
It reports omitted page numbers and does not assign locations to unrelated parser warnings.
A local path does not prove that the host received the file or can open it.
Use an existing authorized file tool when available, or attach the export to share it with the assistant host.
If the host saves an accepted tool response into its own file, its existing file tools can inspect that result.
Set `delivery` to `file` to export a small result, or omit it to retain the previous fragment interface.
In fragment mode, follow `next_cursor` until null and preserve every fragment's order.
The result includes existing structure, parser warnings, page origins and citation references, while excluding `backend_raw`.
Pass a returned evidence identifier to `openreading_read` for the exact citation quote.
Alternatively, call `openreading_search` with `query` set to `renewal` for a focused question.
Follow `next_cursor` when present, and explain evidence gaps when the returned passages do not support an answer.

## Recipes

For a long import, pass `{"path":"agreement.pdf"}` to `openreading_start_import`.
It returns a job ID promptly. Pass that ID to `openreading_get_import` with `wait_seconds` up to 20.
Stage and elapsed time describe observed work; no percentage or completion estimate is invented.
After `succeeded`, use the returned receipt for normal retrieval and citations.
Call `openreading_cancel_import` to stop a job, then check its terminal state.
A host disconnect or cancelled status request leaves background work running.
After reconnecting, call `openreading_list_imports` with `{}` to recover job identifiers.
Follow its `next_cursor` for more summaries, then request status or cancellation by identifier.
The list covers this input grant only, including completed jobs and unreadable status records.
Job order follows identifiers rather than creation time; restart listing to discover concurrent arrivals.

- Configure arguments as an array in your MCP client so spaces in directory names remain intact.
- Grant a narrow document directory rather than your home directory, and keep retained evidence outside that grant.
- Reuse the same artifact identifier across questions instead of importing the same source repeatedly.

For an embedded chooser, pass `selection_provider=picker` to `openreading.mcp_server.main.main(argv)`.
The provider follows the transactional context-manager contract in `openreading.mcp_server.selection`.
A batch provider yields copied references and skipped-entry counts; the selection tool returns paginated receipts.
For example, call the same tool with its `next_cursor` to continue without reopening a chooser.
Import each item separately and retain its job and artifact identifiers for cross-document questions.
A folder snapshot never grants live access; later files require another explicit selection.
Shield asynchronous cleanup, including failed acquisition, so cancellation cannot interrupt child reaping or rollback.
For example, use `anyio.CancelScope(shield=True)` around cleanup awaits before propagating cancellation.
Keep blocking work off the event loop and remove only copies created by that selection.
Call `openreading_select_document` with `{}`, then import the returned `path`.
The default selection deadline is 120 seconds, independent of extraction.
A trusted launcher can pass `selection_timeout_seconds=None` to disable the local chooser and copy deadline.
Local Cancel and host-delivered cancellation still clean up the selected copy.
Local Cancel remains available when a host Stop button does not deliver protocol cancellation.
If the chooser cannot be reached, restart the client to reset selection.
Detached imports continue across that restart and can be discovered through the job listing.

## How it decides

The profile selects its configured local adapter explicitly, preventing ambient routing from changing extraction.
Each result contains one JSON text payload, preventing duplicate structured content from spending context twice.
Invalid arguments produce fixed protocol errors, preventing schema validation from echoing private input values.
Document text remains untrusted data, preventing a quoted instruction from gaining tool authority.

Internal general-execution preflight snapshots explicit operator configuration and independent backend and strategy entrypoint scopes.
For example, a permitted strategy cannot authorize its reducto leaf when the backend scope excludes reducto.
Read `openreading.mcp_server.execution` for the shared authorization contract used at admission and again inside the worker.
Preflight performs no source acquisition, credential resolution or execution, and adds no callable tool.

The internal `ExecutionAttempt` worker runs authorized requests in a disposable child process.
It copies from the granted descriptor, reauthorizes in the child, and preserves backend scope through the shared API.
For example, replacing the original pathname cannot redirect an already opened source descriptor.
Provider output cannot enter MCP stdout; complete normalized responses return as validated `ResultContent` values.
A private liveness pipe terminates the local execution group if its supervisor disappears.
Control writes remain cancellable when the pipe fills, including on older supported Python versions.
Its explicit operator environment avoids ambient host credentials and configuration, but provides no operating-system sandbox.
Reserved directory and configuration overrides fail before an attempt starts, with the five reserved names listed in `execution_process`.
Private source copies and strategy journals persist under an attempt directory; the internal job records retain its location.
The general tool layer checks admission receipt budgets before launching jobs, while scoped resume remains separate work.

## Operations

`openreading_compare` accepts ordered `or1_` artifact or `orr1_` normalized-response identifiers under this input grant.
Its optional `baseline` identifies an existing input or another retained response, appended after the same authorization checks.
Repeated identifiers remain separate subjects; a matching baseline selects the first occurrence.
The returned receipt identifies a retained comparison report, available through `openreading_get_result`.
The report uses the existing engine without reparsing documents or executing either backend.
Its `provenance.subjects` maps report labels to input identifiers; the `adapters` map uses those labels to preserve repeated backend versions.
For each label, `provenance.subject_sources` reports source hashes and their verification basis.
For example, an `or1_` subject has one `verified_source_bytes` hash; an `orr1_` subject retains its `producer_asserted` hash list.
The verified hash describes a retained copy rehashed against its manifest when comparison loads the input.
It does not establish whether your original file still matches or whether two subjects share the same document.
Retrieving the retained report does not repeat that source-byte check.
Earlier comparison records lack that attribution and retain their original bytes without inferred verification claims.
Report locations describe normalized comparisons, not new source citations or independently verified accuracy.
For example, read the mapped input artifact before citing a page supporting a reported text disagreement.
Retained response fingerprints are inherited producer assertions; comparison does not establish that inputs describe the same source document.
A report's `source: file` describes retained JSON input, not the original parser acquisition method.
The synchronous comparison runs in a thread; host cancellation does not undo completed publication.
Repeat unchanged arguments with unchanged inputs and implementation to recover a lost receipt.
Comparison has no durable job or computation deadline here. Truth scoring and corpus comparison require separate MCP input contracts.

Use `openreading_get_result` with a returned `orr1_` identifier for complete general response or report retrieval.
Its default `delivery="auto"` returns intact content or a local export with content length and hash.
Use `delivery="fragments"` and follow each cursor to null when reconstructing the result through bounded replies.
For example, concatenate text spans at `/payload/document/text` using their supplied character offsets.
The same `--document-response-bytes` and `--document-export-root` settings govern these replies and exports.
General results include producer provenance and preserve partial status, warnings and values without inventing citation passages.
Report locations identify report content; use the mapped input artifacts for source-document evidence.
Existing `or1_` artifacts continue using `openreading_get_document`, search and exact reads unchanged.
The general MCP tools expose durable execution jobs with complete retained normalized responses and separately reported provider outcomes.
`openreading.mcp_server.execution_jobs` documents the internal `ExecutionJobs` start, get, list and cancel lifecycle.
Each accepted job survives client exit and retains one complete result under its original input grant.
For example, a retained failed response has job state `succeeded` and separate `response_state: failed`.
A `processing` response remains processing; job completion does not certify completed remote work.
Cancellation before publication yields no receipt; a committed result wins a concurrent cancellation request.
Recovery verifies a committed receipt after supervisor death and never repeats provider execution automatically.
Status lookup and listing persist recovery outcomes, which their general tool annotations identify as writes.
Concurrency defaults to one job per grant; explicit deadlines include time waiting for a slot.
Job records, snapshots and journals persist until removed, and reconnecting clients can discover work without starting duplicate parses.

The process speaks MCP on stdout; configure your client to capture diagnostics separately from that protocol stream.
Domain errors set `isError` and return fixed codes from `openreading.artifacts.limits`, `openreading.artifacts.result_models` or `openreading.types.selection`.
Selection uses v0.2 with compatible single-file receipts; complete delivery uses document-tool v0.4.
The v0.1 fragment result remains compatible. New artifacts use v0.4; legacy v0.3 artifacts remain readable.
Full-document access sends retained content to your assistant and does not establish token savings.
It preserves the stored result, including extraction limitations, rather than promising perfect OCR recognition.
The import profile decides which channels exist; this tool does not enable disabled table extraction.
`--document-response-bytes` defaults to 1000000 bytes for the complete serialized MCP response, including escaping and request ID.
It controls delivery, never document processing. Different hosts can impose smaller independent limits.
`--document-export-root` selects a trusted local directory; without it, exports stay under the private artifact store.
Exports use private files beneath a grant-specific directory and remain until explicitly removed.
Removing an artifact does not remove an already exported copy. Model arguments cannot set export paths.
Malformed arguments return protocol errors, while normal transport closure exits with status zero.
Configuration failures exit two. SIGINT and SIGTERM cancel synchronous imports and exit 130, including clients that keep stdin open.
Detached imports continue and keep private status under `jobs/INPUT_GRANT_SHA256/JOB_ID/`.
Before uninstalling a client, cancel unwanted jobs and wait for terminal states.
There is no uninstall cancellation hook; removing the client can leave detached work running.
Reconnecting with the same input and artifact roots restores job discovery and cancellation.
The local profile requires POSIX support. Explicit root symlinks resolve once before file access begins.

The [artifact guide](../artifacts/README.md) explains retention, source hashing, page provenance, and quota behavior.
The Python SDK dependency is optional and pinned through the lockfile's supported v1 release line.

## Reference

Run `openreading help mcp` for the command contract and `openreading mcp --help` for its required arguments.
Read `openreading.mcp_server.tools` for input schemas, tool annotations, and server instructions.
Read `openreading.artifacts.document` for JSON Pointer fragments, exact reconstruction and continuation binding.
Those instructions prohibit guessed evidence identifiers and distinguish literal search gaps from missing source facts.
They describe block offsets and generic warnings as limited evidence, rather than explanations for suspected extraction failures.
Instruction delivery does not prove compliance; each native client needs its own behavior check.
Client packaging and installation checks belong in the separate `openreading-agent-tools` repository.

## Not built yet

Batch execution, scoped resume, readiness and liveness discovery, strategy inspection and proposed triage remain unbuilt MCP operations.
Comparison covers retained normalized responses; truth scoring and batch-result corpus comparison remain unbuilt MCP operations.
The remaining agent surface is proposed in [the agent design](../../../design/agentic.md).
Passing stdio tests does not establish desktop installation compatibility or measured model token savings.

Background status exposes `page_progress` when Docling reports successful physical-page assembly.
For example, `pages_assembled: 12` with `total_pages: 251` describes observed assembly, not an estimated percentage.
Document-wide processing and publication can still be pending after all pages finish assembly.
Absent progress means no observation; cached imports do not invent new processing.
