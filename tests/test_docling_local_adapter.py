"""The local adapter preserves the unified interface and refuses remote or unsupported inputs."""

import base64

import pytest

from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext


def request(**change):
    value = {
        "document": {"bytes_base64": base64.b64encode(b"%PDF-synthetic").decode()},
        "backend": {"id": "docling_local"},
        "outputs": {"markdown": False},
    }
    value.update(change)
    return OpenReadingRequest.model_validate(value)


class Client:
    def convert(self, data):
        assert data == b"%PDF-synthetic"
        return {
            "pages": {"1": {"size": {"width": 100, "height": 200}}},
            "items": [
                {
                    "label": "text",
                    "text": "Exact source words",
                    "prov": [{"page_no": 1, "charspan": [0, 18]}],
                }
            ],
            "page_origins": {"1": "native"},
        }


def test_local_adapter_uses_inline_contract_and_physical_page_projection():
    from openreading.adapters.docling_local import DoclingLocalAdapter

    adapter = DoclingLocalAdapter(client=Client())
    req = request()
    ctx = RunContext()
    job = adapter.submit(req, ctx)
    response = adapter.normalize(job, ctx, req)
    assert response.backend.id == "docling_local"
    assert response.document.text == "Exact source words"
    assert job.state.value == "succeeded"
    assert adapter.report_cost(job).native_quantity == 1
    from openreading.schemas import validate_response

    validate_response(response.to_schema_dict())


def test_local_adapter_conformance():
    from openreading.adapters.docling_local import DoclingLocalAdapter
    from openreading.testing import ConformanceCase, check_adapter_conformance

    check_adapter_conformance(
        DoclingLocalAdapter(client=Client()),
        [ConformanceCase(request=request(), deterministic=True)],
        adapter_factory=lambda: DoclingLocalAdapter(client=Client()),
    )


@pytest.mark.parametrize(
    "document", [{"url": "https://example.invalid/private.pdf"}, {"path": "missing.pdf"}]
)
def test_input_failure_is_sanitized_and_never_downloads(document):
    from openreading.adapters.docling_local import DoclingLocalAdapter
    from openreading.types.errors import TerminalError

    with pytest.raises(TerminalError) as error:
        DoclingLocalAdapter(client=Client()).submit(request(document=document), RunContext())
    assert "private.pdf" not in str(error.value)
    assert "missing.pdf" not in str(error.value)


def test_parser_failure_does_not_echo_document_contents():
    from openreading.adapters.docling_local import DoclingLocalAdapter
    from openreading.types.errors import TerminalError

    class Broken:
        def convert(self, data):
            raise RuntimeError("planted confidential document")

    with pytest.raises(TerminalError) as error:
        DoclingLocalAdapter(client=Broken()).submit(request(), RunContext())
    assert "confidential" not in str(error.value)
