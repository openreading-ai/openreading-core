"""The four-category error taxonomy (adapter_interface.md §1.3).

The router does four different things with failures, so every adapter maps its backend's
errors onto exactly these four. `ScopeRefused` is raised by the router BEFORE submit();
the other three come from inside submit()/poll()/normalize().
"""

from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """Base. Carries the backend's own code verbatim for response.status.error.backend_code."""

    def __init__(self, message: str = "", *, backend_code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.backend_code = backend_code
        # Set by readiness.attach_auth_hint at the execution boundary when backend_code is
        # `auth_rejected`: the actionable, key-free "check <VAR>" sentence that becomes this
        # error's message. Also the idempotence flag — only the innermost boundary stamps it.
        self.auth_hint: str | None = None

    # ---- serialization (Ledger T4a, R3/AC-6) ----------------------------------------------
    #
    # `Job.error` is this taxonomy, not JSON-native (`dataclasses.asdict`/a bare `json.dumps` both
    # choke on an Exception subclass). `to_dict`/`from_dict` capture enough to round-trip a
    # terminal state faithfully — class identity (so `from_dict` reconstructs the correct
    # subclass, not a generic `AdapterError`), the message, and `backend_code` at minimum, plus
    # each subclass's own extra field(s) (`retry_after`/`feature`/`constraint`). `auth_hint` is
    # deliberately NOT round-tripped: it is stamped fresh at the execution boundary
    # (readiness.attach_auth_hint) from the CURRENT run's own credentials, never persisted state.
    #
    # Dispatch is via the explicit `_TAXONOMY` table at the bottom of this module (only the four
    # classes design doc §11's retry-policy table names — RetryableError/TerminalError/
    # UnsupportedFeatureError/ScopeRefused — plus this base), NOT an `__init_subclass__`
    # auto-registry: a few further-derived subclasses below (`MissingCredentialsError` et al.)
    # have their own `__init__` that doesn't accept a bare `backend_code` kwarg, so blindly
    # reconstructing any registered subclass by name would raise on those. An unrecognized `type`
    # degrades to a plain `AdapterError` carrying the same message/backend_code rather than
    # crashing — losing exact class identity for a class this milestone doesn't require round-
    # tripping, instead of failing the round trip entirely.

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "message": self.message,
            "backend_code": self.backend_code,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AdapterError:
        target = _TAXONOMY.get(d.get("type", ""))
        if target is not None and target is not AdapterError:
            return target.from_dict(d)
        return AdapterError(d.get("message", ""), backend_code=d.get("backend_code"))


class RetryableError(AdapterError):
    """Transient. Router retries the SAME backend with backoff, honoring retry_after."""

    def __init__(
        self,
        message: str = "",
        *,
        backend_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, backend_code=backend_code)
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "retry_after": self.retry_after}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AdapterError:
        return RetryableError(
            d.get("message", ""),
            backend_code=d.get("backend_code"),
            retry_after=d.get("retry_after"),
        )


class TerminalError(AdapterError):
    """Permanent for this (document, backend). No retry of the same backend; MAY fall back."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AdapterError:
        return TerminalError(d.get("message", ""), backend_code=d.get("backend_code"))


class UnsupportedFeatureError(AdapterError):
    """A requested channel/feature the backend structurally cannot produce.

    NOT a hard failure: the adapter returns whatever it CAN and records this as a
    response.warnings[] entry (never a null/fake value). The router MAY fall back if the
    feature was declared required.
    """

    def __init__(
        self, message: str = "", *, backend_code: str | None = None, feature: str = ""
    ) -> None:
        super().__init__(message, backend_code=backend_code)
        self.feature = feature

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "feature": self.feature}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AdapterError:
        return UnsupportedFeatureError(
            d.get("message", ""), backend_code=d.get("backend_code"), feature=d.get("feature", "")
        )


class ScopeRefused(AdapterError):
    """The declared backend allow-list leaves nothing this request could run.

    403 `scope_denied` on the wire. It used to have a sibling, `ComplianceRefused`, which answered
    a different question: compliance refused because the DOCUMENT may not go to that backend,
    scope refuses because the CALLER did not permit it. Core cannot answer the first honestly, so
    only the second remains, and it is the one whose fix is in the caller's own hands.

    Every source of an allow-list intersects and none widens: the file's `policy.backends`, a
    caller's argument, and the server's API-key scope. A run with any permitted backend left is
    pruned rather than refused, so this is raised only when the intersection leaves nothing.
    `backend_code` names one backend that was denied, so the message is actionable, never the
    token, which is the secret.
    """

    def __init__(
        self, message: str = "", *, backend_code: str | None = None, constraint: str = ""
    ) -> None:
        # `constraint` names WHICH rule emptied the set, so a caller can tell "your list permitted
        # nothing" from "your list named backends this build does not carry". It defaults to the
        # generic code so the common case needs no argument.
        super().__init__(message, backend_code=backend_code or constraint or "scope_denied")
        self.constraint = constraint

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "constraint": self.constraint}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScopeRefused:
        # Its own, because the base dispatches through `_TAXONOMY` back to this class: without an
        # override the lookup would call itself forever.
        return cls(
            d.get("message", ""),
            backend_code=d.get("backend_code"),
            constraint=d.get("constraint", ""),
        )


class MissingCredentialsError(TerminalError):
    """A directly-named backend is missing required credentials/config. Carries the exact env var
    names (`missing`) so the CLI/server can tell the user precisely what to set."""

    def __init__(self, message: str = "", *, missing: list[str] | None = None) -> None:
        super().__init__(message, backend_code="missing_credentials")
        self.missing = missing or []


class UnknownStrategyError(TerminalError):
    """A request named `strategy:<name>` but no such strategy is defined in the loaded config (or
    no config is loaded). A caller error, mapped to HTTP 400 / CLI exit 2."""

    def __init__(self, message: str = "", *, name: str = "") -> None:
        super().__init__(message, backend_code="unknown_strategy")
        self.name = name


class PlanExhaustedError(TerminalError):
    """Every backend in a RoutePlan failed or was skipped (missing credentials). Terminal for the
    request; carries the full attempt trail so the caller can report why each backend didn't run."""

    def __init__(self, message: str = "", *, trail: list[dict] | None = None) -> None:
        super().__init__(message, backend_code="plan_exhausted")
        self.trail = trail or []


# Ledger T4a (R3/AC-6): the exact set `AdapterError.from_dict` dispatches on by name — design doc
# §11's own retry-policy table, plus the base. Deliberately NOT every subclass in this module (see
# `AdapterError.to_dict`'s own docstring for why `MissingCredentialsError` et al. are excluded).
_TAXONOMY: dict[str, type[AdapterError]] = {
    "AdapterError": AdapterError,
    "RetryableError": RetryableError,
    "TerminalError": TerminalError,
    "UnsupportedFeatureError": UnsupportedFeatureError,
    "ScopeRefused": ScopeRefused,
}


# --- input-resolution error (not part of the AdapterError taxonomy above) -----------------------
#
# Raised before any backend/adapter code runs, so it doesn't carry a backend_code and isn't a
# member of the four-category taxonomy the module docstring describes. Lives here (rather than in
# `batch.sources`, its original home) so `api._document_dict` can raise it too without api.py
# importing from the batch package (BL-133); `batch.sources` imports it back for its own,
# pre-existing use (a missing arg / an unmatched glob during intake expansion).


class SourceNotFoundError(OSError):
    """A source argument — a document path, or a glob pattern — named nothing that exists: a
    missing path, or a glob with no matches. An `OSError` subclass (not a plain `Exception`) so a
    caller's existing `except OSError` around a document/path read (the CLI's document-argument
    guards, `_load_policy`'s own `except (OSError, json.JSONDecodeError)` pattern) catches it
    uniformly, the same way it already catches a real `FileNotFoundError`/`PermissionError` on the
    same read — no dedicated clause required at those sites."""
