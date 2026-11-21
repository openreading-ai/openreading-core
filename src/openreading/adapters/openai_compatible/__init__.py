"""The `openai-compatible` provider-profile kind: any endpoint that speaks the OpenAI chat API.

A provider profile is a named backend you define in `openreading.yaml` instead of in code, and
`openreading.config` owns its grammar. This package turns one validated profile into one adapter
instance. `openreading.adapters.registry.install_profiles` is the only place that does so.
"""

from __future__ import annotations

from openreading.adapters.openai_compatible.adapter import (
    OpenAICompatibleAdapter,
    OpenAICompatibleClient,
    ProviderProfile,
)

__all__ = ["OpenAICompatibleAdapter", "OpenAICompatibleClient", "ProviderProfile"]
