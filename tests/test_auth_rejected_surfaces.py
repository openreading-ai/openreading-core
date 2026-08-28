"""BL-6 — the `auth_rejected` hint fires on EVERY surface, not just `parse --backend X`.

the openreading.credentials docstring promises that a key which is present but rejected by the provider always
produces *"key was found but rejected — check `<VAR>`"*. The derivation lives once, in
`readiness.auth_rejected_hint`, and `readiness.auth_hinted` stamps it onto the failure at the
execution boundary, so single parse, batch parse, `route --run`, `replay`, `calibrate` and the HTTP
API all say the same thing.

The fixture is a real ReductoAdapter with an injected client that answers 401 with a body that
ECHOES the rejected key — the exact hazard the hint must never pass through. Fully offline: no
network, no real credential.
"""

from __future__ import annotations

import base64
import json

import pytest

from openreading.adapters._http import error_for_status
from openreading.adapters.registry import BUILTIN_ADAPTERS, make_adapter
from openreading.cli import main
from openreading.router.registry import Registry
from openreading.testing.sample_pdf import build_sample_pdf

pytest.importorskip("fitz", reason="pymupdf not installed")

_SECRET = "sk_live_LEAKED_SECRET"
_LEAKY_401 = f'{{"detail":"invalid api key: {_SECRET}"}}'
_ENV = "REDUCTO_API_KEY"


class _RejectingReductoClient:
    """Every call 401s, and the provider's body echoes the key back (seen in the wild)."""

    def parse(self, document: dict, options: dict, is_async: bool) -> dict:
        raise error_for_status(401, {}, message=_LEAKY_401)

    def extract(self, document: dict, schema: dict, is_async: bool) -> dict:
        raise error_for_status(401, {}, message=_LEAKY_401)

    def get_job(self, job_id: str) -> dict:
        raise error_for_status(401, {}, message=_LEAKY_401)

    def verify_webhook(self, headers: dict, body: dict) -> bool:
        return True


@pytest.fixture
def rejecting_reducto(monkeypatch):
    """`reducto` in the catalog, credential present, provider says no."""
    from openreading.adapters.reducto import ReductoAdapter

    monkeypatch.setenv(_ENV, _SECRET)
    monkeypatch.setitem(
        BUILTIN_ADAPTERS, "reducto", lambda: ReductoAdapter(client=_RejectingReductoClient())
    )


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _assert_actionable(text: str) -> None:
    """The contract: name the var to fix, never echo the key."""
    assert _ENV in text, text
    assert "rejected" in text, text
    assert _SECRET not in text, "the provider's 401 body (which echoed the key) leaked"


# --- CLI: single parse (the path that already worked — pinned against regression) ----------------


def test_parse_single_names_the_env_var(sample_pdf, rejecting_reducto, capsys):
    rc = main(["parse", sample_pdf, "--backend", "reducto"])
    assert rc == 3
    _assert_actionable(capsys.readouterr().err)


# --- CLI: batch parse ---------------------------------------------------------------------------


def test_batch_parse_items_carry_the_env_var(tmp_path, rejecting_reducto, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(2):
        (docs / f"d{i}.pdf").write_bytes(build_sample_pdf())

    rc = main(["parse", str(docs), "--backend", "reducto"])
    assert rc == 1  # every item failed → batch state "failed"
    captured = capsys.readouterr()

    env = json.loads(captured.out)
    assert [i["error"]["code"] for i in env["items"]] == ["auth_rejected"] * 2
    for item in env["items"]:
        _assert_actionable(item["error"]["message"])

    # stdout is usually redirected, so on_progress echoes each failing item's actionable message
    # to stderr as it happens — once per item, since fault isolation (M6) means a failure never
    # raises out of the batch and stdout's envelope is the only other place the message lives.
    _assert_actionable(captured.err)
    assert captured.err.count("check REDUCTO_API_KEY") == 2, captured.err


# --- CLI: route --run driven all the way to PlanExhaustedError (Trent's T7) ----------------------


@pytest.fixture
def only_reducto(monkeypatch, rejecting_reducto):
    """A one-backend registry for `route`, so the plan cannot fall back to a local backend and the
    chain really exhausts."""
    reg = Registry()
    reg.register(make_adapter("reducto"))
    monkeypatch.setattr("openreading.cli.app.build_registry", lambda: reg)


@pytest.fixture
def open_policy(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"optimize_for": "accuracy"}))
    return str(p)


def test_route_run_exhaustion_prints_trail_hint_and_still_emits_the_plan(
    sample_pdf, only_reducto, open_policy, capsys
):
    rc = main(["route", sample_pdf, "--policy", open_policy, "--run"])
    assert rc == 3
    captured = capsys.readouterr()

    # the formatted failure trail, then the actionable hint for the rejected backend
    assert "plan exhausted — reducto:TerminalError(auth_rejected)" in captured.err
    _assert_actionable(captured.err)

    # the plan itself is still the answer to `route` — exhaustion does not suppress it
    plan = json.loads(captured.out)
    assert plan["chosen"] == "reducto"
    assert "result" not in plan


def test_route_run_without_run_flag_is_unaffected(sample_pdf, only_reducto, open_policy, capsys):
    rc = main(["route", sample_pdf, "--policy", open_policy])
    assert rc == 0  # planning never touches a credential
    assert json.loads(capsys.readouterr().out)["chosen"] == "reducto"


# --- CLI: replay + calibrate --------------------------------------------------------------------

_HOSTED_CONFIG = """\
version: 1
strategies:
  hosted_only:
    steps:
      - backend: reducto
        escalate_if:
          confidence_below: 0.85
      - tesseract
"""


def _config(tmp_path) -> str:
    cfg = tmp_path / "openreading.yaml"
    cfg.write_text(_HOSTED_CONFIG)
    return str(cfg)


# single-step: no fallback rung, so an auth_rejected reducto guarantees exhaustion regardless of
# whether the tesseract binary happens to be installed on the machine running the suite.
_REDUCTO_ONLY_CONFIG = """\
version: 1
strategies:
  hosted_only:
    steps:
      - backend: reducto
"""


def _reducto_only_config(tmp_path) -> str:
    cfg = tmp_path / "openreading.yaml"
    cfg.write_text(_REDUCTO_ONLY_CONFIG)
    return str(cfg)


def test_replay_exhaustion_names_the_env_var(sample_pdf, tmp_path, rejecting_reducto, capsys):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"orchestration": {"strategy": "hosted_only", "decisions": []}}))
    cfg = _reducto_only_config(tmp_path)
    rc = main(["replay", sample_pdf, "--trace", str(trace), "--config", cfg])
    assert rc == 3
    _assert_actionable(capsys.readouterr().err)


def test_calibrate_names_the_env_var_instead_of_crashing(tmp_path, rejecting_reducto, capsys):
    ds = tmp_path / "dataset"
    (ds / "case_00").mkdir(parents=True)
    (ds / "case_00" / "case.json").write_text(
        json.dumps({"name": "c0", "input": {"builtin_sample": True}, "expected": {}})
    )
    rc = main(["calibrate", str(ds), "--strategy", "hosted_only", "--config", _config(tmp_path)])
    assert rc == 3  # a clean exit, not a traceback
    _assert_actionable(capsys.readouterr().err)


# --- HTTP API -----------------------------------------------------------------------------------


def _server_client():
    pytest.importorskip("fastapi", reason="server extra not installed")
    from fastapi.testclient import TestClient

    from openreading.server import create_app

    return TestClient(create_app())


def _reducto_body():
    return {
        "document": {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        },
        "backend": {"id": "reducto"},
    }


@pytest.mark.parametrize("endpoint", ["/v1/parse", "/v1/jobs"])
def test_server_error_response_names_the_env_var(endpoint, rejecting_reducto):
    r = _server_client().post(endpoint, json=_reducto_body())
    assert r.status_code == 424  # same code as missing credentials: the caller must fix a var
    err = r.json()["error"]
    assert err["backend_code"] == "auth_rejected"
    _assert_actionable(err["message"])


def test_server_batch_items_name_the_env_var(rejecting_reducto):
    body = {"documents": [_reducto_body()["document"]], "backend": "reducto"}
    r = _server_client().post("/v1/batch", json=body)
    assert r.status_code == 200  # per-item isolation: the batch envelope reports the failure
    item = r.json()["items"][0]
    assert item["error"]["code"] == "auth_rejected"
    _assert_actionable(item["error"]["message"])
