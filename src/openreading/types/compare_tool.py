"""Compare retained normalized responses without granting paths or backend execution.

The compare-tool.v0.1 request accepts ordered retained identifiers and an optional baseline.
A baseline outside the list becomes another authorized subject, as in the shared comparison API.
For example, two orr1 inputs can use a third retained result as their reference baseline.
Repeated identifiers remain separate subjects; baseline selection uses the first occurrence.
Truth scoring and corpus comparison require additional input contracts and are not exposed here.
"""

from typing import Annotated, Literal

from pydantic import Field

from openreading.artifacts.models import ErrorEnvelope, WireModel
from openreading.artifacts.result_models import InputResultId, ResultFailure, ResultReceipt


class CompareRequest(WireModel):
    result_ids: list[InputResultId] = Field(min_length=2)
    baseline: InputResultId | None = None


class CompareReceipt(ResultReceipt):
    kind: Literal["comparison_report"] = "comparison_report"


CompareErrorCode = Literal["invalid_comparison", "comparison_failed"]
_MESSAGES: dict[CompareErrorCode, str] = {
    "invalid_comparison": "Comparison requires readable normalized responses with distinct subject labels.",
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
    Field(title="OpenReading Compare Tool v0.1"),
]
