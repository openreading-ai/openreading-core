"""Return static descriptors only for the backend selected by trusted setup.

Discovery never constructs an execution context or resolves environment credentials.
It performs no dependency, asset or liveness check. A configured backend can still
fail before extraction, for example when its model files are absent.
The response preserves dated descriptor sources rather than upgrading vendor claims.
Its 65536-byte JSON payload ceiling limits discovery, never document processing.
"""

from openreading.adapters.registry import make_adapter
from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import json_bytes
from openreading.types.backend_discovery import BackendDiscovery

MAX_DISCOVERY_BYTES = 65536


def describe_backends(config: ProfileConfig) -> BackendDiscovery:
    """Describe the configured adapter without widening the profile's backend set."""
    backend = "docling_local" if config.docling is not None else "pymupdf"
    result = BackendDiscovery(
        ocr_enabled=config.docling.ocr if config.docling is not None else False,
        backends=[make_adapter(backend).descriptor],
    )
    if len(json_bytes(result.wire())) > MAX_DISCOVERY_BYTES:
        raise ArtifactError("response_too_large")
    return result
