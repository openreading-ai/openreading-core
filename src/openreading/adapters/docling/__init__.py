"""Docling adapter (optional extra `docling`; talks to a self-hosted docling-serve container)."""

from __future__ import annotations

from openreading.adapters.docling.adapter import DoclingAdapter, DoclingClient

__all__ = ["DoclingAdapter", "DoclingClient"]
