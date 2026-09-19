"""Compare authorized retained responses and publish the shared engine's report.

Inputs are or1 artifacts, orr1 normalized responses, or all-batch orr1 inputs under the existing input grant.
The existing loaders verify integrity before comparing; no model-supplied filesystem path is read.
An external baseline identifier is loaded through the same boundary and appended once.
Subject labels come from comparison.ingest, including its repeated-backend version disambiguation.
For example, synthetic and synthetic (2) map separately to their retained input identifiers.
Duplicate labels fail closed because a report map cannot represent two distinct subjects under one key.

Single-response reports preserve the shared engine's default source=file, meaning retained JSON input here.
That field does not identify how a parser originally acquired its document.
Provenance subjects map each report label to its retained input.
The report subjects array preserves input order; canonical JSON objects sort their keys.
The adapters map uses those same labels and the versions reported in normalized inputs.
Source hashes are inherited assertions for orr1 inputs and verified retained-copy hashes for or1 artifacts.
Verification rehashes the retained copy against its manifest; the user's original file is not rechecked.
The subject_sources map preserves each subject's hash list and names that verification basis.
An orr1 input with no asserted hashes keeps an empty list, never an inferred document identity.
The aggregate source_sha256 list follows report subject order and does not assert equal verification.
No source-equality, accuracy, winner or new physical-page citation is established by this wrapper.

Request and effective-option fingerprints bind order, baseline, expected values and the operation revision.
Core version comes from installed metadata; commit stays null because no verified commit is available here.
No credential lookup, ambient routing setup, parser execution or provider call occurs.
Comparison may read static adapter descriptors through the existing comparison engine.

The synchronous tool performs CPU work in a thread and returns only a bounded result receipt.
The receipt is measured against the real MCP envelope before atomic report publication.
Cancellation waits for thread completion and does not roll back an already published report.
A lost reply can be recovered by repeating the same request with unchanged inputs and implementation.
An implementation upgrade can change result identity even when the comparison payload stays identical.
No durable comparison job, computation deadline or bounded-memory claim is provided.
Inline truth passes unchanged to the shared scorer and is retained in provenance.truth.
The caller supplies expected values; neither source correctness nor the truth values are independently verified.
For example, an empty object produces overall=null, and an unsupported dimension remains unscored.
Malformed expected values can produce a sanitized comparison_failed without retaining a report.

All-batch inputs reuse comparison.corpus without accepting truth or baseline, including empty truth.
Stable run_1, run_2 labels preserve input order and duplicates and map to retained batch identifiers.
No single adapter version describes a batch; provenance.adapters records null for each run label.
Nested reports preserve their response-reported versions, and source hashes remain producer assertions.
The shared index considers succeeded items with responses and matches relpath, filename, then sha256.
The last succeeded duplicate key wins; missing keys and failure-only documents do not enter the union.
A key absent from any run is unpaired, even when the other runs contain extracted content.
These matching rules do not establish that identical filenames identify identical source documents.
The corpus report therefore covers comparable keys, not every original batch item or failure.
"""

from __future__ import annotations

import hashlib
import importlib.metadata

from mcp import types

from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import (
    AttributedResultProvenance,
    ResultContent,
    ResultKind,
    ScoredResultProvenance,
    SubjectSource,
)
from openreading.artifacts.results import RetainedResults
from openreading.comparison import CompareInputError, load_subjects
from openreading.comparison.corpus import corpus_pairs, corpus_report_dict
from openreading.comparison.report import build_report
from openreading.mcp_server.delivery import response_bytes, tool_result, validate_delivery_config
from openreading.types.compare_tool import CompareError, CompareRequest


def compare_results(
    store: RetainedResults,
    request: CompareRequest,
    *,
    budget: int,
    request_id: str | int,
) -> types.CallToolResult:
    """Retain a comparison report only after every input and the receipt budget validate."""
    validate_delivery_config(budget, None)
    identifiers = list(request.result_ids)
    if request.baseline is not None and request.baseline not in identifiers:
        identifiers.append(request.baseline)
    payloads, sources, kinds = [], [], []
    for identifier in identifiers:
        if identifier.startswith("or1_"):
            manifest, _, payload = store.store.load_document(identifier)
            kind = "normalized_response"
            source = SubjectSource(
                source_sha256=[manifest.document_sha256], verification="verified_source_bytes"
            )
        else:
            content = store.load(identifier)
            if content.kind not in {"normalized_response", "batch_result"}:
                raise CompareError("invalid_comparison")
            kind = content.kind
            payload = content.payload
            source = SubjectSource(
                source_sha256=content.provenance.source_sha256, verification="producer_asserted"
            )
        payloads.append(payload)
        sources.append(source)
        kinds.append(kind)
    corpus = kinds[0] == "batch_result"
    if len(set(kinds)) != 1 or (
        corpus and (request.baseline is not None or request.truth is not None)
    ):
        raise CompareError("invalid_comparison")
    index = identifiers.index(request.baseline) if request.baseline is not None else None
    result_kind: ResultKind = "corpus_report" if corpus else "comparison_report"
    try:
        if corpus:
            labels = [f"run_{i + 1}" for i in range(len(payloads))]
            report = corpus_report_dict(corpus_pairs(payloads, labels), payloads, labels)
            adapters = dict.fromkeys(labels)
        else:
            subjects = load_subjects(payloads)
            labels = [s.label for s in subjects]
            if len(set(labels)) != len(labels):
                raise CompareError("invalid_comparison")
            label = labels[index] if index is not None else None
            report = build_report(subjects, baseline_label=label, truth=request.truth)
            adapters = {s.label: s.response["backend"].get("version") for s in subjects}
    except CompareError:
        raise
    except CompareInputError:
        raise CompareError("invalid_comparison") from None
    except Exception:
        # Engine diagnostics can include extracted text. The protocol must not echo that on failure.
        raise CompareError("comparison_failed") from None
    provenance = AttributedResultProvenance(
        request_sha256=hashlib.sha256(json_bytes(request.model_dump(mode="json"))).hexdigest(),
        config_sha256=hashlib.sha256(
            json_bytes(
                {
                    "operation": "retained-comparison.v0.3",
                    "baseline_index": index,
                    "kind": result_kind,
                }
            )
        ).hexdigest(),
        core_version=importlib.metadata.version("openreading"),
        core_commit=None,
        adapters=adapters,
        source_sha256=[digest for source in sources for digest in source.source_sha256],
        subjects=dict(zip(labels, identifiers, strict=True)),
        subject_sources=dict(zip(labels, sources, strict=True)),
    )
    if request.truth is not None:
        provenance = ScoredResultProvenance(
            **provenance.model_dump(mode="python"), truth=request.truth
        )
    content = ResultContent(kind=result_kind, payload=report, provenance=provenance)
    # Digests have fixed wire width, so a placeholder measures the eventual receipt exactly.
    preview = store.receipt("orr1_" + "0" * 64, content)
    if response_bytes(tool_result(preview.wire()), request_id) > budget:
        raise ArtifactError("response_too_large")
    receipt = store.publish(result_kind, report, provenance)
    return tool_result(receipt.wire())
