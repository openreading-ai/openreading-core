"""Pulse adapter (optional extra `pulse`; BYO x-api-key; sync + async poll; dual response shape)."""

from __future__ import annotations

from openreading.adapters.pulse.adapter import PulseAdapter, PulseClient

__all__ = ["PulseAdapter", "PulseClient"]
