"""Azure AI Document Intelligence adapter (optional extra `azure-document-intelligence`)."""

from __future__ import annotations

from openreading.adapters.azure_document_intelligence.adapter import (
    AzureDIClient,
    AzureDocumentIntelligenceAdapter,
    PollResp,
)

__all__ = ["AzureDocumentIntelligenceAdapter", "AzureDIClient", "PollResp"]
