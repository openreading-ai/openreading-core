"""open-ocr adapter (optional extra `open-ocr`; BYO API key; sync inline + async poll/webhook)."""

from __future__ import annotations

from openreading.adapters.open_ocr.adapter import OpenOCRAdapter, OpenOCRClient

__all__ = ["OpenOCRAdapter", "OpenOCRClient"]
