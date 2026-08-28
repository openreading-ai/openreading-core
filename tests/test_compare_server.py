"""M3c — POST /v1/compare (H7). Pure endpoint: compares response envelopes, never runs a backend.
Offline via FastAPI's TestClient (no socket)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="server extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from openreading import schemas  # noqa: E402
from openreading.server import create_app  # noqa: E402
from tests.fakes import make_envelope  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


def test_compare_two_responses(client):
    body = {
        "responses": [
            make_envelope("a", fields={"Total": "$5"}),
            make_envelope("b", fields={"Total": "$9"}),
        ]
    }
    r = client.post("/v1/compare", json=body)
    assert r.status_code == 200
    report = r.json()
    schemas.validate_comparison_report(report)
    assert report["mode"] == "pairwise"
    assert any(f["code"] == "field_value_conflict" for f in report["findings"])


def test_compare_with_baseline_and_truth(client):
    body = {
        "responses": [
            make_envelope("a", fields={"Total": "$5"}),
            make_envelope("b", fields={"Total": "$5"}),
        ],
        "baseline": "a",
        "truth": {"typed_fields": {"Total": "$5"}},
    }
    report = client.post("/v1/compare", json=body).json()
    assert report["baseline"]["baseline"] == "a"
    assert "truth" in report


def test_compare_missing_responses_is_400(client):
    assert client.post("/v1/compare", json={"nope": 1}).status_code == 400


def test_compare_fewer_than_two_is_400(client):
    body = {"responses": [make_envelope("a")]}
    assert client.post("/v1/compare", json=body).status_code == 400


def test_compare_invalid_response_is_400(client):
    body = {"responses": [make_envelope("a"), {"not": "a response"}]}
    assert client.post("/v1/compare", json=body).status_code == 400
