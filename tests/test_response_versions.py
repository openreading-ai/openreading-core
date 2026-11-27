"""Response v0.4 beside v0.3: the client reads both, and a retained check reaches the agent.

`validate_external_response` picks the response file by the response's own `schema_version`, so
an artifact retained under v0.3 stays readable after the upgrade. A v0.4 response with
`typed_fields` and `schema_check` comes back from document delivery with both intact.
"""

from __future__ import annotations

import copy

import jsonschema
import pytest

from openreading import schemas
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.retention import validate_external_response
from openreading.types.response import NormalizedResponse, SchemaCheck
from tests.test_artifact_document import rich_response
from tests.test_artifact_retention import complete, retain, retention  # noqa: F401 (fixture)

CHECK = {
    "schema_hash": "sha256:" + "a" * 64,
    "schema_ref": None,
    "valid": False,
    "critical_ok": False,
    "fields": [{"path": "amount", "criticality": "critical", "status": "missing"}],
    "extra": [],
    "errors": [{"path": "amount", "message": "'amount' is a required property"}],
    "formats_unchecked": [],
}


def _v04():
    response = rich_response()
    response["schema_version"] = "0.4"
    response["schema_check"] = copy.deepcopy(CHECK)
    return response


def test_the_current_response_file_is_v0_4_and_v0_3_stays_known():
    assert schemas.RESPONSE_SCHEMA_FILE == "response.v0.4.json"
    assert set(schemas.RESPONSE_SCHEMA_FILES) == {"0.3", "0.4"}
    assert NormalizedResponse.model_fields["schema_version"].default == "0.4"


def test_validate_response_reads_each_known_version_by_its_own_number():
    schemas.validate_response(rich_response())
    schemas.validate_response(_v04())
    v03_with_check = rich_response()
    v03_with_check["schema_check"] = CHECK
    with pytest.raises(jsonschema.ValidationError):
        schemas.validate_response(v03_with_check)  # v0.3 is closed and has no schema_check
    with pytest.raises(jsonschema.ValidationError):
        schemas.validate_response({**rich_response(), "schema_version": "0.2"})


def test_the_client_accepts_v0_3_and_v0_4_and_refuses_an_unknown_version():
    assert validate_external_response(rich_response()).schema_version == "0.3"
    parsed = validate_external_response(_v04())
    assert isinstance(parsed.schema_check, SchemaCheck)
    assert parsed.schema_check.fields[0].status == "missing"
    with pytest.raises(ValueError, match="Unknown response schema_version"):
        validate_external_response({**_v04(), "schema_version": "9.9"})


def test_a_schema_check_round_trips_with_its_null_ref():
    out = NormalizedResponse.model_validate(_v04()).to_schema_dict()
    assert out["schema_check"] == CHECK
    schemas.validate_response(out)


def test_a_retained_check_reaches_the_document_delivery(retention):  # noqa: F811
    response = _v04()
    receipt = retain(retention, response)
    delivered = complete(retention, receipt.artifact_id)["response"]
    assert delivered["typed_fields"] == response["typed_fields"]
    assert delivered["schema_check"] == response["schema_check"]


def test_an_artifact_retained_under_v0_3_loads_after_the_upgrade(retention):  # noqa: F811
    from openreading.artifacts.service import ArtifactService

    receipt = retain(retention, rich_response())
    reopened = ArtifactService(retention.config, identity=retention.identity)
    try:
        delivered = complete(reopened, receipt.artifact_id)["response"]
    finally:
        reopened.close()
    assert delivered["schema_version"] == "0.3"


def test_an_unknown_version_is_not_retained(retention):  # noqa: F811
    with pytest.raises(ArtifactError, match="parse_failed"):
        retain(retention, {**_v04(), "schema_version": "9.9"})
    assert list(retention.store.documents.glob("*")) == []
