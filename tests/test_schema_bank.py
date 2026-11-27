"""The schema bank: entries, name resolution (SX6 to SX8), provenance, the `schema` verb and
replay. Offline. The keyed proof against real backends is `tests/test_schema_bank_live.py`."""

from __future__ import annotations

import builtins
import json
import re
from importlib import resources
from pathlib import Path

import pytest

from openreading import schema_bank
from openreading.cli.app import main
from openreading.derive.fields import check_typed_fields, schema_hash
from openreading.schema_bank import UnknownSchemaError, entries, resolve
from openreading.schema_io import SchemaFileError, load_schema
from openreading.types.blocks import TypedField

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "bank"
NAMES = ["bank_statement", "clinical_trial_agreement", "drawing_title_block", "generic", "rfq"]
DOMAIN = [n for n in NAMES if n != "generic"]
COUNTS = {
    "bank_statement": (7, 5, 2, 1),
    "rfq": (5, 6, 3, 0),
    "drawing_title_block": (6, 5, 2, 0),
    "clinical_trial_agreement": (7, 7, 1, 0),
    "generic": (0, 0, 0, 0),
}
# The expected records that prove each entry, as (entry, record file in examples/bank).
RECORDS = [
    ("rfq", "rfq_ardent_letter_quote.expected.json"),
    ("drawing_title_block", "drawing_mounting_bracket_asme.expected.json"),
    ("clinical_trial_agreement", "cta_pellucid_short.expected.json"),
    ("bank_statement", "john_smith_1000_2026_01.bank_statement.expected.json"),
    ("generic", "john_smith_1000_2026_01.generic.expected.json"),
]


def _text(name: str) -> str:
    return resources.files("openreading.schema_bank").joinpath(f"{name}.v1.yaml").read_text()


# --- the entries ----------------------------------------------------------------------------


def test_the_bank_holds_the_five_entries_at_v1():
    assert [(e.name, e.version, e.ref) for e in entries()] == [
        (n, 1, f"bank:{n}@v1") for n in NAMES
    ]


@pytest.mark.parametrize(("name", "counts"), sorted(COUNTS.items()))
def test_label_counts(name, counts):
    got = resolve(name).label_counts
    assert tuple(got[c] for c in ("critical", "expected", "optional", "ignore")) == counts


@pytest.mark.parametrize("name", NAMES)
def test_every_entry_loads_from_package_data_and_is_a_valid_json_schema(name):
    from jsonschema import validators

    entry = resolve(name)
    assert resources.files("openreading.schema_bank").joinpath(f"{name}.v1.yaml").is_file()
    validators.validator_for(entry.schema).check_schema(entry.schema)
    assert entry.schema_hash == schema_hash(entry.schema)
    assert entry.purpose and entry.purpose == entry.purpose.strip()
    assert entry.schema["title"] == name


@pytest.mark.parametrize("name", DOMAIN)
def test_every_label_in_a_domain_entry_states_its_reason(name):
    """SX6: a reason comment, naming the same level, on every `x-read-criticality` line."""
    seen = 0
    for line in _text(name).splitlines():
        if "x-read-criticality" not in line:
            continue
        seen += 1
        match = re.search(r"x-read-criticality:\s*(\w+)\s+#\s*(\w+):\s*(\S.{14,})$", line)
        assert match, f"no reason comment: {line.strip()}"
        assert match.group(1) == match.group(2), f"comment names another level: {line.strip()}"
        assert not re.search(r"\b(gold|scored|scoring)\b", match.group(3)), line.strip()
    assert seen == sum(COUNTS[name])


def test_no_two_labels_share_boilerplate_reason_text():
    """A reason that fits every field of its level says nothing about this one."""
    for name in DOMAIN:
        reasons = [
            line.split("#", 1)[1].split(":", 1)[1].strip()
            for line in _text(name).splitlines()
            if "x-read-criticality" in line
        ]
        for reason in reasons:
            assert "but not gating" not in reason
            assert "only when" not in reason


def test_generic_carries_no_x_read_keyword_at_any_depth():
    assert "x-read-" not in _text("generic")
    assert "x-read-" not in json.dumps(resolve("generic").schema)


@pytest.mark.parametrize(("name", "record"), RECORDS)
def test_each_entry_accepts_its_expected_record(name, record):
    schema = resolve(name).schema
    data = json.loads((EXAMPLES / record).read_text())
    check = check_typed_fields(schema, {k: TypedField(value=v) for k, v in data.items()})
    assert check.errors == []
    assert check.valid and check.extra == []
    # A correct reading of the proof document must pass every critical field. A critical label
    # on a field most documents leave empty would alarm on a correct reading.
    assert [
        f.path for f in check.fields if f.criticality == "critical" and f.status != "present"
    ] == []
    assert check.critical_ok
    for required in schema.get("required", []):
        assert required in data


@pytest.mark.parametrize("pdf", ["rfq_ardent_letter_quote", "drawing_mounting_bracket_asme"])
def test_each_example_document_ships_with_its_record(pdf):
    assert (EXAMPLES / f"{pdf}.pdf").read_bytes().startswith(b"%PDF")
    assert (EXAMPLES / f"{pdf}.expected.json").is_file()


def test_the_examples_readme_names_every_proof_document():
    text = (ROOT / "examples" / "README.md").read_text()
    for item in EXAMPLES.glob("*.pdf"):
        assert f"bank/{item.name}" in text
    assert "20261116" in text


# --- resolution -----------------------------------------------------------------------------


def test_a_bare_name_is_the_latest_and_a_pin_is_that_version():
    assert resolve("bank_statement").ref == "bank:bank_statement@v1"
    assert resolve("bank_statement@v1").schema == resolve("bank_statement").schema


def test_a_bare_name_picks_the_highest_version(monkeypatch):
    base = resolve("rfq")
    newer = schema_bank.BankEntry("rfq", 2, "p", "sha256:x", base.label_counts, {"title": "v2"})
    monkeypatch.setattr(schema_bank, "_load", lambda: (base, newer))
    assert resolve("rfq").version == 2
    assert resolve("rfq@v1").version == 1


@pytest.mark.parametrize(
    "value",
    ["./rfq", "../rfq", "rfq.yaml", "rfq.json", "/etc/hosts", "RFQ", "rfq@v", "rfq@1", "", " rfq"],
)
def test_resolve_refuses_anything_that_is_not_a_bank_name(value):
    with pytest.raises(UnknownSchemaError) as raised:
        resolve(value)
    assert raised.value.name == value
    assert raised.value.known == NAMES
    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize("value", ["nope", "rfq@v9"])
def test_an_unknown_name_or_version_lists_the_bank(value):
    with pytest.raises(UnknownSchemaError, match="bank_statement, clinical_trial_agreement"):
        resolve(value)


def test_resolve_opens_no_file_outside_the_package(tmp_path, monkeypatch):
    """SX8: a name from an agent never reaches the disk beyond package data."""
    (tmp_path / "rfq").write_text("{}")
    monkeypatch.chdir(tmp_path)
    schema_bank._load.cache_clear()
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    real_path_open = Path.open

    def path_spy(self, *args, **kwargs):
        opened.append(str(self))
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_spy)
    resolve("rfq")
    for hostile in ("./rfq", "../rfq", "rfq.yaml", "/etc/hosts"):
        with pytest.raises(UnknownSchemaError):
            resolve(hostile)
    package = str(resources.files("openreading.schema_bank"))
    assert all(p.startswith(package) for p in opened), opened
    assert not any(p.endswith("hosts") or p.endswith("/rfq") for p in opened)


def test_a_local_file_named_rfq_never_changes_what_resolve_returns(tmp_path, monkeypatch):
    before = resolve("rfq").schema
    (tmp_path / "rfq").write_text('{"type": "object", "properties": {"x": {}}}')
    monkeypatch.chdir(tmp_path)
    assert resolve("rfq").schema == before


def test_entries_are_copies_a_caller_may_change():
    resolve("rfq").schema["title"] = "changed"
    entries()[0].schema.clear()
    assert resolve("rfq").schema["title"] == "rfq"


# --- load_schema: the path rule --------------------------------------------------------------


def test_load_schema_returns_the_bank_entry_for_a_name(tmp_path, monkeypatch):
    (tmp_path / "rfq").write_text('{"type": "object", "properties": {"x": {}}}')
    monkeypatch.chdir(tmp_path)
    assert load_schema("rfq") == resolve("rfq").schema
    assert load_schema("rfq@v1") == resolve("rfq").schema


def test_load_schema_reads_a_file_by_separator_or_suffix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = {"type": "object", "properties": {"x": {"type": "string"}}}
    (tmp_path / "rfq").write_text(json.dumps(body))
    (tmp_path / "rfq.yaml").write_text("type: object\nproperties:\n  y:\n    type: string\n")
    assert load_schema("./rfq") == body
    assert load_schema("rfq.yaml")["properties"] == {"y": {"type": "string"}}
    assert load_schema(tmp_path / "rfq") == body  # a Path is always a file


def test_load_schema_unknown_name_is_a_value_error_with_the_names_and_a_file_hint():
    with pytest.raises(ValueError, match="bank_statement, clinical_trial_agreement") as raised:
        load_schema("myschema")
    assert "./myschema" in str(raised.value)
    with pytest.raises(UnknownSchemaError, match="rfq@v9"):
        load_schema("rfq@v9")


def test_a_missing_file_stays_a_schema_file_error():
    with pytest.raises(SchemaFileError, match="cannot read"):
        load_schema("./no_such_schema")
    with pytest.raises(SchemaFileError, match="cannot read"):
        load_schema("no_such.yaml")


# --- the CLI ---------------------------------------------------------------------------------


def _capture_run(monkeypatch):
    from openreading.cli import app as cli_app

    seen: dict = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stopped by test")

    monkeypatch.setattr(cli_app.api, "run", spy)
    return seen


@pytest.fixture
def pdf(tmp_path):
    from openreading.testing.sample_pdf import build_sample_pdf

    p = tmp_path / "doc.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _parse(pdf, value):
    return main(["parse", pdf, "--backend", "google-gemini", "--schema", value])


def test_schema_name_reaches_the_request_even_beside_a_local_file_of_that_name(
    pdf, tmp_path, monkeypatch
):
    (tmp_path / "rfq").write_text('{"type": "object", "properties": {"x": {}}}')
    monkeypatch.chdir(tmp_path)
    seen = _capture_run(monkeypatch)
    _parse(pdf, "rfq")
    assert seen["extraction_schema"] == {"json_schema": resolve("rfq").schema}


def test_a_pinned_name_reaches_the_request(pdf, monkeypatch):
    seen = _capture_run(monkeypatch)
    _parse(pdf, "bank_statement@v1")
    assert seen["extraction_schema"]["json_schema"]["title"] == "bank_statement"


def test_an_unknown_version_exits_2_and_lists_the_names(pdf, capsys):
    assert _parse(pdf, "rfq@v9") == 2
    err = capsys.readouterr().err
    assert "bank_statement" in err and "rfq@v9" in err


def test_an_extensionless_unknown_value_exits_2_and_names_the_file_spelling(
    pdf, tmp_path, monkeypatch, capsys
):
    (tmp_path / "myschema").write_text('{"type": "object", "properties": {"x": {}}}')
    monkeypatch.chdir(tmp_path)
    assert _parse(pdf, "myschema") == 2
    assert "./myschema" in capsys.readouterr().err
    seen = _capture_run(monkeypatch)
    _parse(pdf, "./myschema")
    assert seen["extraction_schema"]["json_schema"]["properties"] == {"x": {}}


def test_schema_list_prints_each_entry(capsys):
    assert main(["schema", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:6] == ["NAME", "VERSION", "CRITICAL", "EXPECTED", "OPTIONAL", "IGNORE"]
    rows = {line.split()[0]: line.split() for line in lines[1:]}
    assert sorted(rows) == NAMES
    assert rows["bank_statement"][1:6] == ["v1", "7", "5", "2", "1"]
    assert resolve("rfq").purpose in "\n".join(lines)


def test_schema_list_json_is_the_entries_without_their_schema(capsys):
    assert main(["schema", "list", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got == [e.to_dict() for e in entries()]
    assert all("schema" not in item for item in got)
    assert set(got[0]) == {"name", "version", "ref", "purpose", "schema_hash", "label_counts"}


@pytest.mark.parametrize("name", ["bank_statement", "rfq@v1", "generic"])
def test_schema_show_prints_the_resolved_schema(name, capsys):
    assert main(["schema", "show", name]) == 0
    assert json.loads(capsys.readouterr().out) == resolve(name).schema


def test_schema_show_keeps_the_labels(capsys):
    main(["schema", "show", "rfq"])
    assert "x-read-criticality" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["nope", "rfq@v9", "./rfq", "rfq.yaml"])
def test_schema_show_unknown_exits_2(name, capsys):
    assert main(["schema", "show", name]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "bank_statement" in captured.err


def test_help_schema_chapter_explains_how_to_write_a_schema(capsys):
    assert main(["help", "schema"]) == 0
    text = capsys.readouterr().out
    for code in ("critical", "expected", "optional", "ignore"):
        assert code in text
    assert "x-read-criticality" in text and "./invoice.yaml" in text


def test_the_help_chapters_yaml_example_is_a_valid_schema(tmp_path):
    import yaml

    import openreading.cli

    doc = openreading.cli.__doc__ or ""
    start = doc.index("    type: object\n    required: [invoice_number, total]")
    end = doc.index("\n\n", start)
    body = "\n".join(line[4:] for line in doc[start:end].splitlines())
    path = tmp_path / "invoice.yaml"
    path.write_text(body)
    assert load_schema(str(path))["required"] == ["invoice_number", "total"]
    assert yaml.safe_load(body)["properties"]["notes"]["x-read-criticality"] == "ignore"


# --- provenance ------------------------------------------------------------------------------


def _response():
    from openreading.types.enums import BackendType, ResponseState
    from openreading.types.response import BackendInfo, Document, NormalizedResponse, Status

    return NormalizedResponse(
        status=Status(state=ResponseState.SUCCEEDED),
        backend=BackendInfo(id="x", type=BackendType.HOSTED_API),
        document=Document(),
        typed_fields={"rfq_number": TypedField(value="RFQ-1")},
    )


def _ref_for(schema):
    from openreading.router.finalize import apply_schema_check
    from openreading.types.request import OpenReadingRequest

    req = OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": "ZmFrZQ==", "mime_type": "application/pdf"},
            "backend": {"id": "google-gemini"},
            "extraction_schema": {"json_schema": schema},
        }
    )
    check = apply_schema_check(_response(), req).schema_check
    assert check is not None
    return check.schema_ref, check.schema_hash


def test_schema_ref_comes_from_a_hash_match():
    ref, digest = _ref_for(resolve("rfq").schema)
    assert ref == "bank:rfq@v1" and digest == resolve("rfq").schema_hash
    assert schema_bank.ref_for_hash(digest) == "bank:rfq@v1"
    assert schema_bank.ref_for_hash("sha256:" + "0" * 64) is None


def test_a_user_schema_has_no_ref_and_an_exact_copy_has_one(tmp_path):
    custom = {"type": "object", "properties": {"rfq_number": {"type": "string"}}}
    assert _ref_for(custom)[0] is None
    edited = resolve("rfq").schema
    edited["properties"]["rfq_number"]["x-read-criticality"] = "optional"
    assert _ref_for(edited)[0] is None
    copy = tmp_path / "copy.json"
    copy.write_text(json.dumps(resolve("rfq").schema))
    assert _ref_for(load_schema(str(copy)))[0] == "bank:rfq@v1"


# --- replay ----------------------------------------------------------------------------------


def test_a_replay_returns_the_journaled_schema_after_the_bank_file_changes(tmp_path, monkeypatch):
    from openreading.api import _request_from_header
    from openreading.ledger.header import read_header
    from openreading.ledger.localfs import LocalFsBlobStore
    from openreading.types.request import ExtractionSchema
    from tests.fakes import ScriptedBackend, scripted_registry
    from tests.test_ledger_replay import _cfg, _req, _run_once

    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    schema = load_schema("rfq")
    req = _req()
    req.extraction_schema = ExtractionSchema(json_schema=schema)
    reg = scripted_registry(ScriptedBackend("solo", local=True, text="hello"))
    run_id, _, _ = _run_once(tmp_path / "ledger", reg, _cfg([{"backend": "solo"}]), req)

    edited = resolve("rfq")
    edited.schema["properties"]["rfq_number"]["x-read-criticality"] = "optional"
    monkeypatch.setattr(schema_bank, "_load", lambda: (edited,))
    assert load_schema("rfq")["properties"]["rfq_number"]["x-read-criticality"] == "optional"

    header = read_header(tmp_path / "ledger", run_id)
    replayed = _request_from_header(header, LocalFsBlobStore(tmp_path / "ledger" / "blobs"))
    assert replayed.extraction_schema is not None
    assert replayed.extraction_schema.json_schema == schema
    assert schema["properties"]["rfq_number"]["x-read-criticality"] == "critical"
