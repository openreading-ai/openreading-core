r"""Shared, deterministic derivation layer — where a ``D`` (derivable) channel grade is made true.

One unit-tested package holds every GFM, HTML, table and geometry transform an adapter needs,
so no adapter carries its own copy. Every function is pure, deterministic, and stdlib-only
(``pdf_page_count`` may use an in-tree optional dep). Adapters import ONLY from
``openreading.derive``. Outside ``adapters/`` the only importer today is
``openreading.comparison`` (the GFM cell escape
``openreading.derive.tables._esc_pipe`` for report rendering); ``evals`` imports nothing from here,
and the kit's C11 check (below) uses ``openreading.comparison.align.token_similarity`` over raw
block texts, not these projections. When a ``D`` grade says "the platform derives this channel,"
this is where that derivation lives — once. It is a package rather than an
``adapters/_derive.py`` module because it carries property tests and has importers outside
``adapters/``.

Why one shared layer (the failure it avoids)
--------------------------------------------
Mapping quality was systematically under-delivered while the conformance kit stayed green:
markdown shipped verbatim as ``text``; ``text == markdown`` byte-identical with zero derivation
work; declared-``D`` channels with no derivation written at all ("vapor grades" — routing on
those grades was routing on fiction); table content missing from ``text``; reading order
fabricated by class-grouping; pages renumbered under subsetting; UTF-8 byte offsets sliced as
code points (garbling every non-ASCII document); provider truncation normalized as
``SUCCEEDED``; ``str()``-coerced and last-writer-wins typed fields; merged cells given wrong
grid coordinates; confidences on unconverted scales. Each transform had been reimplemented (or
was needed and absent) in >= 3 adapters, and every copy had its own bug. The verdict was
execution and enforcement, not architecture: (a) named channel semantics, (b) this shared layer
so ``D`` means "a derivation exists", (c) kit enforcement of the positive direction
(deliver-or-warn), (d) a real versioning policy (``openreading.schemas``).

Grades: what N / D / X mean (``openreading.types.enums.ChannelGrade``)
----------------------------------------------------------------------
``N`` native — the backend emits the channel directly. ``D`` derivable — the platform computes
it deterministically from what the backend emits, via THIS package, and a declared ``D``
obligates that derivation to exist (no declared channel without an implementation or a warning;
the generalized lesson of an aggregator that accepted a ``detect_tables`` input with no output
slot).
``X`` impossible — no faithful way to produce it; would require fabrication, so it is never
populated and a request for it warns (C4/C5). Static grades cannot express mode-dependent
provenance (a backend whose markdown is native in one mode and derived in another), so
``Response.channel_provenance`` (``{channel: "native" | "derived"}``, experimental) carries the
per-response signal. After this design no adapter grades ``text`` X: it is ``N`` where the provider
emits plain text and ``D`` via ``md_to_text`` / ``html_to_text`` / ``table_to_text`` everywhere
else. ``table_cells`` floors at ``D`` for any backend whose payload holds a grid in any form
(HTML, pipe-markdown, cell arrays); backends with no grid at all stay honestly ``X``.

The channel contract (normative; the IDs are used in schema ``$defs`` descriptions, kit findings
and CHANGELOG "Changed" rows)
----------------------------------------------------------------------------------------------
Scope rule: an invariant naming a channel covers EVERY field of that channel's type — for ``text``
that is ``document.text``, ``pages[].text``, ``blocks[].text`` AND ``chunks[].text`` (a chunk-path
leak once slipped past a block-path fix; the kit walks all four).

- **C1 ``text.plain``** — plain UTF-8: no HTML tags, no markdown syntax (headings, emphasis,
  pipes, fences, list markers, blockquotes, image/link syntax), no LaTeX, no adapter-invented
  notation. Checkbox state renders as the words ``checked`` / ``unchecked``, never ``[X]``/``[ ]``.
- **C2 ``text.complete``** — everything the backend returned as document content appears in plain
  projection, INCLUDING table content (rows as lines, cells joined by a single tab) and figure
  captions. Page texts join with ``\n\n``; no empty-string segments.
- **C3 ``markdown.gfm``** — parses as GFM. Tables are pipe tables whenever a cell grid exists
  (native or derived), with ``|`` escaped as ``\|`` and embedded newlines as ``<br>``; raw HTML
  tables are the fallback only when no grid exists. Literal source text is escaped (``escape_md``)
  so content can never be reinterpreted as markup. Headings come only from native structure
  signals (provider heading roles, PDF outline, metadata title) — never invented. ``markdown`` may
  equal ``text`` for a structure-free backend (honest), but it must be POPULATED (C6), never
  silently ``None``.
- **C4 / C5** — an ``X`` channel is never populated; a requested ``X`` channel warns.
- **C6 ``channels.deliver-or-warn``** — a requested channel graded N or D is populated, or a
  machine-readable warning names it. The positive direction C4/C5 lacked; its absence is exactly
  why byte-identical duplicates and vapor grades shipped green.
- **C7 ``confidence.unit``** — every numeric confidence anywhere in the response (Block,
  TableCell, Page, doc_type, Citation, ``document.confidence``) is a float in [0,1]. Native
  scales are converted (÷100 for percent scales); enum labels map through documented constants;
  a qualitative native label may stay a string beside the float where the model allows
  (``TypedField.confidence``). Word-level confidences aggregate to an element by **min** (a
  usable quality floor — mean hides one garbage word). Document-level-only signals go to
  ``document.confidence`` / ``pages[].confidence`` or stay in ``backend_raw``; they are never
  smeared onto blocks.
- **C8 ``bbox.canonical``** — normalized [0,1], top-left origin, via ``to_canonical``, raw
  coordinates preserved in ``bbox_native`` (DECISIONS D6: one choke point in
  ``openreading.types.geometry``). ``bbox`` is OPTIONAL on a block (``required: ["type"]``): a
  markdown-derived, bbox-less block is legal — ``blocks`` can be ``D`` while ``block_bbox`` stays
  ``X``. Cell bboxes are kept where providers give them.
- **C9 ``page.provenance``** — where page attribution exists, ``page_number`` and ``bbox.page``
  are 1-based indices into the SOURCE document, preserved under page subsetting (requesting
  pages 3-4 reports pages 3-4, never 1-2); where the provider renumbers, its ``original_page``
  wins.
  Container rule for page-unattributable derived output: the blocks live in one synthetic
  ``Page(page_number=1)`` and the adapter emits the ``page_attribution_unavailable`` warning, which
  distinguishes "one synthetic container" from "this document has one page". ``BBox.page`` stays
  required ``>= 1``; bbox-less blocks have no page to fabricate.
- **C10 ``status.honest``** — provider truncation/partial signals (``stop_reason=max_tokens``,
  ``finish_reason=length``, partial job states) map to ``ResponseState.PARTIAL`` or a warning —
  never a bare ``SUCCEEDED``. Enforced as a per-adapter truncated-fixture test, not a kit walk.
- **C11 ``text-blocks.coherent``** — when both ``text`` and ``blocks`` are populated, token
  similarity (``openreading.comparison.align.token_similarity``) between ``document.text`` and
  the space-joined block spine is >= 0.5; skipped when either channel is absent. Permanently
  advisory: legitimate divergence (header filtering) is tolerated.
- **C12** (meta, ``openreading.schemas``) — version-identity consistency, SCOPED PER FAMILY:
  every schema file asserts filename version == ``$id`` version; the newest file of a family
  additionally asserts in-band const == pydantic default only where the family carries them
  (response and comparison-report have a required const; request asserts its optional default;
  adapter-descriptor and strategy-config have no in-band string version, so filename == ``$id``
  only). Older files' consts are not asserted, which exempts ``response.v0.2.json``'s known-bad
  ``"0.1"`` const (released, immutable) — the drift class the test exists to stop recurring.

Also contract, per channel: ``blocks`` granularity stays adapter-native and is declared via the
descriptor hint ``output.block_granularity`` (word|line|paragraph|section|element); reading order
follows document order (``order_by_position``) when the provider array groups by class.
``typed_fields`` keep native JSON types (no ``str()`` coercion), repeated names collect into a
list (never last-writer-wins), nested provider structures map to nested values (no dotted-key
flattening), and when the provider returns per-field geometry or page anchors
``TypedField.citations[]`` is populated. Citations are part of the N and D obligation. The spec
also asks that the extraction schema's declared type fill ``TypedField.type``. As shipped,
``anthropic_claude``, ``azure_document_intelligence``, ``google_document_ai`` and
``google_gemini`` populate it, and every other adapter leaves ``type`` at ``None``.
``table_cells``: one ``Table`` model is the only structured representation (HTML/pipe are
projections of it); grid coordinates are true positions under merged cells, spans recorded as
``row_span``/``col_span``, ``Table.rows`` holds the value at the span origin and ``None`` at
covered positions (consumers wanting replication expand spans themselves); ``is_header`` comes
only from provider signals (``<th>``, column/row-header kinds, pipe separator row) — never
fabricated as "row 0".
``backend_raw`` is outside the versioned contract: opaque, may change without a bump.

Function contracts (each: what it guarantees / the defect class it retires)
---------------------------------------------------------------------------
- ``md_to_text(md)`` — GFM -> plain. Emphasis stripped only in matched delimiter pairs (never
  blanket character deletion, which broke ``snake_case`` and ``3*4``); fence content exempt from
  every transform; bullets/quotes/link+image syntax removed; pipe tables -> tab-joined rows.
- ``html_to_text(html)`` — tag-strip with structure: block tags -> ``\n``, ``<tr>`` -> ``\n``,
  ``<td>``/``<th>`` -> tab, entities unescaped. A grid never collapses to one line.
- ``escape_md(text)`` — escape markdown metacharacters in literal text bound for the markdown
  channel (unescaped ``|`` and newlines in cells produced malformed GFM).
- ``cells_to_grid(cells: Iterable[GridCell]) -> Table`` — THE one occupancy-cursor
  implementation: cells in provider order ``(row, col?, row_span, col_span, text, is_header,
  bbox?)`` -> true grid coordinates under merged cells; ``col=None`` lets the cursor assign the
  column (provider enumeration indices shift under spans). Three adapters would otherwise each
  re-implement the cursor.
- ``html_table_to_table(html) -> Table | None`` — th/td + rowspan/colspan, thin wrapper over
  ``cells_to_grid`` (replaces naive tr/td accumulators).
- ``md_table_to_table(md) -> Table | None`` — pipe table -> grid; the separator row marks header.
- ``table_to_pipe_md(table) -> str`` — grid -> GFM: ``|`` -> ``\|``, ``\n`` -> ``<br>``; header
  row only from ``is_header`` (a blank header row when none — GFM requires one).
- ``table_to_text(table) -> str`` — grid -> plain: rows as lines, cells tab-joined (the C2
  projection; retires "table content absent from text").
- ``md_to_blocks(md) -> list[Block]`` — line-level GFM segmentation into typed, bbox-less blocks
  with ``reading_order`` (h1 -> TITLE, h2+ -> SECTION_HEADER, paragraph -> TEXT, fence -> CODE,
  pipe group -> TABLE + ``Table``, list -> LIST_ITEM). The caller applies the C9 container rule.
  Lifts markdown/LLM-only backends' ``blocks`` from X to D without fabricating geometry.
- ``order_by_position(blocks, keys)`` — ``keys`` is a sequence parallel to ``blocks``: the
  provider span offset when known, else ``None``. Stable sort key ``(bbox.page or inf, key if
  given else bbox.y, bbox.y, bbox.x)``; blocks with neither key nor bbox keep input order. The
  offsets are an explicit argument because ``Block`` has no span field — a pure ``list[Block]``
  function cannot see provider payloads. Retires "all tables after all paragraphs" and
  class-grouped ordering.
- ``utf8_slice(text, start, end)`` — slice by UTF-8 BYTE offsets (``encode()[s:e].decode(
  errors="replace")``); providers that report byte offsets garbled after the first multibyte
  character when sliced by code points (ASCII fixtures hid it).
- ``aggregate_confidence(word_confs) -> float | None`` — ``min()`` of members; ``None`` with no
  members (the C7 aggregation rule).
- ``pdf_page_count(data) -> int | None`` — exact page count from PDF bytes via an in-tree PDF
  library, ``None`` when unavailable/unparseable (replaces cited-pages page_count heuristics).

Enforcement (``openreading.testing.conformance``)
-------------------------------------------------
``check_adapter_conformance`` returns a ``ConformanceReport`` with ``.violations``
(raise-worthy) and ``.advisories`` rather than stopping at the first failure. That split exists
because the positive-direction invariants could not otherwise be introduced without breaking
every adapter at once. ``strict_checks`` names the invariant ids that count as violations for
one run, and it defaults to ``_DEFAULT_STRICT`` = {C1, C6}. C7 and C3 are always strict, and
C11 is permanently advisory. A downstream adapter that is not yet remediated passes
``strict_checks=frozenset()`` to demote C1 and C6 back to advisories.

C3 is a weak parse check rather than a strict GFM lint, because real backends legally embed
HTML in markdown. The C1 detector targets syntax that round-trips as parseable GFM or HTML
STRUCTURE: a GFM table separator row such as ``|---|---|``, an ATX heading line, an HTML tag
pair. A ``| a | b |`` data row with no separator is not flagged. One false positive has no
per-case suppression hook in the kit, a scanned document whose genuine text really is an ASCII
pipe table with a separator row. The only lever there is omitting ``"C1"`` from
``strict_checks``, which demotes the whole check to advisory for that run. C10 is a
per-adapter fixture obligation rather than a kit check, and the ``anthropic_claude``,
``google_gemini`` and ``qwen_vl`` tests pass ``"C10"`` in ``strict_checks`` where it is a
no-op id.

Release identity: this contract landed as response schema v0.3 + adapter-descriptor v0.3, and the
stability note written into ``response.v0.3.json`` is the pin — "invariants C1-C11 are enforced
progressively during v0.5 development and are hard guarantees as of the v0.5 package release".
The ``v0.5`` in that quoted note is the Canon milestone label rather than a package version.
The shipped package version is ``0.3.0``, and ``openreading.schemas`` carries the manifest that
maps a milestone label onto its schema files.

Descriptor re-grades follow one direction rule: honesty-restoring DOWNGRADES land immediately
(they make today's claims true with no implementation), capability-raising UPGRADES land only
with their implementation (upgrading first would deepen the lie).

Decisions (internal/decisions/DECISIONS.md)
-------------------------------------------
- **D6** — canonical bbox conversion is one choke point, ``to_canonical`` in
  ``openreading.types.geometry``, emitting [0,1] top-left AND ``bbox_native``; avoids per-adapter
  geometry drift (C8 is the kit-enforced form).
- **D13** — "never fabricate / never silently drop a channel the caller asked for" is realized
  two ways: an optional-but-unavailable channel records a ``warnings[]`` entry and returns what
  the backend can produce; a structurally impossible primary ask on a directly-named backend
  raises ``UnsupportedFeatureError``. C4/C5/C6 are the channel-level statement of that guardrail.

Related: compare is rebuilt on this contract in ``openreading.comparison`` (a block whose text is
present in the other subject's page text is a packaging difference — a ``structure`` finding,
never ``block_missed``; non-deterministic backends compare by similarity only; subjects are
labelled by bare ``backend.id``, and only a COLLIDING id pulls in ``backend.version`` as
``<id> (<ver>)`` — or ``<id>#N`` when that label is taken or the version is empty). Versioning
policy, ``x-stability``, golden fixtures and the
transitive-backward substitution rule live in ``openreading.schemas``. Per-adapter audit evidence
and phase history: internal/design/canonical-normalization.md.
"""

from __future__ import annotations

from openreading.derive.blocks import md_to_blocks, order_by_position
from openreading.derive.confidence import aggregate_confidence
from openreading.derive.geometry import utf8_slice
from openreading.derive.pages import pdf_page_count
from openreading.derive.tables import (
    GridCell,
    cells_to_grid,
    html_table_to_table,
    md_table_to_table,
    table_to_pipe_md,
    table_to_text,
)
from openreading.derive.text import escape_md, html_to_text, md_to_text

__all__ = [
    # tables
    "GridCell",
    "cells_to_grid",
    "html_table_to_table",
    "md_table_to_table",
    "table_to_pipe_md",
    "table_to_text",
    # text
    "md_to_text",
    "html_to_text",
    "escape_md",
    # blocks / order
    "md_to_blocks",
    "order_by_position",
    # utilities
    "aggregate_confidence",
    "utf8_slice",
    "pdf_page_count",
]
