"""Vendored JSON Schemas (the source of truth) + a validator CLI.

The ``*.json`` files in this package define the whole OpenReading contract. They were cut from
internal/research/openreading/normalized_schema.md §2/§3, and every surface (CLI,
``openreading.run`` / ``route``, the HTTP server) speaks exactly these shapes. Pydantic models in
``openreading.types`` are ergonomic constructors that MUST validate against these files
(DECISIONS D4: where the two could drift, the JSON Schema wins; D5: control-plane runtime types
are dataclasses, only the wire envelope is pydantic). ``python -m openreading.schemas validate``
is wired into ``make verify``.

The one-sentence contract: one request shape in, one response schema out, for every backend —
swapping ``backend.id`` from ``pymupdf`` to ``aws-textract`` to ``reducto`` changes nothing else.

Families
--------
- request (``request_schema``): CLI flags, ``run()`` kwargs, and ``POST /v1/parse`` /
  ``/v1/route`` / ``/v1/jobs`` bodies all build this one shape.
- response (``response_schema``): CLI stdout, ``run()``'s return value, ``/v1/parse``, the
  ``response`` field of a job.
- adapter-descriptor (``descriptor_schema``): the router (eligibility, ranking), the credential
  broker, ``openreading backends``, ``/v1/backends``.
- strategy-config (``strategy_config_schema``): the optional ``openreading.yaml`` grammar
  (``openreading.strategies``).
- comparison-report (``comparison_report_schema``): the diff over N responses —
  ``openreading.compare()`` and the ``openreading compare`` renderers (``openreading.comparison``).
- batch-result / corpus-report: one envelope over many documents — ``openreading.run_batch()``,
  ``openreading parse <dir>``, ``POST /v1/batch`` — and batch-vs-batch comparison —
  ``openreading compare runA.json runB.json`` where both arguments are batch envelopes
  (``openreading.batch``, internal/design/batch-intake.md §5/§8).
- leaderboard-report: N backends ranked on one ``evals.dataset`` corpus (``openreading.evals``).
- liveness-report: one backend's liveness answer — ``openreading backends --check``,
  ``POST /v1/backends/{id}/liveness`` (``openreading.liveness``,
  internal/design/liveness.md §5).
- local-document / passage / agent-document-tool: retained evidence and bounded MCP
  payloads owned by ``openreading.artifacts``. These separate families leave the existing
  request and normalized response contracts unchanged.
- step / journal: the executor step contract and the per-line JSONL journal shape
  (internal/design/ledger.md §5.4).

Not in these schemas: the routing plan (``/v1/route``), job handles (``/v1/jobs``), backend
readiness (``/v1/backends``) and the HTTP error envelope are small purpose-built objects owned by
``openreading.server``.

Validate anything programmatically with ``validate_request`` / ``validate_response`` /
``validate_descriptor`` (each raises ``jsonschema.ValidationError``). The server validates bodies
on the way in and responses on the way out; the CLI validates before printing. Inspect any
backend's descriptor with ``make_adapter(id).descriptor.to_schema_dict()``.

Request (``request.v0.3.json``)
-------------------------------
Required: ``document`` and ``backend``; ``additionalProperties: false`` at the top level AND at
every nested object node — ``document``, ``backend``, ``backend.runtime``, ``outputs``,
``outputs.chunking``, ``extraction_schema``, ``features``, ``pages``, ``pages.ranges[]``,
``routing``, ``async`` (unknown keys are rejected — D7: a deployment knob such as the backend
allow-list therefore lives on ``RouterConfig``, never on the wire). The one
deliberate exception is ``extraction_schema.json_schema``'s VALUE, an arbitrary caller-supplied
JSON Schema the wire contract does not shape. Nested strictness is v0.2 (M12): v0.1 closed the
top level only, so a misspelled nested field such as ``document.mim_type`` passed schema
validation and failed only later, at the pydantic layer (``openreading.types.request``, already
``extra="forbid"`` throughout) — the vendored schema stopped being the source of truth exactly
where nesting began. Optional blocks: ``schema_version`` (const ``"0.3"``), ``outputs``,
``features``, ``pages``, ``extraction_schema``, ``routing``, ``async``,
``idempotency_key``.

- ``document``: exactly ONE of ``bytes_base64`` (the router may spool/upload where a backend needs
  storage, e.g. Textract's S3 flow), ``url`` (passed through to backends declaring
  ``accepts_url`` — azure-document-intelligence, chunkr, docling, mistral-ocr, open-ocr, pulse,
  reducto —
  downloaded to bytes first for the rest), ``path`` (local backends only), ``file_id`` (a
  previously uploaded file, e.g. a reused Chunkr task). Plus ``mime_type`` (inferred from the
  extension by CLI/Python, default ``application/pdf``), ``filename``, ``password``.
- ``backend``: ``id`` (registry slug, or ``null`` to resolve the chain from the policy), ``type``
  (``hosted_api`` | ``oss_library`` | ``framework_loader`` | ``self_hosted_model``,
  informational), ``operation`` (Textract ``DetectDocumentText``/``AnalyzeDocument``/
  ``AnalyzeExpense``/``AnalyzeID``/``AnalyzeLending``, Azure model ids, reducto/chunkr
  ``parse``/``extract``), ``version`` (pin), ``credentials_ref`` (a BYO-key HANDLE, never a raw
  secret; normally omitted, in which case credentials resolve from the environment via
  ``openreading.credentials``; the ``env:<alias>`` scheme resolves each key from
  ``<alias>_<KEY>`` and the alias must be allow-listed in ``OPENREADING_CREDENTIALS_REF_ALIASES``
  — an unknown alias is refused rather than treated as a free-form env prefix, so a request body
  cannot aim the broker at arbitrary environment), ``runtime`` (``mode`` in_process/subprocess/
  container/remote_endpoint, ``endpoint``, ``image``, ``device``, ``system_deps_ok``).
  ``runtime.endpoint`` is operator configuration ONLY and is refused when set in a request —
  configure it through the backend's own variable (``QWEN_VL_ENDPOINT``, ``DOCLING_SERVE_URL``,
  ...); every other ``runtime`` field still overrides environment-resolved config.
- ``outputs``: ``markdown`` (true), ``text`` (true), ``blocks`` (true), ``typed_fields`` (false),
  ``tables`` (``none`` | ``cells`` | ``markdown`` | ``html``, default ``markdown``), ``chunking``
  (``strategy`` none/by_page/by_title/by_section/by_similarity/fixed, ``max_characters``,
  ``overlap``), ``include_backend_raw`` (true). What a backend cannot fill is recorded in
  ``warnings[]`` — never a hard error, never a fabricated value.
- ``features``: ``ocr`` (auto/force/off), ``ocr_languages``, ``layout``, ``reading_order``,
  ``tables``, ``forms_key_value``, ``figures_images``, ``signatures``, ``classification``,
  ``handwriting``. Booleans default off except layout/reading_order/tables. An unhonorable flag
  becomes a warning; under routing, required capabilities filter candidates at stage 2.
- ``extraction_schema``: ``json_schema`` (draft 2020-12 subset), ``instructions`` (LLM guidance),
  ``citations`` (per-field page+bbox grounding where supported). Routed to the backend's native
  mechanism; a backend that structurally cannot extract (pymupdf) raises ``unsupported_feature``
  rather than silently returning geometry-only (D13).
- ``pages``: ``ranges`` of 1-based inclusive ``{start, end?}``, ``max_pages``; applied natively
  where supported, else by the normalizer.
- ``routing`` (for ``backend.id = null``): ``doc_type_hint`` (bank_statement, paystub, w2,
  1003_loan_app, 1040_tax, invoice, id_document, medical_form, clinical_pdf, generic),
  ``fallback`` (ordered ids, honored WITHIN the resolved set only — routing reorders a
  restriction and never widens one).
- ``async``: ``mode`` auto (default; router picks per backend and document) | sync | async;
  ``webhook_url`` (always caller-supplied; without it async polls).
- ``idempotency_key``: when omitted, defaults to a deterministic key over the document content,
  backend, resolved version and result-affecting options, so a retried submit is recognised as
  the same request. The default exists ONLY for ``bytes_base64`` / ``path`` documents; a ``url``
  or ``file_id`` document gets no default (bytes are not fetched at that stage, so nothing
  content-stable exists). That leaves URL submissions to azure-document-intelligence, chunkr,
  pulse and reducto without default retry protection, and means open-ocr's real
  ``Idempotency-Key`` header never fires for a URL source. Pass the key explicitly when retry
  safety matters. This gap is open, tracked in internal/eng-council/FOUNDER-INBOX.md. Whether a key
  reaches the vendor at all is the descriptor's ``idempotency_supported`` (below).

Response (``response.v0.3.json``)
---------------------------------
Required: ``schema_version`` (const ``"0.3"``), ``status``, ``backend``, ``document``. The honest
common denominator is the schema's ``anyOf``: at least one of ``document.markdown``,
``document.text``, ``document.pages`` or top-level ``typed_fields`` is always present. What a
backend cannot produce stays absent rather than fabricated. Warnings explain some limitations,
but they are not an exhaustive inventory of missing channels. Inspect the content fields and
the experimental ``channel_provenance`` map when deciding whether a result meets your needs.
Presence does not imply nonempty content: ``document: {}`` with ``typed_fields`` is schema-valid.

Read a response in this order: ``status`` for completion, content for the data you need,
``warnings`` for limitations, then ``backend`` and ``usage`` for production context.
The consumer walkthrough is the schemas README. ``openreading help response`` gives terminal
readers the field paths, optional-value rules, and distinctions between outer response shapes.

- ``status.state``: ``succeeded`` | ``partial`` | ``failed`` | ``processing`` (a CLOSED enum);
  optional ``error`` {``code``, ``message``, ``backend_code`` = the vendor's original code}.
- ``backend``: ``id``, ``type``, resolved ``operation``/``version``, ``output_paradigm`` — which of
  the six raw shapes (``markdown``, ``typed_fields``, ``element_list``, ``block_tree``,
  ``block_graph``, ``token_stream``) the backend natively produced, so a consumer knows native
  from derived.
- ``document``: ``markdown``/``text`` (reading-order renditions, native or assembled),
  ``page_count``, ``language``, ``doc_type``, ``confidence`` (document-level [0,1] — the honest
  home for whole-document signals, never smeared onto blocks), ``pages[]``. Each page:
  ``page_number`` (1-based, into the SOURCE document, preserved under page subsetting — C9),
  ``width``/``height``/``unit`` (``pdf_point``/``pixel``/``inch``; needed to de-normalize
  geometry), ``dpi``, ``rotation``, per-page ``markdown``/``text``/``confidence``,
  ``source_backend`` (strategy runs), and ``blocks[]``.
- ``blocks[]`` — the common spine, one entry per reading-order element: ``type`` (a CLOSED enum
  of 22: title, section_header, header, footer, page_number, text, list, list_item, table,
  table_cell, figure, image, caption, formula, code, key_value, form_field, signature,
  selection_mark, barcode, table_of_contents, other — safe to switch on exhaustively; only a
  MAJOR may add to it), ``native_type`` (the backend's own label verbatim, e.g. Textract
  ``KEY_VALUE_SET``; an unmapped native concept arrives as ``type: other`` with the label kept
  here, which is why the enum can stay closed), ``text``/``markdown``/``html``, ``bbox`` (optional — markdown-derived
  blocks carry none rather than invented geometry), ``confidence`` ([0,1]; absent for
  deterministic parsers and token-stream models, explained by ``confidence_unavailable``),
  ``reading_order`` (0-based), ``table`` (``n_rows``, ``n_cols``, ``cells[]`` with
  row/col/spans/text/is_header/bbox/confidence, and a lowest-common-denominator ``rows``
  list-of-lists: span origins filled, covered positions ``None``), ``children`` (child block ids
  when the source preserves hierarchy — Textract CHILD relationships, Docling groups, DocAI; flat
  consumers ignore it), ``text_type`` (printed | handwriting | unknown).
- ``bbox`` — canonical geometry with native lineage (C8, D6): every box is top-left origin,
  y-down, normalized [0,1] relative to its page — ``x``/``y``/``w``/``h``, ``page`` (1-based),
  optional ``polygon`` in the same space — and ``bbox_native`` keeps the raw source geometry
  untouched (``coords``, ``origin`` top_left/bottom_left, ``unit``
  normalized/pdf_point/pixel/inch, ``dpi``) so every conversion is lossless and auditable.
  ``openreading.types.geometry.to_canonical`` is the single choke point that produces both.
- ``typed_fields``: a map keyed by field name (extractor backends and schema-driven extraction;
  absent for pure parsers). Each value: ``value`` (native JSON type — no ``str()`` coercion;
  repeated names collect into a list, nested provider structures stay nested), declared
  ``type``, ``normalized_value``, ``confidence`` (numeric [0,1], or the vendor's qualitative
  string preserved as-is), ``citations[]`` (``page`` + ``bbox`` + ``text``).
- ``chunks[]``: ``id``, ``text``/``markdown``, ``block_ids`` (each chunk traces to spine blocks),
  ``page_span``, optional ``embedding``.
- ``usage``: ``pages_processed``, native ``credits``, ``input_tokens``/``output_tokens``,
  ``duration_ms``. Counters only, in the unit each backend meters in. The adapter meters
  (``report_cost()`` projects the counters out of ``job.raw``) and the router accounts: after
  ``normalize()`` it fills only the ``usage`` fields the adapter left unset, and never reshapes
  one unit into another. ``cost_usd`` and ``cost_basis`` were removed with the per-vendor price
  tables that filled them. A derived price sat on ``usage`` beside
  counters that were measured, and nothing downstream could tell the two apart.
- ``job``: the async handle (``id``, timestamps, ``poll_url``, provider console URL).
- ``warnings[]``: ``{code, message, field}`` for anything requested but unavailable, degraded or
  noteworthy. The code set is OPEN, so a consumer tolerates a code it has never seen. Every code
  a response can carry is the first argument of an ``add_warning(`` call in this tree, so the
  shipped set stays greppable. As shipped, grouped by what each one tells you:

  - a channel is missing: ``<channel>_unavailable`` for ``markdown``, ``text``, ``blocks``,
    ``block_bbox``, ``block_confidence``, ``table_cells`` and ``typed_fields`` (this backend
    could not produce it for this document), ``channel_unsupported``, ``tables_unsupported``
    and ``typed_fields_unsupported`` (the backend has no such capability at all),
    ``channel_unavailable_in_mode`` and ``channel_not_produced_by_operation`` (the capability
    exists, the operation or mode this run chose does not carry it), ``typed_fields_empty``,
    ``confidence_unavailable``, ``page_attribution_unavailable``, ``cost_unavailable``.
  - the output is degraded: ``typed_fields_malformed`` (the vendor's structured output was not
    a JSON object, so the response is PARTIAL), ``typed_fields_unverified``,
    ``output_truncated``, ``interaction_incomplete`` (the vendor stopped before finishing),
    ``partial_conversion`` (the local converter returned partial output), ``bbox_space_approximate``, ``ambiguous_page_provenance`` (unattributable text omitted),
    ``unreadable_pages`` (no page-addressable text), and ``table_text_unavailable``
    (table text absent while structured table extraction is disabled).
  - the run took a detour: ``fallback_used`` (the router's attempt trail),
    ``idempotent_replay``, ``quality_below_threshold`` (every rung gated, best result
    retained), ``quality_escalated`` (a rung gated and a later rung answered, so the walk
    recovered), ``budget_exhausted`` (the time budget, not a gate, ended a strategy walk).
    Those last three are separate codes because escalating is right for a gate and wrong for
    an exhausted budget. An agent triage playbook branches on them to separate "escalate"
    from "consume".
  - the vendor said so: ``backend_warning`` (the vendor's own warning text, passed through).

  ``unsupported_feature`` is NOT a warning code. It is an ``on_error`` map key, an exception
  category and an HTTP ``error.category``, each documented elsewhere in this file. Unknown warning
  codes remain valid, so consumers preserve them instead of rejecting an otherwise usable response.
- ``backend_raw`` (present by default via ``outputs.include_backend_raw``): the untouched native
  payload — ``payload`` (the raw value: hosted-API JSON verbatim or the serialized native object
  for libraries; a reference handle instead when too large to inline, paired with ``encoding:
  reference``), ``encoding`` (json | json_serialized_object | text | base64 | reference),
  ``media_type`` (e.g. ``application/vnd.docling+json``), ``object_class`` (provenance of
  serialized objects, e.g. ``fitz.Page.get_text.dict``), ``truncated`` (set when the payload was
  too large to inline). Explicitly OUTSIDE the versioned contract: opaque, may change without a
  bump.
- ``orchestration`` (strategy runs only; permissive control-plane object), ``channel_provenance``
  (``{channel: native | derived}`` per response, ``x-stability: experimental`` — static
  descriptor grades cannot express mode-dependent adapters such as qwen or azure),
  ``schema_url`` (the ``$id``).

The normalized view is the portable consumption surface. Raw payloads and native coordinates
provide additional lineage when present, but raw payload contents are not a stable API.

Channel invariants: what a channel is allowed to hold
-----------------------------------------------------
Eleven invariants, C1 to C11, say what each channel of a response may hold, and the schema's
``$defs`` descriptions name them by id. ``openreading.derive`` carries the normative text, so
this file names the ids and points there rather than keeping a second copy that can drift.
Read them with ``uv run python -m pydoc openreading.derive``.

- C1 ``text.plain``, C2 ``text.complete``, C3 ``markdown.gfm``, C4 and C5 (an impossible
  channel is never populated, and an explicit request for one warns), C6
  ``channels.deliver-or-warn``, C7 ``confidence.unit``, C8 ``bbox.canonical``, C9
  ``page.provenance``, C10 ``status.honest``, C11 ``text-blocks.coherent``.
- C12 is the one invariant about this package rather than about a channel. It requires version
  identity per family, so a family's filename version, its ``$id`` version and its in-band
  const agree. The manifest below is where that is enforced.

Each invariant covers every field of its channel's type, so C1 binds ``document.text``,
``pages[].text``, ``blocks[].text`` and ``chunks[].text`` alike. The table contract that used to
be restated here lives with the rest, in ``openreading.derive``. The invariants became hard
guarantees at the Canon milestone, the ``0.5.0`` row of the manifest below. ``0.5.0`` there is a
milestone label rather than a package version. The shipped package version is ``0.3.0``.

Adapter descriptor (``adapter-descriptor.v0.8.json``)
-----------------------------------------------------
A static, machine-readable declaration per adapter — the reason the router NEVER branches on
backend type: credential resolution, execution, and the readiness UI read descriptor fields.
Required: ``id``, ``type``, ``provisioning``, ``wait_modes``, ``capabilities``, and ``runtime``.
No ``additionalProperties: false`` (so each additive bump
keeps every older descriptor valid) and no in-band version — filename + ``$id`` only.

- Identity: ``id``, ``type``, ``adapter_impl`` (http | in_process | subprocess | container),
  ``operations`` (sub-operations with distinct behaviour/cost), ``wait_modes`` (inline | poll |
  webhook).
- ``provisioning``: ``byo_mode`` (api_key, cloud_credential, pip, container, weights, endpoint —
  a coarse hint; the per-key truth is ``credentials_spec``), ``auth`` (none/api_key/sigv4/
  oauth2/entra/gcp_adc).
- ``capabilities``: each of ``ocr``, ``handwriting``, ``printed_tables``, ``complex_tables``,
  ``forms_key_value``, ``layout``, ``reading_order``, ``multi_column``, ``figures_charts``,
  ``signatures``, ``classification``, ``splitting``, ``custom_schema_extraction``,
  ``vlm_based``, ``human_in_the_loop`` is ``"verified"`` (first-party live run or benchmark),
  ``"claimed"`` (vendor docs only) or ``false``; plus ``languages``, ``input_formats``,
  ``max_pages_per_request``, ``max_file_size``. Descriptive, not a gate: the router runs no
  capability filter, and a backend that cannot read a document refuses first-hand.
- ``output``: ``paradigms`` (the six raw shapes above); ``channels`` grades each response
  channel — ``markdown``, ``text``, ``blocks``, ``block_bbox``, ``block_confidence``,
  ``typed_fields``, ``table_cells`` — as N (native), D (derivable) or X (impossible); the
  conformance kit enforces C4/C5/C6 against these grades; optional ``block_granularity``
  (word | line | paragraph | section | element) so consumers and compare can reason about
  packaging differences instead of discovering them empirically.
- ``runtime``: ``offline_capable``, ``license`` (copyleft flagged here), ``system_deps``,
  ``version_pin``, hardware/serving profile, ``sandbox``. ``router`` carries only
  ``normalization_difficulty``. ``sources``: primary-source URLs with
  access dates backing every claim above.
- BYO-credential declaration (v0.2): ``credentials_spec[]`` (``{key, required, secret, env:
  [names in precedence order], description, example}``), ``config_spec[]`` (non-secret config:
  endpoints, model names, processor ids, regions, buckets), ``signup_url`` (where a user
  provisions credentials; surfaced in missing-credential error messages and readiness output),
  ``accepts_url``, ``live_gate_env`` (variables gating the live test; explicit for
  ambient-chain backends like
  Textract). The broker, ``openreading backends``, ``/v1/backends`` and missing-credential errors
  are all generated from these — no per-backend logic anywhere else. The kit requires them of
  every backend with ``auth != none`` OR an endpoint/container ``byo_mode``: docling and qwen-vl
  run on your own hardware and still need an endpoint URL.
- ``batch`` (v0.4): ``native`` (verified | claimed | false), ``max_items``, ``max_concurrency``,
  ``notes``. Absent means platform batching (the runner fans out single-document runs).
- ``liveness`` (v0.5): ``probe`` (none | local | endpoint | vendor), ``method``, ``timeout_s``,
  ``notes``. Absent/``none`` means the platform infers a status from configuration instead —
  statically readable so a UI can say "cannot be tested" before probing. A probe is never a
  billed request; that rule is deliberately NOT a ``billable`` field, whose only honest value
  would be ``false`` (D-v7-4).
- ``idempotency_supported`` (v0.6, default true): true iff ``submit()`` forwards the key in a
  form the vendor honours on a retried submit, so the retry cannot be double-billed. Never send a
  header the vendor would silently ignore. True: pymupdf, tesseract, docling (no vendor call),
  qwen-vl (a retry only re-burns GPU), open-ocr (real ``Idempotency-Key``), aws-textract (real
  ``ClientRequestToken``; the S3 upload key it depends on is derived from the document's content
  digest, not a fresh key per attempt). False: anthropic-claude, azure-document-intelligence,
  chunkr, google-document-ai, google-gemini, mistral-ocr, nuextract, pulse, reducto (no
  vendor-side mechanism found).
- ``cancel_supported`` (v0.6, default true): true iff ``cancel()`` stops the job AT THE VENDOR,
  not merely locally. True: chunkr (only while still queued), nuextract, pulse, reducto. False:
  anthropic-claude, aws-textract, azure-document-intelligence, google-document-ai, google-gemini,
  mistral-ocr, open-ocr (no vendor cancel API, or inline-only dispatch that never holds a live
  job). Local/self-hosted
  inline-only adapters keep the default. Evidence: internal/eng-council/sprint26-BL-164.md and
  internal/eng-council/implementation/sprint26-BL-166.md.
- ``protocol_version`` (v0.7): the adapter-contract version implemented (1 = poll/
  resolve_webhook/cancel take no ctx, a client may be cached on self; 2 = they take
  ``RunContext`` and no client is ever cached on self). Optional in the schema (keeps v0.7
  additive) but required with no default in the pydantic model, and ``Registry.register()``
  refuses a version below the router's floor.

Versioning rules
----------------
- The version lives in the filename (``*.v0.N.json``) and in ``$id``. In-band, per family:
  response, comparison-report, batch-result, corpus-report, leaderboard-report and
  liveness-report REQUIRE a ``schema_version`` const — the in-band const is the only version
  signal a consumer gets (no HTTP header carries it); request's ``schema_version`` is optional
  (each file's own const; ``"0.2"`` in the newest) with the newest as the documented default
  (making it required would be optional-to-required = MAJOR, deferred); adapter-descriptor,
  step and journal have none;
  strategy-config keeps its own integer ``version`` const (1 in both v0.1 and v0.2). Two
  ``$id`` shapes coexist and both are permanent (released files are immutable): the slash form
  ``https://openreading.ai/schemas/<family>/vX.Y.json`` on request, response, strategy-config
  and adapter-descriptor v0.1-v0.4; the dotted form
  ``https://openreading.ai/schemas/<family>.vX.Y.json`` on every other file (adapter-descriptor
  v0.5+, comparison-report, batch-result, corpus-report, liveness-report, leaderboard-report,
  step, journal). Anything matching ``$id`` must accept both. The package version is the
  deployment vector — self-hosted BYO, no central service, so no server-side down-conversion.
- Additive by default: every valid older instance stays valid, tested transitively — each
  released response golden validates against its own and every newer response schema
  (``tests/test_schema_evolution.py``), and each descriptor bump carries an
  "older descriptor still validates" test.
- MINOR allowlist: add optional properties; add values to sets documented OPEN (warning codes,
  backend ids); widen types where absence was already handled; add schema files; relax
  producer-only constraints. Consumers must tolerate all of these — the response envelope
  (``NormalizedResponse``, ``extra="ignore"``) parses unknown top-level fields and survives
  re-serialization, with the unknowns DROPPED so ``to_schema_dict()`` never carries an
  unvalidated field; only the nested payload models keep ``extra="forbid"``.
- Which sets are OPEN is not a matter of policy, and is readable off the schema: an open set is
  declared ``{"type": "string"}`` and carries its known values in prose (``warnings[].code``,
  ``backend.id``). A JSON Schema ``enum`` is CLOSED by construction — a value outside it fails
  validation, so a consumer switching exhaustively on one is doing what the contract invited.
- MAJOR: remove/rename a field; optional-to-required; tighten a type/constraint consumers see;
  change an existing field's meaning; add values to CLOSED enums — every ``enum`` in a released
  schema, including ``status.state``, the comparison-report mode, and ``Block.type``; change the
  ``document`` anyOf guarantee. Removed/renamed names go on a reserved list and are never reused
  with different semantics.
- ``Block.type`` is closed and stays closed, and the normalization layer is what makes that
  affordable. A backend meeting a native concept with no peer in the 22 values maps it to
  ``other`` and preserves the vendor's own label in ``native_type`` — required of every adapter
  (``pydoc openreading.adapters``), implemented as the ``.get(native, BlockType.OTHER)`` default
  in each mapping table, and stated in the field's own schema ``description``. So a new native
  concept reaches a caller today, honestly and unambiguously, with no schema change at all; the
  22 values have not moved since response v0.1 for that reason. Adding a 23rd would not be
  additive in the way a warning code is: it would RE-PARTITION ``other``, silently reclassifying
  blocks that shipped under the old value, which is the channel-semantics rule's own test. It
  would also oblige every adapter's mapping table to be revisited at once, since the vocabulary
  is normalized ACROSS backends — one backend adopting the new value while another leaves the
  same concept in ``other`` is a worse contract than no new value. A genuine need for a 23rd type
  is therefore a MAJOR, deliberately.
- Channel-semantics rule: documented invariants ARE the contract even when the shape is
  unchanged. Test — would a consumer's legitimate assertion against the old semantics fail on
  new output, or vice versa? Either direction is a breaking semantic change and needs a bump
  with the invariant ID named in the "Changed" history below. Under 0.x the MINOR slot carries
  such major-ish changes, stated explicitly, never shipped silently (Canon's C1 strengthening
  and C7 tightening are the precedent).
- A released schema file is immutable: a change means a new versioned file, never an in-place
  edit. Mechanized as sha256 pins in ``tests/test_schema_evolution.py::_FROZEN_RELEASED``; every
  vendored file must be either pinned or listed in ``_UNRELEASED``. An UNRELEASED file may be
  edited during the milestone that ships it; it is frozen at milestone end. (The pins were
  re-derived once, at the ``$id`` host rename — pre-1.0, never on PyPI, nothing had pinned the
  old ids.)
- Stability ladder: fields annotate ``x-stability: experimental | stable`` (draft 2020-12
  tolerates unknown keywords). ``experimental_fields()`` generates the registry; the compat
  meta-test in ``tests/test_schema_evolution.py`` asserts registry == annotations and excludes
  experimental fields from backward guarantees.
- Deprecation: JSON Schema ``deprecated: true`` + pydantic ``deprecated=``; using a deprecated
  feature appends a ``warnings[]`` entry; never deprecate before the replacement is shipped and
  stable; deprecated for at least one MINOR before removal; removal only at MAJOR.
- MINOR playbook: copy ``family.vX.Y.json`` to ``vX.(Y+1).json`` and edit the COPY; new const +
  pydantic default in one commit (a consistency test pins them, so the v0.1/v0.2 version-identity
  drift cannot recur); new golden fixtures; the transitive-backward suite; the manifest and the
  history below. MAJOR adds a forward-only ``migrate(instance, to_version)`` up-converter, a
  "Breaking" table naming per-field before/after and invariant IDs, the reserved-names update,
  and keeps the prior MAJOR's fixtures parseable via ``migrate()`` for one cycle.
- Packaging: every file under ``src/openreading/schemas`` ships in the wheel via hatchling's
  ``packages`` include, and ``pyproject.toml``'s ``[tool.hatch.build.targets.wheel.force-include]``
  additionally pins the early files by name — added after D-v2-6.1a found the descriptor JSON
  absent from a built wheel. Point the ``*_SCHEMA_FILE`` constant below at the new file; the
  older files stay vendored for the transitive-backward suite.
- ``backend_raw`` is outside the versioned contract; the schema version governs only the
  normalized envelope.

Milestone manifest (milestone label -> schema-file version per family)
----------------------------------------------------------------------
The labels below are milestone names, not released package versions: ``pyproject.toml`` and
``openreading.__version__`` are ``0.3.0`` and nothing has shipped to PyPI.

- 0.1.0 "The contract": request v0.1, response v0.1, adapter-descriptor v0.1.
- 0.2.0 "BYO-key": adapter-descriptor v0.2.
- 0.3.0 "Strategies": response v0.2, strategy-config v0.1.
- 0.4.0 "Compare": comparison-report v0.1.
- 0.5.0 "Canon": response v0.3, adapter-descriptor v0.3, comparison-report v0.2.
- 0.6.0 "Manifest": batch-result v0.1, corpus-report v0.1, adapter-descriptor v0.4.
- 0.7.0 "Pulse" / "Plain": liveness-report v0.1, adapter-descriptor v0.5, strategy-config v0.2.
- "One file": strategy-config v0.3 (the closed `policy` block).
- Ledger: adapter-descriptor v0.6 and v0.7, step v0.1, journal v0.1, leaderboard-report v0.1.
- Security review (M12): request v0.2.

Schema version history (oldest first)
-------------------------------------
- The contract — request v0.1, response v0.1, adapter-descriptor v0.1: the initial cut.
- BYO-key — adapter-descriptor v0.2 (additive; permissive so every v0.1 descriptor validates):
  ``credentials_spec``, ``config_spec``, ``signup_url``, ``accepts_url``, ``live_gate_env``;
  ``trains_on_customer_data`` gains ``unverified`` (vendors advertising a BAA with an
  unconfirmed no-train posture had no honest value; D-v2-9.2); ``byo_mode`` gains
  ``cloud_credential`` (Textract SigV4 keypairs and DocAI ADC are not API keys; D-v2-6.1b).
- Strategies — response v0.2 (additive over v0.1; D-v3-5): optional top-level
  ``orchestration`` object and ``pages[].source_backend``; only strategy-engaged runs populate
  them, the legacy path is byte-identical. strategy-config v0.1: new family, the complete
  grammar validated BEFORE pydantic construction (D-v3-6). ``strategy:`` is a reserved prefix
  on the free-string ``backend.id``, not a request-schema change (D-v3-2).
- Compare — comparison-report v0.1: new family, the read-only cross-backend report.
- Canon. Added: response v0.3 — ``document.confidence``, ``channel_provenance``
  (experimental), ``schema_url``, the ``page_attribution_unavailable`` warning code, and the
  const-fix for the v0.1/v0.2 version-identity drift; adapter-descriptor v0.3 — optional
  ``output.block_granularity``; comparison-report v0.2 — content-first ``headline`` and the
  ``structure`` finding code (a finding-semantics change, hence a named MINOR).
  Changed (named semantic strengthenings): C1 ``text`` strengthened to markup-free and
  complete; C7 numeric confidence tightened to [0,1] on TableCell/Page/doc_type/Citation.
- Manifest. Added: batch-result v0.1 — new family; per-item succeeded/failed/skipped with honest
  reasons, each succeeded item a full response v0.3 envelope, plus an aggregate ``summary``;
  composes the single-document contract without changing it. corpus-report v0.1 — new family;
  documents pair across runs by identity (relpath, then filename, then sha256), each pair
  carries a comparison-report v0.2, plus a verdict rollup; a separate family, NOT a
  comparison-report mode (that enum is CLOSED). adapter-descriptor v0.4 — optional ``batch``
  block (additive). Unchanged: request v0.1, response v0.3.
- Plain — strategy-config v0.2 (additive over v0.1; config ``version`` const stays 1): the
  simple dialect's body grammar (``plain_try``/``race``/``compare_body``) and the
  ``disagreement_over`` gate predicate. A new file only because v0.1 is byte-frozen.
- Pulse. Added: liveness-report v0.1 — new family; a seven-state ladder (``not_configured`` ->
  ``configured_unverified`` -> ``live``, with ``unreachable``/``unauthorized``/``error`` as the
  measured negatives and ``not_supported`` as the floor), a redundant ``measured`` boolean so a
  third-party frontend can tell measurement from inference without encoding this enum (D-v7-3:
  rendering an inference as ``live`` is the "configured wearing the word ready" defect the
  family exists to fix), ``probe`` kind, ``latency_ms`` (null when not measured), redacted
  ``detail``, ``checked_at``; returned by ``POST /v1/backends/{id}/liveness``.
  adapter-descriptor v0.5 — optional ``liveness`` block (additive). Unchanged: request v0.1,
  response v0.3; liveness is a control-plane family and never routing input (D-v7-6).
- Leaderboard — leaderboard-report v0.1: new family; N registered backends ranked on ONE
  ``evals.dataset`` ``case.json`` corpus via the unchanged ``evals.runner.run_case`` path.
- Ledger defect fixes — adapter-descriptor v0.6 (additive over v0.5): ``idempotency_supported``
  and ``cancel_supported`` (both default true), set honestly per adapter against each vendor's
  real API docs rather than assumed; one bump for both defects so the schema is not bumped twice
  for two fields of the same kind. Unchanged: request v0.1, response v0.3 (the real vendor
  cancel call for four adapters is scoped to ``cancel()``, which has no wire shape).
- Ledger T1 — step v0.1 (``StepRequest``/``StepResult``, the contract every executor
  implements) and journal v0.1 (the per-line shape of a run's JSONL journal): two new families.
- Ledger T4a — adapter-descriptor v0.7 (additive over v0.6): optional ``protocol_version``
  integer; the pydantic model requires it so the registry can refuse an adapter still on the
  older contract.
- Security review (M12) — request v0.2 (Changed, a named strengthening, not merely additive):
  ``additionalProperties: false`` now closes every nested object node, not only the top level
  (document, backend, backend.runtime, outputs, outputs.chunking, extraction_schema, features,
  pages, pages.ranges[], routing, compliance, async), matching the pydantic mirrors'
  ``extra="forbid"``. Before this, a misspelled nested field such as ``document.mim_type`` passed
  schema validation and only failed later, at the pydantic layer, contradicting "schemas are the
  source of truth" (AGENTS.md). ``extraction_schema.json_schema``'s VALUE is deliberately excluded
  — it is an arbitrary caller-supplied JSON Schema, not a field of this contract. Under 0.x this
  rides the MINOR slot per the channel-semantics rule (named here, not shipped silently); a
  caller who was relying on an unknown nested key being silently ignored is the only one affected,
  and no such caller could pass pydantic construction anyway. request.v0.1.json is unchanged.
"""

from __future__ import annotations

import json
import sys
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

# v0.2 (Security review, M12): additionalProperties:false now closes every nested object node,
# not only the top level, matching the pydantic mirrors' extra="forbid" — additive+Changed over
# v0.1 (§6/§8; see "Schema version history" above for the full rationale).
REQUEST_SCHEMA_FILE = "request.v0.3.json"
# v0.3 (Canon): named channel invariants (C1-C11 in $defs descriptions), confidence bounds [0,1]
# on TableCell/Page/doc_type/Citation, + document.confidence / channel_provenance / schema_url,
# and the const-fix for the v0.1/0.2 version-identity drift. Additive+Changed over v0.2 (§6/§8).
RESPONSE_SCHEMA_FILE = "response.v0.3.json"
# v0.3 adds the optional output.block_granularity hint (§4.3); additive over v0.2.
# v0.4 (Manifest v0.6) adds the optional `batch` block (native-batch declaration); additive over
# v0.3.
# v0.5 (Pulse) adds the optional `liveness` block (liveness-probe declaration); additive over v0.4.
# v0.6 (BL-166 + BL-164) adds `idempotency_supported`/`cancel_supported` (both default true, one
# bump for both defects); additive over v0.5.
# v0.7 (Ledger T4a, AC-8) adds the optional `protocol_version` integer (optional here so this
# schema stays additive over v0.6; the pydantic AdapterDescriptor model requires it with no
# default — see that field's own comment for why the two layers deliberately diverge).
DESCRIPTOR_SCHEMA_FILE = "adapter-descriptor.v0.8.json"
# v0.3 (Strategies): the optional openreading.yaml orchestration grammar.
# v0.2 (Plain, v0.7): the simple dialect's body grammar (plain_try/race/compare_body) + the
# disagreement_over gate predicate — additive over v0.1 (config `version` const stays 1). Cut as
# a new file because v0.1 is byte-frozen (schema-evolution §8); v0.1 remains the frozen artifact.
# v0.3 (One file): `policy` becomes a closed, typed object. It was `additionalProperties:
# true` while a hand-written JSON policy file was the primary spelling and the block its superset.
# With the file the only spelling, a typo and a quoted boolean are refused here rather than by a
# validator standing in for the schema. No file that was valid and meaningful becomes invalid: a
# key outside this set was already refused, one rung later. The config `version` const stays 1.
STRATEGY_CONFIG_SCHEMA_FILE = "strategy-config.v0.4.json"
# v0.4 (Compare): the read-only cross-backend comparison report (the openreading.comparison
# docstring).
# v0.5 (Canon): the `structure` finding code + content-first `headline`; finding-
# semantics change, so a MINOR bump with the change named in the CHANGELOG (§8/§9).
COMPARISON_REPORT_SCHEMA_FILE = "comparison-report.v0.2.json"
# v0.6 (Manifest): the batch-run envelope + the corpus (batch-vs-batch) comparison report — two new
# families composing the single-document contract (internal/design/batch-intake.md §5/§8).
BATCH_RESULT_SCHEMA_FILE = "batch-result.v0.2.json"
CORPUS_REPORT_SCHEMA_FILE = "corpus-report.v0.1.json"
# BL-160 (Leaderboard): N registered backends ranked on ONE evals.dataset case.json corpus, via the
# unchanged evals.runner.run_case path — a new family, not an edit to any existing schema.
LEADERBOARD_REPORT_SCHEMA_FILE = "leaderboard-report.v0.1.json"
# Pulse (internal/design/liveness.md): one backend's liveness answer, distinguishing an INFERRED
# status (read from local config) from a MEASURED one (we contacted the backend). Also a new
# family.
LIVENESS_REPORT_SCHEMA_FILE = "liveness-report.v0.1.json"
# Ledger T1 (internal/design/ledger.md §5.4): the step contract every executor implements
# (StepRequest/StepResult) and the per-line shape of a run's JSONL journal. Two new families.
STEP_SCHEMA_FILE = "step.v0.1.json"
JOURNAL_SCHEMA_FILE = "journal.v0.1.json"
LOCAL_DOCUMENT_SCHEMA_FILE = "local-document.v0.2.json"
PASSAGE_SCHEMA_FILE = "passage.v0.2.json"
AGENT_DOCUMENT_TOOL_SCHEMA_FILE = "agent-document-tool.v0.2.json"


_PACKAGE = "openreading.schemas"


@cache
def _load(name: str) -> dict[str, Any]:
    with resources.files(_PACKAGE).joinpath(name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def request_schema() -> dict[str, Any]:
    """The vendored ``REQUEST_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(REQUEST_SCHEMA_FILE)


def response_schema() -> dict[str, Any]:
    """The vendored ``RESPONSE_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(RESPONSE_SCHEMA_FILE)


def descriptor_schema() -> dict[str, Any]:
    """The vendored ``DESCRIPTOR_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(DESCRIPTOR_SCHEMA_FILE)


def strategy_config_schema() -> dict[str, Any]:
    """The vendored ``STRATEGY_CONFIG_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(STRATEGY_CONFIG_SCHEMA_FILE)


def comparison_report_schema() -> dict[str, Any]:
    """The vendored ``COMPARISON_REPORT_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(COMPARISON_REPORT_SCHEMA_FILE)


def batch_result_schema() -> dict[str, Any]:
    """The vendored ``BATCH_RESULT_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(BATCH_RESULT_SCHEMA_FILE)


def corpus_report_schema() -> dict[str, Any]:
    """The vendored ``CORPUS_REPORT_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(CORPUS_REPORT_SCHEMA_FILE)


def leaderboard_report_schema() -> dict[str, Any]:
    """The vendored ``LEADERBOARD_REPORT_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(LEADERBOARD_REPORT_SCHEMA_FILE)


def liveness_report_schema() -> dict[str, Any]:
    """The vendored ``LIVENESS_REPORT_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(LIVENESS_REPORT_SCHEMA_FILE)


def step_schema() -> dict[str, Any]:
    """The vendored ``STEP_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(STEP_SCHEMA_FILE)


def journal_schema() -> dict[str, Any]:
    """The vendored ``JOURNAL_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(JOURNAL_SCHEMA_FILE)


def experimental_fields(schema: dict[str, Any] | None = None) -> set[str]:
    """The generated `x-stability: experimental` registry (§8).

    Walks a schema (default: the current response schema) and returns the set of field paths
    annotated ``x-stability: experimental``. The compat meta-tests exclude these from backward
    guarantees, and a meta-test asserts this generated set equals the schema annotations, so
    the two can never silently drift.
    """
    schema = response_schema() if schema is None else schema
    found: set[str] = set()

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if node.get("x-stability") == "experimental":
            found.add(path)
        for container in ("properties", "$defs"):
            for name, sub in (node.get(container) or {}).items():
                walk(sub, f"{path}.{name}" if path else name)
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}[]")
        ap = node.get("additionalProperties")
        if isinstance(ap, dict):
            walk(ap, f"{path}{{}}")

    walk(schema, "")
    return found


_VALIDATOR_CACHE: dict[int, tuple[dict[str, Any], Any]] = {}


def _validator(schema: dict[str, Any]):
    # Cached per (family, version) — i.e. per schema object identity. `_load` (above) is `@cache`d
    # per filename, so every `*_schema()` call site returns the SAME dict object for its family;
    # keying on `id(schema)` here is therefore stable for the process lifetime, and storing the
    # schema itself alongside its validator keeps that object referenced so its id can never be
    # reused by something else. Without this, `cls.check_schema(schema)` re-validated the schema
    # itself against the 2020-12 metaschema on every single call (BL-167): ~15ms per call, paid in
    # full by every request the tiniest envelope included. This correctness depends on `_load`
    # never returning a fresh object for the same filename — do not add `_load.cache_clear()` or a
    # defensive copy there without re-deriving this cache's keying strategy too.
    cached = _VALIDATOR_CACHE.get(id(schema))
    if cached is not None:
        return cached[1]
    from jsonschema.validators import validator_for

    cls = validator_for(schema)
    cls.check_schema(schema)  # raises if the schema itself is not a valid JSON Schema
    validator = cls(schema)
    _VALIDATOR_CACHE[id(schema)] = (schema, validator)
    return validator


def validate_request(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid request."""
    _validator(request_schema()).validate(instance)


def validate_response(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid response."""
    _validator(response_schema()).validate(instance)


def validate_descriptor(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid adapter descriptor."""
    _validator(descriptor_schema()).validate(instance)


def validate_strategy_config(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid strategy config."""
    _validator(strategy_config_schema()).validate(instance)


def validate_comparison_report(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid comparison report."""
    _validator(comparison_report_schema()).validate(instance)


def validate_batch_result(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid batch result."""
    _validator(batch_result_schema()).validate(instance)


def validate_corpus_report(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid corpus report."""
    _validator(corpus_report_schema()).validate(instance)


def validate_leaderboard_report(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid leaderboard report."""
    _validator(leaderboard_report_schema()).validate(instance)


def validate_liveness_report(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid liveness report."""
    _validator(liveness_report_schema()).validate(instance)


def validate_step(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid step."""
    _validator(step_schema()).validate(instance)


def validate_journal_record(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid journal record."""
    _validator(journal_schema()).validate(instance)


def local_document_schema() -> dict[str, Any]:
    """The retained source and extraction identity contract."""
    return _load(LOCAL_DOCUMENT_SCHEMA_FILE)


def passage_schema() -> dict[str, Any]:
    """Exact source spans and physical page provenance."""
    return _load(PASSAGE_SCHEMA_FILE)


def agent_document_tool_schema() -> dict[str, Any]:
    """Bounded import, search, read, and error payloads."""
    return _load(AGENT_DOCUMENT_TOOL_SCHEMA_FILE)


def _cli_validate() -> int:
    """Validate every vendored schema, then validate any stored normalized fixture.

    Each vendored ``*.json`` file must itself be a valid 2020-12 JSON Schema. Any file at
    ``tests/fixtures/<slug>/normalized/*.json`` must then validate against the response schema.
    No adapter ships fixtures in that layout today, so the second line reports ``0 checked``.
    The raw fixtures at ``tests/fixtures/<slug>/*.json`` are vendor payloads rather than
    response envelopes, so this sweep deliberately leaves them alone.
    """
    # 1. schemas are themselves valid JSON Schema
    _validator(request_schema())
    _validator(response_schema())
    _validator(descriptor_schema())
    _validator(strategy_config_schema())
    _validator(comparison_report_schema())
    _validator(batch_result_schema())
    _validator(corpus_report_schema())
    _validator(leaderboard_report_schema())
    _validator(liveness_report_schema())
    _validator(step_schema())
    _validator(journal_schema())
    for contract in (local_document_schema(), passage_schema(), agent_document_tool_schema()):
        _validator(contract)
    print(
        f"schemas: {REQUEST_SCHEMA_FILE} OK, {RESPONSE_SCHEMA_FILE} OK, "
        f"{DESCRIPTOR_SCHEMA_FILE} OK, {STRATEGY_CONFIG_SCHEMA_FILE} OK, "
        f"{COMPARISON_REPORT_SCHEMA_FILE} OK, {BATCH_RESULT_SCHEMA_FILE} OK, "
        f"{CORPUS_REPORT_SCHEMA_FILE} OK, {LEADERBOARD_REPORT_SCHEMA_FILE} OK, "
        f"{LIVENESS_REPORT_SCHEMA_FILE} OK, {STEP_SCHEMA_FILE} OK, {JOURNAL_SCHEMA_FILE} OK, "
        f"{LOCAL_DOCUMENT_SCHEMA_FILE} OK, {PASSAGE_SCHEMA_FILE} OK, "
        f"{AGENT_DOCUMENT_TOOL_SCHEMA_FILE} OK"
    )

    # 2. any stored normalized-response fixtures validate against the response schema
    root = Path(__file__).resolve().parents[3]  # repo root
    fixtures = sorted((root / "tests" / "fixtures").glob("*/normalized/*.json"))
    rv = _validator(response_schema())
    failures = 0
    for f in fixtures:
        try:
            rv.validate(json.loads(f.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 — report, don't abort the sweep
            failures += 1
            print(f"FIXTURE INVALID: {f.relative_to(root)}\n    {exc}", file=sys.stderr)
    print(f"fixtures: {len(fixtures)} checked, {failures} invalid")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m openreading.schemas validate``. Exit 0 when every schema and
    fixture validates, 1 when a fixture fails, 2 on a usage error.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "validate":
        return _cli_validate()
    print("usage: python -m openreading.schemas validate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
