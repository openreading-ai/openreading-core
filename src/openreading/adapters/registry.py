"""The built-in adapter catalog: slug → adapter class, plus a factory that builds a router
Registry with every adapter instantiated. Constructing an adapter never imports its runtime dep
(each is lazy-imported inside health/submit), so this module is import-safe with no extras
installed — the router reads only the static descriptors.
"""

from __future__ import annotations

from collections.abc import Callable

from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter
from openreading.adapters.aws_textract import AWSTextractAdapter
from openreading.adapters.azure_document_intelligence import AzureDocumentIntelligenceAdapter
from openreading.adapters.base import BackendAdapter
from openreading.adapters.chunkr import ChunkrAdapter
from openreading.adapters.docling import DoclingAdapter
from openreading.adapters.google_document_ai import GoogleDocumentAIAdapter
from openreading.adapters.google_gemini import GoogleGeminiAdapter
from openreading.adapters.mistral_ocr import MistralOCRAdapter
from openreading.adapters.nuextract import NuExtractAdapter
from openreading.adapters.open_ocr import OpenOCRAdapter
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


def make_adapter(slug: str) -> BackendAdapter:
    if slug not in BUILTIN_ADAPTERS:
        raise KeyError(f"unknown backend {slug!r}; known: {', '.join(sorted(BUILTIN_ADAPTERS))}")
    adapter = BUILTIN_ADAPTERS[slug]()
    # Ledger T4a (AC-8), fix for review F2: this is a SECOND adapter-construction entry point,
    # used directly at ~20 production call sites (including api.prepare_named_backend's common
    # named-backend path) that never go through router.registry.Registry.register(). Without this,
    # a below-floor adapter built here sails through to submit()/poll() and fails later, mid-run,
    # as a raw unlabeled error instead of being refused by name here — exactly the "fails later
    # instead of by name at registration" outcome AC-8 exists to prevent.
    check_protocol_version_floor(adapter)
    return adapter


def build_registry() -> Registry:
    """A Registry with all built-in adapters registered (descriptors only — nothing runs)."""
    reg = Registry()
    for slug in BUILTIN_ADAPTERS:
        reg.register(make_adapter(slug))
    return reg
