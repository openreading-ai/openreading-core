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
  ``POST /v1/backends/{id}/liveness``, the web UI's "Check now" (``openreading.liveness``,
  internal/design/liveness.md §5).
- step / journal: the executor step contract and the per-line JSONL journal shape
  (internal/design/ledger.md §5.4).

Not in these schemas: the routing plan (``/v1/route``), job handles (``/v1/jobs``), backend
readiness (``/v1/backends``) and the HTTP error envelope are small purpose-built objects owned by
``openreading.server``.

Validate anything programmatically with ``validate_request`` / ``validate_response`` /
``validate_descriptor`` (each raises ``jsonschema.ValidationError``). The server validates bodies
on the way in and responses on the way out; the CLI validates before printing. Inspect any
backend's descriptor with ``make_adapter(id).descriptor.to_schema_dict()``.

Request (``request.v0.1.json``)
-------------------------------
Required: ``document`` and ``backend``; ``additionalProperties: false`` (unknown keys are
rejected — D7: deployment knobs such as ``allow_unverified_compliance`` therefore live on
``RouterConfig``, never on the wire). Optional blocks: ``schema_version`` (const ``"0.1"``),
``outputs``, ``features``, ``pages``, ``extraction_schema``, ``routing``, ``compliance``,
``async``, ``idempotency_key``.

- ``document``: exactly ONE of ``bytes_base64`` (the router may spool/upload where a backend needs
  storage, e.g. Textract's S3 flow), ``url`` (passed through to backends declaring
  ``accepts_url`` — azure-document-intelligence, chunkr, docling, open-ocr, pulse, reducto —
  downloaded to bytes first for the rest), ``path`` (local backends only), ``file_id`` (a
  previously uploaded file, e.g. a reused Chunkr task). Plus ``mime_type`` (inferred from the
  extension by CLI/Python, default ``application/pdf``), ``filename``, ``password``.
- ``backend``: ``id`` (registry slug, or ``"auto"`` for the compliance-first router), ``type``
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
- ``routing`` (for ``backend.id = "auto"``): ``doc_type_hint`` (bank_statement, paystub, w2,
  1003_loan_app, 1040_tax, invoice, id_document, medical_form, clinical_pdf, generic),
  ``optimize_for`` (accuracy | cost | latency | offline — stage-3 weights), ``fallback`` (ordered
  ids, honored WITHIN the eligible set only — routing never re-admits a compliance-dropped
  backend).
- ``compliance`` (the stage-1 filter; hard constraints, never traded off; unverified vendor
  claims fail closed): ``require_baa`` (BAA path, or fully-local where PHI never leaves),
  ``no_train_on_data`` (unconfirmed opt-outs and unverified no-train claims excluded too),
  ``data_region`` (e.g. ``"us"``, ``"eu"``; enforced against the descriptor's
  ``data_region_options``), ``require_local``, ``max_retention`` (``"zero"``, ``"48h"``). These
  also bind
  a directly named backend: a non-compliant request is refused (``ComplianceRefused`` / HTTP
  403), never silently run.
- ``async``: ``mode`` auto (default; router picks per backend and document) | sync | async;
  ``webhook_url`` (always caller-supplied; without it async polls).
- ``idempotency_key``: when omitted, defaults to a deterministic key over the document content,
  backend, resolved version and result-affecting options, so a retried submit is recognised as
  the same request. The default exists ONLY for ``bytes_base64`` / ``path`` documents; a ``url``
  or ``file_id`` document gets no default (bytes are not fetched at that stage, so nothing
  content-stable exists). That leaves URL submissions to azure-document-intelligence, chunkr,
  pulse and reducto without default retry protection, and means open-ocr's real
  ``Idempotency-Key`` header never fires for a URL source — pass the key explicitly when retry
  safety matters (tracked: internal/eng-council/FOUNDER-INBOX.md, 2026-08-22). Whether a key
  reaches the vendor at all is the descriptor's ``idempotency_supported`` (below).

Response (``response.v0.3.json``)
---------------------------------
Required: ``schema_version`` (const ``"0.3"``), ``status``, ``backend``, ``document``. The honest
common denominator is the schema's ``anyOf``: at least one of ``document.markdown``,
``document.text``, ``document.pages`` or top-level ``typed_fields`` is always present. What a
backend cannot produce is ABSENT with a ``warnings[]`` entry — never fabricated.

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
  ``cost_usd`` with ``cost_basis`` (billed/estimated/infra_only/unknown), ``duration_ms``. The
  adapter meters (``report_cost()`` projects its own counters through its pricing model) and the
  router accounts: after ``normalize()`` it fills only the ``usage`` fields the adapter left
  unset — an adapter-reported ``cost_usd`` is never overwritten, and a local backend gets
  ``infra_only`` with a null price rather than an invented one.
- ``job``: the async handle (``id``, timestamps, ``poll_url``, provider console URL).
- ``warnings[]``: ``{code, message, field}`` for anything requested but unavailable, degraded or
  noteworthy. Codes are an OPEN set; known: ``confidence_unavailable``, ``unsupported_feature``,
  ``fallback_used`` (the router's attempt trail), ``idempotent_replay``, ``baa_tier_confirmed``
  (``require_baa`` satisfied only by the deployment's tier-gated confirmation),
  ``page_attribution_unavailable``, ``quality_below_threshold`` (every rung gated, best result
  retained), ``quality_escalated`` (a rung gated and a LATER rung answered, so the walk recovered;
  its pair, ``quality_below_threshold``, says the walk did not), ``budget_exhausted`` (the time
  budget, not a gate, ended a strategy walk — the two are separate codes because escalating is
  right for the first and wrong for the second), coordinate-conversion notes. Those three are the
  codes the agent triage playbook branches on to separate "escalate" from "consume", so they are
  listed here rather than left to a reader's grep. This is what makes the never-fabricate rule
  practical: absence is always accounted for.
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

Nothing the backend produced is ever destroyed: normalized view + raw view + per-box native
geometry give full lineage for every response.

Channel invariants (C1-C11; schema ``$defs`` descriptions name them; hard guarantees since the
Canon milestone — the "0.5.0" row of the manifest below, a milestone label: the shipped package
version is still ``0.3.0``; each covers EVERY field of its channel's type — ``document``,
``pages[]``, ``blocks[]`` and ``chunks[]`` alike):

- C1 ``text.plain``: plain UTF-8, no HTML/markdown/LaTeX or adapter-invented notation
  (checkbox state renders as the words checked/unchecked).
- C2 ``text.complete``: everything returned as content appears in ``text`` — table rows as
  lines with tab-joined cells, captions included; pages join with a blank line. No adapter
  grades ``text`` X.
- C3 ``markdown.gfm``: parses as GFM; tables are pipe tables whenever a cell grid exists, raw
  HTML only as fallback; literal content is escaped so it cannot be re-read as markup; headings
  only from native structure signals, never invented.
- C4/C5: an X channel is never populated; an explicitly requested X channel warns.
- C6 ``channels.deliver-or-warn``: a requested N or D channel is populated, or a warning names it.
- C7 ``confidence.unit``: every numeric confidence (Block, TableCell, Page, doc_type, Citation)
  is a float in [0,1]; 0-100 sources are divided; word-level confidences aggregate by MIN (mean
  hides one garbage word); document-level signals surface as ``document.confidence``, not on
  blocks.
- C8 ``bbox.canonical``: as above.
- C9 ``page.provenance``: page numbers index the source document; page-unattributable derived
  blocks live in one synthetic ``Page(page_number=1)`` plus ``page_attribution_unavailable``, so
  "one synthetic container" is never mistaken for "a one-page document".
- C10 ``status.honest``: provider truncation/partial signals (``max_tokens``, ``length``, partial
  job states) become ``partial`` or a warning — never a bare ``succeeded``.
- C11 ``text-blocks.coherent``: when both are populated, ``document.text`` and the concatenated
  block spine clear a loose token-similarity threshold (warning-grade only).
- Tables: one ``Table``/``TableCell`` model is the only structured representation (HTML/pipe are
  projections); grid positions are true positions under merged cells; ``is_header`` comes from
  provider signals, never "row 0".

Adapter descriptor (``adapter-descriptor.v0.7.json``)
-----------------------------------------------------
A static, machine-readable declaration per adapter — the reason the router NEVER branches on
backend type: eligibility, ranking, credential resolution and the readiness UI read descriptor
fields only. Required: ``id``, ``type``, ``provisioning``, ``wait_modes``, ``capabilities``,
``cost``, ``compliance``, ``runtime``. No ``additionalProperties: false`` (so each additive bump
keeps every older descriptor valid) and no in-band version — filename + ``$id`` only.

- Identity: ``id``, ``type``, ``adapter_impl`` (http | in_process | subprocess | container),
  ``operations`` (sub-operations with distinct behaviour/cost), ``wait_modes`` (inline | poll |
  webhook).
- ``provisioning``: ``byo_mode`` (api_key, cloud_credential, pip, container, weights, endpoint —
  a coarse hint; the per-key truth is ``credentials_spec``), ``auth`` (none/api_key/sigv4/
  oauth2/entra/gcp_adc), ``billing_target`` (caller_account/caller_infra; the ``openreading``
  enum value exists but is never emitted — pure pass-through, no resale, kit-enforced).
- ``capabilities``: each of ``ocr``, ``handwriting``, ``printed_tables``, ``complex_tables``,
  ``forms_key_value``, ``layout``, ``reading_order``, ``multi_column``, ``figures_charts``,
  ``signatures``, ``classification``, ``splitting``, ``custom_schema_extraction``,
  ``vlm_based``, ``human_in_the_loop`` is ``"verified"`` (first-party live run or benchmark),
  ``"claimed"`` (vendor docs only) or ``false``; plus ``languages``, ``input_formats``,
  ``max_pages_per_request``, ``max_file_size``. Read by the router's stage-2 filter.
- ``output``: ``paradigms`` (the six raw shapes above); ``channels`` grades each response
  channel — ``markdown``, ``text``, ``blocks``, ``block_bbox``, ``block_confidence``,
  ``typed_fields``, ``table_cells`` — as N (native), D (derivable) or X (impossible); the
  conformance kit enforces C4/C5/C6 against these grades; optional ``block_granularity``
  (word | line | paragraph | section | element) so consumers and compare can reason about
  packaging differences instead of discovering them empirically.
- ``cost``: ``native_unit`` (page/credit/token/doc/gpu_second/cpu_second/subscription),
  ``usd_per_page_equiv_low``/``_high``, ``basis``, ``lossiness`` of the page-equivalence
  conversion. Feeds stage-3 cost scoring and ``usage.cost_usd``.
- ``compliance`` — facts, not marketing; the stage-1 filter treats anything unverified as
  ineligible unless the deployment explicitly allows it: ``hipaa_baa`` (``yes`` | ``tier_gated``
  — BAA only on a higher plan, eligible under ``require_baa`` only once the operator confirms
  ``baa_tier_confirmed`` (D7a: otherwise a PHI caller could be routed to a vendor with nothing
  signed) | ``no`` | ``na_local``), ``trains_on_customer_data`` (``yes`` | ``no`` | ``opt_out``
  — eligible only when the operator confirms the opt-out | ``na_local`` | ``unverified`` — fails
  closed under ``no_train_on_data`` unless ``allow_unverified_compliance``),
  ``data_region_options`` (``["*"]`` for local), ``data_retention``/``max_retention_hours``
  (unknown retention fails closed under ``max_retention``), ``runs_fully_local`` (the BAA-free
  PHI path), ``soc2``, ``gdpr``, ``pci``, ``train_opt_out_precondition``, ``zdr_flag``,
  ``phi_path_constraints``.
- ``runtime``: ``offline_capable``, ``license`` (copyleft flagged here), ``system_deps``,
  ``version_pin``, hardware/serving profile, ``sandbox``. ``router``: ``normalization_difficulty``,
  ``integration_priority`` P0-P2, ``priority_reason``. ``sources``: primary-source URLs with
  access dates backing every claim above.
- BYO-credential declaration (v0.2): ``credentials_spec[]`` (``{key, required, secret, env:
  [names in precedence order], description, example}``), ``config_spec[]`` (non-secret config:
  endpoints, model names, processor ids, regions, buckets), ``signup_url`` (where a user
  provisions credentials; surfaced in missing-credential error messages and readiness output),
  ``accepts_url``, ``live_gate_env`` (variables gating the live test; explicit for
  ambient-chain backends like
  Textract). The broker, ``openreading backends``, ``/v1/backends`` and missing-credential errors
  are all generated from these — no per-backend logic anywhere else. The kit requires them of
  every backend with ``auth != none`` OR an endpoint/container ``byo_mode`` (NOT keyed on
  ``runs_fully_local``: docling/qwen-vl are compliance-local yet still need an endpoint URL).
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
  chunkr, google-document-ai, nuextract, pulse, reducto (no vendor-side mechanism found).
- ``cancel_supported`` (v0.6, default true): true iff ``cancel()`` stops the job AT THE VENDOR,
  not merely locally. True: chunkr (only while still queued), nuextract, pulse, reducto. False:
  anthropic-claude, aws-textract, azure-document-intelligence, google-document-ai, open-ocr (no
  vendor cancel API, or inline-only dispatch that never holds a live job). Local/self-hosted
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
  (const ``"0.1"``) with the newest as the documented default (making it required would be
  optional-to-required = MAJOR, deferred); adapter-descriptor, step and journal have none;
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
  experimental fields from backward guarantees. Designed, not shipped: the spec also asks
  compliance code to consult the registry so ONLY stable fields drive compliance decisions;
  ``openreading.router.compliance`` never reads ``experimental_fields()`` or ``x-stability`` —
  it reads descriptor ``compliance`` facts only (none of which are experimental today).
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
- Ledger: adapter-descriptor v0.6 and v0.7, step v0.1, journal v0.1, leaderboard-report v0.1.

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
  integer; the pydantic model requires it so the registry can refuse pre-T4a adapters.
"""

from __future__ import annotations

import json
import sys
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

REQUEST_SCHEMA_FILE = "request.v0.1.json"
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
DESCRIPTOR_SCHEMA_FILE = "adapter-descriptor.v0.7.json"
# v0.3 (Strategies): the optional openreading.yaml orchestration grammar.
# v0.2 (Plain, v0.7): the simple dialect's body grammar (plain_try/race/compare_body) + the
# disagreement_over gate predicate — additive over v0.1 (config `version` const stays 1). Cut as
# a new file because v0.1 is byte-frozen (schema-evolution §8); v0.1 remains the frozen artifact.
STRATEGY_CONFIG_SCHEMA_FILE = "strategy-config.v0.2.json"
# v0.4 (Compare): the read-only cross-backend comparison report (the openreading.comparison
# docstring).
# v0.5 (Canon tranche 2): the `structure` finding code + content-first `headline`; finding-
# semantics change, so a MINOR bump with the change named in the CHANGELOG (§8/§9).
COMPARISON_REPORT_SCHEMA_FILE = "comparison-report.v0.2.json"
# v0.6 (Manifest): the batch-run envelope + the corpus (batch-vs-batch) comparison report — two new
# families composing the single-document contract (internal/design/batch-intake.md §5/§8).
BATCH_RESULT_SCHEMA_FILE = "batch-result.v0.1.json"
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


_PACKAGE = "openreading.schemas"


@cache
def _load(name: str) -> dict[str, Any]:
    with resources.files(_PACKAGE).joinpath(name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def request_schema() -> dict[str, Any]:
    return _load(REQUEST_SCHEMA_FILE)


def response_schema() -> dict[str, Any]:
    return _load(RESPONSE_SCHEMA_FILE)


def descriptor_schema() -> dict[str, Any]:
    return _load(DESCRIPTOR_SCHEMA_FILE)


def strategy_config_schema() -> dict[str, Any]:
    return _load(STRATEGY_CONFIG_SCHEMA_FILE)


def comparison_report_schema() -> dict[str, Any]:
    return _load(COMPARISON_REPORT_SCHEMA_FILE)


def batch_result_schema() -> dict[str, Any]:
    return _load(BATCH_RESULT_SCHEMA_FILE)


def corpus_report_schema() -> dict[str, Any]:
    return _load(CORPUS_REPORT_SCHEMA_FILE)


def leaderboard_report_schema() -> dict[str, Any]:
    return _load(LEADERBOARD_REPORT_SCHEMA_FILE)


def liveness_report_schema() -> dict[str, Any]:
    return _load(LIVENESS_REPORT_SCHEMA_FILE)


def step_schema() -> dict[str, Any]:
    return _load(STEP_SCHEMA_FILE)


def journal_schema() -> dict[str, Any]:
    return _load(JOURNAL_SCHEMA_FILE)


def experimental_fields(schema: dict[str, Any] | None = None) -> set[str]:
    """The generated `x-stability: experimental` registry (§8).

    Walks a schema (default: the current response schema) and returns the set of field paths
    annotated ``x-stability: experimental``. The compat meta-tests exclude these from backward
    guarantees, and a meta-test asserts this generated set equals the schema annotations, so
    the two can never silently drift. Designed, not shipped: the spec also has compliance code
    consult this registry so ONLY stable fields drive compliance decisions;
    ``openreading.router.compliance`` does not call it.
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
    _validator(request_schema()).validate(instance)


def validate_response(instance: dict[str, Any]) -> None:
    _validator(response_schema()).validate(instance)


def validate_descriptor(instance: dict[str, Any]) -> None:
    _validator(descriptor_schema()).validate(instance)


def validate_strategy_config(instance: dict[str, Any]) -> None:
    _validator(strategy_config_schema()).validate(instance)


def validate_comparison_report(instance: dict[str, Any]) -> None:
    _validator(comparison_report_schema()).validate(instance)


def validate_batch_result(instance: dict[str, Any]) -> None:
    _validator(batch_result_schema()).validate(instance)


def validate_corpus_report(instance: dict[str, Any]) -> None:
    _validator(corpus_report_schema()).validate(instance)


def validate_leaderboard_report(instance: dict[str, Any]) -> None:
    _validator(leaderboard_report_schema()).validate(instance)


def validate_liveness_report(instance: dict[str, Any]) -> None:
    _validator(liveness_report_schema()).validate(instance)


def validate_step(instance: dict[str, Any]) -> None:
    _validator(step_schema()).validate(instance)


def validate_journal_record(instance: dict[str, Any]) -> None:
    _validator(journal_schema()).validate(instance)


def _cli_validate() -> int:
    """Check both schemas are valid, then validate every stored normalized fixture.

    Fixtures live at tests/fixtures/<slug>/normalized/*.json (created per-adapter from
    Phase 1). With none present yet, this validates only the schemas — enough to keep
    `make verify` honest before adapters exist.
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
    print(
        f"schemas: {REQUEST_SCHEMA_FILE} OK, {RESPONSE_SCHEMA_FILE} OK, "
        f"{DESCRIPTOR_SCHEMA_FILE} OK, {STRATEGY_CONFIG_SCHEMA_FILE} OK, "
        f"{COMPARISON_REPORT_SCHEMA_FILE} OK, {BATCH_RESULT_SCHEMA_FILE} OK, "
        f"{CORPUS_REPORT_SCHEMA_FILE} OK, {LEADERBOARD_REPORT_SCHEMA_FILE} OK, "
        f"{LIVENESS_REPORT_SCHEMA_FILE} OK, {STEP_SCHEMA_FILE} OK, {JOURNAL_SCHEMA_FILE} OK"
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
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "validate":
        return _cli_validate()
    print("usage: python -m openreading.schemas validate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
