"""Compare retained responses or batches without granting paths or backend execution.

The compare-tool.v0.2 request accepts ordered retained identifiers and an optional baseline.
A baseline outside the list becomes another authorized subject, as in the shared comparison API.
For example, two orr1 inputs can use a third retained result as their reference baseline.
Repeated identifiers remain separate subjects; baseline selection uses the first occurrence.
Inline truth supplies expected values to the shared scorer and remains an unverified caller assertion.
For example, truth={"text": "expected"} scores text without reading a truth file.
An empty truth object preserves an unscored result rather than claiming perfect accuracy.
All-batch inputs select corpus comparison, which refuses baseline and truth, including an empty object.
Mixed input kinds and report inputs refuse before comparison. No source or truth path is accepted.
"""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from openreading.artifacts.models import ErrorEnvelope, WireModel
from openreading.artifacts.result_models import InputResultId, ResultFailure, ResultReceipt


class CompareRequest(WireModel):
    result_ids: list[InputResultId] = Field(min_length=2)
    baseline: InputResultId | None = None
    truth: dict[str, JsonValue] | None = Field(
        default=None,
        description=(
            "Inline expected values for the shared scorer, supplied by the caller, not verified truth. "
            'For example, {"text": "expected text"}. An empty object scores no dimensions. '
            "Values are retained verbatim with the report. Corpus inputs refuse truth and baseline."
        ),
    )


class CompareReceipt(ResultReceipt):
    kind: Literal["comparison_report", "corpus_report"] = "comparison_report"


CompareErrorCode = Literal["invalid_comparison", "comparison_failed"]
_MESSAGES: dict[CompareErrorCode, str] = {
    "invalid_comparison": "Comparison requires only normalized inputs or only batches. Batches refuse truth and baseline.",
    "comparison_failed": "Comparison failed before a report was retained.",
}


class CompareFault(WireModel):
    code: CompareErrorCode
    message: str
    retryable: Literal[False] = False


class CompareFailure(WireModel):
    schema_version: Literal["0.1"] = "0.1"
    error: CompareFault


class CompareError(Exception):
    def __init__(self, code: CompareErrorCode):
        self.code: CompareErrorCode = code
        super().__init__(code)

    def wire(self) -> dict:
        return CompareFailure(
            error=CompareFault(code=self.code, message=_MESSAGES[self.code])
        ).wire()


ComparePayload = Annotated[
    CompareRequest | CompareReceipt | CompareFailure | ResultFailure | ErrorEnvelope,
    Field(title="OpenReading Compare Tool v0.2"),
]
