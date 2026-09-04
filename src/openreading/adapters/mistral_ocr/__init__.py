"""Mistral OCR adapter (optional extra `mistral-ocr`; BYO API key; sync inline `POST /v1/ocr`)."""

from __future__ import annotations

from openreading.adapters.mistral_ocr.adapter import MistralOCRAdapter, MistralOCRClient

__all__ = ["MistralOCRAdapter", "MistralOCRClient"]
