"""LiteParse: LlamaIndex's open-source local parser, run in core's supervised worker.

LiteParse is not LlamaParse. This package runs the local `liteparse` wheel on this machine,
while `openreading.adapters.llamaparse` calls the hosted service. The adapter module docstring
records the flow, the OCR asset gate, and the channel posture.
"""

from openreading.adapters.liteparse import assets, worker
from openreading.adapters.liteparse.adapter import (
    LiteParseAdapter,
    LiteParseRunner,
    LiteParseWorkerError,
    WorkerRunner,
)

__all__ = [
    "LiteParseAdapter",
    "LiteParseRunner",
    "LiteParseWorkerError",
    "WorkerRunner",
    "assets",
    "worker",
]
