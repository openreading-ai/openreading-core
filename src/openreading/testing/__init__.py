"""OpenReading testing utilities: the adapter conformance kit + reference adapters.

Shipped in the package so downstream adapter authors can gate their backend the same way
the built-in adapters are gated: `from openreading.testing import check_adapter_conformance`.
"""

from __future__ import annotations

from openreading.testing.adapters import FixtureAdapter, NullAdapter
from openreading.testing.conformance import (
    ConformanceCase,
    ConformanceError,
    ConformanceReport,
    Finding,
    check_adapter_conformance,
)
from openreading.testing.scrub import scrub_fixture

__all__ = [
    "check_adapter_conformance",
    "ConformanceCase",
    "ConformanceError",
    "ConformanceReport",
    "Finding",
    "NullAdapter",
    "FixtureAdapter",
    "scrub_fixture",
]
