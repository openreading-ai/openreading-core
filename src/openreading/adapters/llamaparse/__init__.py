"""LlamaParse: LlamaIndex's hosted Parse v2 API, registered as one backend per tier.

LlamaParse is not LiteParse. This package calls the hosted service with your API key, while
`openreading.adapters.liteparse` runs the open-source parser on your own machine. The adapter
module docstring records the pinned request contract, the tiers, and the channel posture.
"""

from openreading.adapters.llamaparse.adapter import (
    TIERS,
    HttpxLlamaParseClient,
    LlamaParseAdapter,
    LlamaParseAgenticAdapter,
    LlamaParseAgenticPlusAdapter,
    LlamaParseClient,
    LlamaParseCostEffectiveAdapter,
    LlamaParseFastAdapter,
    TierProfile,
)

__all__ = [
    "TIERS",
    "HttpxLlamaParseClient",
    "LlamaParseAdapter",
    "LlamaParseAgenticAdapter",
    "LlamaParseAgenticPlusAdapter",
    "LlamaParseClient",
    "LlamaParseCostEffectiveAdapter",
    "LlamaParseFastAdapter",
    "TierProfile",
]
