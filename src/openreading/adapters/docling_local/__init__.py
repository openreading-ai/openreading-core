"""Local Docling integration with explicit assets and no native imports at discovery.

The HTTP adapter remains in openreading.adapters.docling. This package owns the
CPU-only pipeline initialization needed by the local retained-document profile.
"""

from openreading.adapters.docling_local.adapter import DoclingLocalAdapter

__all__ = ["DoclingLocalAdapter"]
