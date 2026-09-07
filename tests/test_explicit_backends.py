"""The caller names the backends, and nothing else decides.

+ , landed as one change because
every intermediate state is a repository that lies in a new way.

Gone: the compliance filter and its 180 vendor claims, `optimize_for` and the stage-3 scorer, the
capability gate, and `auto`. Arrived in the same commit: `policy.backends`, so no caller is ever
left without a way to bound the backend set.

Selection is a lookup with no inference in it:

    1. the backend the caller named        -> a chain of one
    2. else policy.backends, in order      -> that is the chain
    3. else pymupdf                        -> needs no key and no config, so it cannot fail on setup
"""

from __future__ import annotations

import pytest

from openreading import api


@pytest.fixture
def sample_pdf(tmp_path):
    from openreading.testing.sample_pdf import build_sample_pdf

    path = tmp_path / "sample.pdf"
    path.write_bytes(build_sample_pdf())
    return path


# ---- the replacement, which must exist before anything is taken away ---------------------------


def test_policy_backends_is_the_chain_in_written_order(tmp_path, sample_pdf):
    cfg = {"version": 1, "policy": {"backends": ["tesseract", "pymupdf"]}}
    plan = api.route(str(sample_pdf), config=cfg)
    assert [plan.chosen.descriptor.id, *[a.descriptor.id for a in plan.fallbacks]] == [
        "tesseract",
        "pymupdf",
    ]


def test_reordering_the_list_reorders_the_chain(tmp_path, sample_pdf):
    """Preference order is the caller's, not a score. A caller who measured latency on their own
    documents beats a weight table core computed from data it never verified."""
    first = api.route(
        str(sample_pdf), config={"version": 1, "policy": {"backends": ["pymupdf", "tesseract"]}}
    )
    second = api.route(
        str(sample_pdf), config={"version": 1, "policy": {"backends": ["tesseract", "pymupdf"]}}
    )
    assert first.chosen.descriptor.id == "pymupdf"
    assert second.chosen.descriptor.id == "tesseract"


def test_no_config_falls_back_to_pymupdf(sample_pdf):
    """Rule 3, and the first five minutes. pymupdf has zero credentials and zero config fields, so
    a fresh clone with no `.env` and no `openreading.yaml` still reads a document."""
    plan = api.route(str(sample_pdf))
    assert plan.chosen.descriptor.id == "pymupdf"


def test_an_empty_backends_list_permits_nothing(sample_pdf):
    from openreading.types.errors import ScopeRefused

    cfg = {"version": 1, "policy": {"backends": []}}
    with pytest.raises(ScopeRefused):
        api.run(str(sample_pdf), config=cfg)


# ---- what left ---------------------------------------------------------------------------------


def test_auto_is_not_a_backend_id(sample_pdf):
    """`auto` asked core to infer from vendor claims it could not verify. It names no registered
    backend now, so a request for it is refused rather than quietly running something else."""
    from openreading.types.errors import ScopeRefused

    with pytest.raises((KeyError, ScopeRefused)):
        api.run(str(sample_pdf), backend="auto")


def test_a_descriptor_carries_no_vendor_compliance_claim():
    """180 claims across 15 adapters, none of which this repository could verify."""
    from openreading.adapters.registry import BUILTIN_ADAPTERS, make_adapter

    for slug in BUILTIN_ADAPTERS:
        assert not hasattr(make_adapter(slug).descriptor, "compliance")


def test_a_request_carrying_a_compliance_block_is_refused():
    import pydantic

    from openreading.types.request import OpenReadingRequest

    with pytest.raises(pydantic.ValidationError):
        OpenReadingRequest.model_validate(
            {
                "document": {"bytes_base64": "eA=="},
                "backend": {"id": "pymupdf"},
                "compliance": {"require_local": True},
            }
        )


def test_optimize_for_is_refused():
    """Four documented values, of which `latency` read no latency figure because none existed and
    `accuracy` ranked by this project's own build priority. Refused, not ignored."""
    import pydantic

    from openreading.types.request import OpenReadingRequest

    with pytest.raises(pydantic.ValidationError):
        OpenReadingRequest.model_validate(
            {
                "document": {"bytes_base64": "eA=="},
                "backend": {"id": "pymupdf"},
                "routing": {"optimize_for": "accuracy"},
            }
        )


def test_nothing_is_dropped_for_a_reason_the_caller_did_not_state(sample_pdf):
    """`dropped` survives, but every entry now names something the CALLER said. The nine
    compliance codes and the capability gate's `missing_<cap>` are unreachable, so a plan the
    caller did not restrict drops nobody."""
    plan = api.route(str(sample_pdf))
    assert plan.dropped == {}


def test_asking_for_signatures_drops_nobody(sample_pdf):
    """Measured before this change: thirteen backends dropped, Azure among them, which ships
    signature detection. The exclusion came from a `False` typed into this repository."""
    from openreading.types.request import Features

    req = api.build_request(str(sample_pdf), "pymupdf")
    req = req.model_copy(update={"features": Features(signatures=True)})
    from openreading.router.compliance import RouterConfig
    from openreading.router.router import Router

    plan = Router(api.build_registry(), RouterConfig()).route(req)
    assert plan.chosen is not None, "asking for a feature no longer empties the chain"


def test_the_scorer_and_its_inputs_are_gone():
    from openreading.router import router as r

    assert not hasattr(r, "_score")
    assert not hasattr(r, "_WEIGHTS")
    assert not hasattr(r, "_QUALITY_BY_PRIORITY")
    assert not hasattr(r, "_truthy_cap")


def test_compliance_refused_is_gone_and_scope_refused_remains():
    from openreading.types import errors

    assert not hasattr(errors, "ComplianceRefused")
    assert hasattr(errors, "ScopeRefused"), "a caller-declared allow-list is still a caller's law"
