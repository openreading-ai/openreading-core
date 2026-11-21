"""The built-in adapter catalog: slug → adapter class, plus a factory that builds a router
Registry with every adapter instantiated. Constructing an adapter never imports its runtime dep
(each is lazy-imported inside health/submit), so this module is import-safe with no extras
installed — the router reads only the static descriptors.

Provider profiles join the catalog at run time. A provider profile is a named backend defined in
the `providers:` block of `openreading.yaml` (grammar in `openreading.config`).
`install_profiles(mapping)` validates that block and replaces the process's profile set with it,
and each profile becomes one `openreading.adapters.openai_compatible` instance under its own id.
`known_backends()` is the catalog every surface reads: the built-ins, then the installed
profiles. `make_adapter` and `build_registry` read it too, so the router resolves a profile like
any other id and never needs to know one is a profile.

Who installs: every surface that loads `openreading.yaml` installs that file's block before it
dispatches. The CLI does it in `openreading.cli.app.main` before parsing arguments, so
`--backend` accepts a profile id. `openreading.api` does it wherever it loads the file, and
`openreading serve` does it once at startup in `create_app`. A surface that finds no file leaves
the set as it was, and a file without a `providers:` block empties it. The set is process-wide
because the file is, so two files with different profiles never run in one process at once.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter
from openreading.adapters.aws_textract import AWSTextractAdapter
from openreading.adapters.azure_document_intelligence import AzureDocumentIntelligenceAdapter
from openreading.adapters.base import BackendAdapter
from openreading.adapters.chunkr import ChunkrAdapter
from openreading.adapters.docling import DoclingAdapter
from openreading.adapters.docling_local import DoclingLocalAdapter
from openreading.adapters.google_document_ai import GoogleDocumentAIAdapter
from openreading.adapters.google_gemini import GoogleGeminiAdapter
from openreading.adapters.liteparse import LiteParseAdapter
from openreading.adapters.llamaparse import (
    LlamaParseAgenticAdapter,
    LlamaParseAgenticPlusAdapter,
    LlamaParseCostEffectiveAdapter,
    LlamaParseFastAdapter,
)
from openreading.adapters.mistral_ocr import MistralOCRAdapter
from openreading.adapters.nuextract import NuExtractAdapter
from openreading.adapters.open_ocr import OpenOCRAdapter
from openreading.adapters.openai_compatible import OpenAICompatibleAdapter, ProviderProfile
from openreading.adapters.pulse import PulseAdapter
from openreading.adapters.pymupdf import PyMuPDFAdapter
from openreading.adapters.qwen_vl import QwenVLAdapter
from openreading.adapters.reducto import ReductoAdapter
from openreading.adapters.tesseract import TesseractAdapter
from openreading.router.registry import Registry, check_protocol_version_floor

BUILTIN_ADAPTERS: dict[str, Callable[[], BackendAdapter]] = {
    "pymupdf": PyMuPDFAdapter,
    "aws-textract": AWSTextractAdapter,
    "azure-document-intelligence": AzureDocumentIntelligenceAdapter,
    "reducto": ReductoAdapter,
    "tesseract": TesseractAdapter,
    "docling": DoclingAdapter,
    "docling_local": DoclingLocalAdapter,
    "liteparse": LiteParseAdapter,
    "llamaparse-fast": LlamaParseFastAdapter,
    "llamaparse-cost-effective": LlamaParseCostEffectiveAdapter,
    "llamaparse-agentic": LlamaParseAgenticAdapter,
    "llamaparse-agentic-plus": LlamaParseAgenticPlusAdapter,
    "qwen-vl": QwenVLAdapter,
    "google-document-ai": GoogleDocumentAIAdapter,
    "google-gemini": GoogleGeminiAdapter,
    "anthropic-claude": AnthropicClaudeAdapter,
    "chunkr": ChunkrAdapter,
    "pulse": PulseAdapter,
    "nuextract": NuExtractAdapter,
    "open-ocr": OpenOCRAdapter,
    "mistral-ocr": MistralOCRAdapter,
}


# Replaced wholesale by `install_profiles`, never mutated in place, so a reader that took the
# dict before an install keeps a consistent snapshot.
_PROFILES: dict[str, ProviderProfile] = {}


def install_profiles(providers: Mapping[str, Mapping[str, Any]] | None) -> None:
    """Replace the installed provider profiles with `providers`, the `providers:` block's shape.

    The block is validated by `openreading.config.validate_providers` first, so a mapping built
    in Python gets the refusals a file gets, a built-in shadow included. `None` or `{}` removes
    every profile. Raises `openreading.config.ConfigError` and installs nothing on a bad block.
    """
    from openreading.config import validate_providers  # lazy: config reads this module lazily

    global _PROFILES
    validated = validate_providers(dict(providers) if providers is not None else None)
    _PROFILES = {
        name: ProviderProfile(
            name=name,
            base_url=p["base_url"],
            model=p["model"],
            key_env=p.get("key_env"),
        )
        for name, p in validated.items()
    }


def installed_profiles() -> dict[str, ProviderProfile]:
    """A copy of the installed provider profiles, by backend id."""
    return dict(_PROFILES)


def known_backends() -> dict[str, Callable[[], BackendAdapter]]:
    """Every backend id this process can run, mapped to a zero-argument factory: the built-ins in
    `BUILTIN_ADAPTERS` order, then the installed provider profiles. Read this, not
    `BUILTIN_ADAPTERS`, wherever the question is "which ids exist here"."""
    known: dict[str, Callable[[], BackendAdapter]] = dict(BUILTIN_ADAPTERS)
    for name, profile in _PROFILES.items():
        known[name] = partial(OpenAICompatibleAdapter, profile)
    return known


def make_adapter(slug: str) -> BackendAdapter:
    """Build one adapter by id, a built-in or an installed provider profile, then refuse it when
    its `protocol_version` is below the router's floor. An unknown id raises `KeyError`, and a
    below-floor adapter raises `ValueError`."""
    known = known_backends()
    if slug not in known:
        raise KeyError(f"unknown backend {slug!r}; known: {', '.join(sorted(known))}")
    adapter = known[slug]()
    # Ledger T4a (AC-8): this is the second adapter-construction entry point.
    # `api.prepare_named_backend` and the other direct-named paths call it without ever reaching
    # `router.registry.Registry.register()`. Without the check here,
    # a below-floor adapter built here sails through to submit()/poll() and fails later, mid-run,
    # as a raw unlabeled error instead of being refused by name here — exactly the "fails later
    # instead of by name at registration" outcome AC-8 exists to prevent.
    check_protocol_version_floor(adapter)
    return adapter


def build_registry() -> Registry:
    """A Registry with every known adapter registered, built-ins then provider profiles
    (descriptors only — nothing runs)."""
    reg = Registry()
    for slug in known_backends():
        reg.register(make_adapter(slug))
    return reg
