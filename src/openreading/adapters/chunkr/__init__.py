"""Chunkr adapter (optional extra `chunkr`; BYO API key or self-hosted container; poll + webhook)."""

from __future__ import annotations

from openreading.adapters.chunkr.adapter import ChunkrAdapter, ChunkrClient

__all__ = ["ChunkrAdapter", "ChunkrClient"]
