"""Google Document AI adapter (optional extra `google-document-ai`; BYO GCP)."""

from __future__ import annotations

from openreading.adapters.google_document_ai.adapter import DocAIClient, GoogleDocumentAIAdapter

__all__ = ["GoogleDocumentAIAdapter", "DocAIClient"]
