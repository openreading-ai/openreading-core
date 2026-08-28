"""`Sanitizer` — the single chokepoint every byte written to a journal or blob port passes through
(internal/design/ledger.md §9.3). It is the backstop, not the only mechanism: construction-time
exclusion (never copying `document.password`, `document.url`, `async_.webhook_url`, or a
credentials_spec secret field into a `StepRequest`'s projected `options`) is the primary defense —
this class catches anything that slips past it by scanning for the run's actual resolved secret
values, reusing `credentials.redact` (the same function the seven pre-existing hand-copied
redaction sites use, per §9.3 — a journal is the eighth site, additive, not a replacement for the
other seven; see the plan's own non-goals §2).

One instance per run (Open Questions §9 item 3, not objected by alex across two rounds): a per-run
instance is cheaper than per-step and there is no step-local state it would need.
"""

from __future__ import annotations

from openreading.credentials import redact


class Sanitizer:
    def __init__(self, secret_values: frozenset[str] = frozenset()) -> None:
        self._secret_values = secret_values

    def scrub_text(self, text: str) -> str:
        if not self._secret_values:
            return text
        return redact(text, set(self._secret_values))

    def scrub_bytes(self, data: bytes) -> bytes:
        if not self._secret_values:
            return data
        return self.scrub_text(data.decode("utf-8", errors="ignore")).encode("utf-8")
