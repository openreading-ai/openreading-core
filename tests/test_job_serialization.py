"""Ledger T4a — R3 (AC-6): `Job.to_dict()`/`Job.from_dict()` must round-trip through
`json.dumps`/`json.loads` in every state a Job can be in, including FAILED with each retry-taxonomy
error class (internal/design/ledger.md §11). None of this depends on any real adapter — `Job`'s own
shape is what's under test, not any particular backend's behavior.
"""

from __future__ import annotations

import json

import pytest

from openreading.types.enums import JobState, WaitMode
from openreading.types.errors import (
    AdapterError,
    RetryableError,
    ScopeRefused,
    TerminalError,
    UnsupportedFeatureError,
)
from openreading.types.job import Job
from openreading.types.runtime import RawResult


def _roundtrip(job: Job) -> Job:
    s = json.dumps(job.to_dict())  # R3's own literal assertion: this must not raise
    return Job.from_dict(json.loads(s))


def test_pending_job_round_trips():
    job = Job(id="j1", backend_id="b1", wait_mode=WaitMode.POLL, state=JobState.PENDING)
    back = _roundtrip(job)
    assert back == job


def test_running_job_round_trips():
    job = Job(
        id="j1",
        backend_id="b1",
        wait_mode=WaitMode.POLL,
        state=JobState.RUNNING,
        backend_job_id="vendor-123",
        poll_handle={"next_token": "abc"},
        next_poll_at=1234.5,
        attempts=2,
    )
    back = _roundtrip(job)
    assert back == job


def test_succeeded_job_round_trips():
    job = Job(
        id="j1",
        backend_id="b1",
        wait_mode=WaitMode.INLINE,
        state=JobState.SUCCEEDED,
        raw=RawResult(payload={"hello": "world"}, media_type="application/json", encoding="json"),
    )
    back = _roundtrip(job)
    assert back == job
    assert back.raw is not None and back.raw.payload == {"hello": "world"}


@pytest.mark.parametrize(
    "error",
    [
        RetryableError("slow down", backend_code="429", retry_after=2.5),
        TerminalError("nope", backend_code="bad_request"),
        UnsupportedFeatureError("no tables here", backend_code="unsupported", feature="tables"),
        ScopeRefused("no BAA", backend_code="hipaa", constraint="hipaa_baa"),
    ],
    ids=["RetryableError", "TerminalError", "UnsupportedFeatureError", "ScopeRefused"],
)
def test_failed_job_round_trips_for_every_taxonomy_class(error: AdapterError):
    job = Job(id="j1", backend_id="b1", wait_mode=WaitMode.POLL, state=JobState.FAILED, error=error)
    back = _roundtrip(job)
    assert back.state is JobState.FAILED
    assert type(back.error) is type(error)
    assert back.error is not None
    assert back.error.message == error.message
    assert back.error.backend_code == error.backend_code


def test_failed_job_preserves_each_error_classes_own_extra_field():
    retryable = RetryableError("slow down", retry_after=3.0)
    back = _roundtrip(
        Job(
            id="j1",
            backend_id="b1",
            wait_mode=WaitMode.POLL,
            state=JobState.FAILED,
            error=retryable,
        )
    )
    assert isinstance(back.error, RetryableError)
    assert back.error.retry_after == 3.0

    unsupported = UnsupportedFeatureError("nope", feature="signatures")
    back = _roundtrip(
        Job(
            id="j1",
            backend_id="b1",
            wait_mode=WaitMode.POLL,
            state=JobState.FAILED,
            error=unsupported,
        )
    )
    assert isinstance(back.error, UnsupportedFeatureError)
    assert back.error.feature == "signatures"

    refused = ScopeRefused("no BAA", constraint="hipaa_baa")
    back = _roundtrip(
        Job(id="j1", backend_id="b1", wait_mode=WaitMode.POLL, state=JobState.FAILED, error=refused)
    )
    assert isinstance(back.error, ScopeRefused)
    assert back.error.constraint == "hipaa_baa"


def test_an_unrecognized_error_class_degrades_to_a_generic_adapter_error():
    """AdapterError.from_dict's own documented fallback: a `type` name outside the four-class
    taxonomy table (e.g. MissingCredentialsError, whose own __init__ doesn't accept a bare
    backend_code kwarg) degrades to a plain AdapterError carrying the same message/backend_code,
    instead of crashing the round trip."""
    from openreading.types.errors import MissingCredentialsError

    error = MissingCredentialsError("missing key", missing=["API_KEY"])
    job = Job(id="j1", backend_id="b1", wait_mode=WaitMode.POLL, state=JobState.FAILED, error=error)
    back = _roundtrip(job)
    assert type(back.error) is AdapterError  # exact taxonomy identity not preserved, by design
    assert back.error is not None
    assert back.error.message == "missing key"
    assert back.error.backend_code == "missing_credentials"


def test_job_with_cost_hint_round_trips():
    from openreading.types.cost import infra_only

    job = Job(
        id="j1",
        backend_id="b1",
        wait_mode=WaitMode.INLINE,
        state=JobState.SUCCEEDED,
        cost_hint=infra_only("page", 3.0),
    )
    back = _roundtrip(job)
    assert back.cost_hint is not None
    assert back.cost_hint.native_quantity == 3.0
    assert back.cost_hint.billing_target == "caller_infra"
