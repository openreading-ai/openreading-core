"""Policy enforcement at the boundaries a loader does not cover.

The file is validated by `strategy-config` v0.3 when it is read. Nothing validates a dict a
Python caller hands straight to `config.apply`, `config.router_config` or `compile_strategy`, and
nothing used to stop a request from relaxing what the file required. Both are enforcement holes
rather than authoring mistakes, so they are pinned here rather than in `test_policy_validation`.

Law PF1: neither source can weaken the other. Booleans OR, retention keeps the lower ceiling, and
two different regions are a refusal rather than a winner, because regions have no ordering and a
request cannot express two at once.

Law PF2: a public call is safe on its own. `compile_strategy` and `config.apply` enforce the
policy they are given without relying on an earlier loader call.
"""

from __future__ import annotations

import pytest

from openreading import api, config
from openreading.adapters.registry import build_registry
from openreading.router.compliance import RouterConfig
from openreading.strategies import compile_strategy
from openreading.strategies.model import StrategyConfig
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ComplianceRefused
from openreading.types.request import OpenReadingRequest

pytest.importorskip("fitz", reason="pymupdf not installed")


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _req(**body) -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": "", "mime_type": "application/pdf"},
            "backend": {"id": "auto"},
            **body,
        }
    )


# --- PF1: retention keeps the lower ceiling ------------------------------------------------------


def test_a_request_ceiling_cannot_raise_the_file_ceiling():
    """A file demanding zero retention and a request asking for 48h used to produce 48h, so a
    backend retaining 24 hours survived a policy that forbade retention outright."""
    out, _ = config.apply(
        _req(compliance={"max_retention": "48h"}), {"max_retention": "zero"}, RouterConfig()
    )
    assert out.compliance is not None
    assert out.compliance.max_retention == "zero"


def test_a_request_ceiling_lower_than_the_file_wins():
    """The union takes the lower of the two, whichever side supplied it."""
    out, _ = config.apply(
        _req(compliance={"max_retention": "zero"}), {"max_retention": "48h"}, RouterConfig()
    )
    assert out.compliance is not None
    assert out.compliance.max_retention == "zero"


def test_equal_ceilings_are_unchanged():
    out, _ = config.apply(
        _req(compliance={"max_retention": "24h"}), {"max_retention": "24h"}, RouterConfig()
    )
    assert out.compliance is not None
    assert out.compliance.max_retention == "24h"


def test_a_ceiling_from_one_side_only_is_taken():
    out, _ = config.apply(_req(), {"max_retention": "48h"}, RouterConfig())
    assert out.compliance is not None and out.compliance.max_retention == "48h"
    out, _ = config.apply(_req(compliance={"max_retention": "48h"}), {}, RouterConfig())
    assert out.compliance is not None and out.compliance.max_retention == "48h"


def test_a_ceiling_that_does_not_parse_fails_closed_before_dispatch():
    """`soon` is the caller's own mistake. It cannot be compared, so it cannot be reconciled with
    the other side, and guessing which one wins is how a ceiling silently disappears."""
    with pytest.raises(ComplianceRefused) as exc:
        config.apply(
            _req(compliance={"max_retention": "soon"}), {"max_retention": "zero"}, RouterConfig()
        )
    assert "max_retention" in str(exc.value)


# --- PF1: two regions are a refusal, not a winner ------------------------------------------------


def test_two_different_regions_refuse_and_name_both():
    """A file requiring `eu` and a request asking for `us` used to produce `us`, so a US-only
    backend ran under a policy that required Europe. There is no region that satisfies both."""
    with pytest.raises(ComplianceRefused) as exc:
        config.apply(_req(compliance={"data_region": "us"}), {"data_region": "eu"}, RouterConfig())
    assert exc.value.constraint == "region_conflict"
    assert "eu" in str(exc.value) and "us" in str(exc.value)


def test_equal_regions_are_not_a_conflict():
    out, _ = config.apply(
        _req(compliance={"data_region": "eu"}), {"data_region": "eu"}, RouterConfig()
    )
    assert out.compliance is not None and out.compliance.data_region == "eu"


def test_regions_match_case_insensitively_before_conflicting():
    out, _ = config.apply(
        _req(compliance={"data_region": "EU"}), {"data_region": "eu"}, RouterConfig()
    )
    assert out.compliance is not None


def test_a_region_from_one_side_only_is_taken():
    out, _ = config.apply(_req(), {"data_region": "eu"}, RouterConfig())
    assert out.compliance is not None and out.compliance.data_region == "eu"


# --- PF1: booleans and attestations --------------------------------------------------------------


def test_booleans_or_from_either_side():
    out, _ = config.apply(
        _req(compliance={"require_baa": True}), {"require_local": True}, RouterConfig()
    )
    assert out.compliance is not None
    assert out.compliance.require_baa is True and out.compliance.require_local is True


def test_the_request_preference_still_wins_over_the_file():
    """`optimize_for` orders the survivors and never changes the set, so the more specific caller
    value wins. It is a preference, and preferences are not constraints."""
    out, _ = config.apply(
        _req(routing={"optimize_for": "latency"}), {"optimize_for": "cost"}, RouterConfig()
    )
    assert out.routing is not None and out.routing.optimize_for == "latency"


# --- PF2: a raw dict cannot buy permission -------------------------------------------------------


def test_a_quoted_boolean_cannot_widen_through_router_config():
    """`bool("false")` is True. Before the schema closed, `validate_policy` caught this on every
    path; the schema catches it only on the path that reads a file."""
    with pytest.raises(ValueError):
        config.router_config({"allow_unverified_compliance": "false"})


def test_a_bare_attestation_string_cannot_confirm_a_backend():
    """A bare string used to become a frozenset of its own characters, confirming no backend at
    all while looking like it confirmed one."""
    with pytest.raises(ValueError):
        config.router_config({"train_optout_confirmed": "aws-textract"})


def test_an_unknown_key_in_a_dict_is_refused_like_one_in_a_file():
    with pytest.raises(ValueError) as exc:
        config.apply(_req(), {"require_locall": True}, RouterConfig())
    assert "require_locall" in str(exc.value)


def test_a_well_formed_dict_still_works():
    """The typed boundary must refuse the wrong shape without refusing the right one."""
    cfg = config.router_config(
        {"baa_tier_confirmed": ["reducto"], "allow_unverified_compliance": True}
    )
    assert cfg.baa_tier_confirmed == frozenset({"reducto"})
    assert cfg.allow_unverified_compliance is True


# --- PF2: compile_strategy enforces the policy it is handed --------------------------------------


def test_direct_compile_strategy_enforces_the_file_policy(sample_pdf):
    """`compile_strategy` and `StrategyConfig` are public exports. A caller who builds the config
    itself and compiles it must get the same refusal `openreading.run` gets, or the block is
    advisory for everyone embedding this package."""
    cfg = StrategyConfig.model_validate(
        {"version": 1, "policy": {"require_local": True}, "strategies": {"s": ["reducto"]}}
    )
    with pytest.raises(ComplianceRefused) as exc:
        compile_strategy(
            api.build_request(sample_pdf, "auto"), "s", cfg, build_registry(), RouterConfig()
        )
    assert "reducto" in str(exc.value)


def test_applying_the_policy_twice_is_a_no_op(sample_pdf):
    """`openreading.run` applies the block before dispatch and `compile_strategy` applies it again
    defensively. The second application must not change the compiled result."""
    cfg = StrategyConfig.model_validate(
        {"version": 1, "policy": {"require_local": True}, "strategies": {"s": ["pymupdf"]}}
    )
    registry = build_registry()
    raw = api.build_request(sample_pdf, "auto")
    once = compile_strategy(raw, "s", cfg, registry, RouterConfig())
    applied, router_config = config.apply(raw, cfg.policy, RouterConfig())
    twice = compile_strategy(applied, "s", cfg, registry, router_config)
    assert once.config_hash == twice.config_hash
    assert once.eligible == twice.eligible


# --- PF6: evaluation honours the same file every other surface does ------------------------------


def _one_case_dataset(tmp_path):
    import json

    case = tmp_path / "c1"
    case.mkdir()
    (case / "case.json").write_text(
        json.dumps(
            {
                "name": "c1",
                "input": {"builtin_sample": True},
                "expected": {"text_contains": ["OpenReading Test Document"]},
            }
        )
    )
    return str(tmp_path)


def test_leaderboard_applies_the_file_compliance_to_every_case(tmp_path):
    """`--config` on `leaderboard` passed only the three attestations through, so the file's
    `require_local` never reached a dataset request and a hosted backend ran under it. The
    attestations qualify requirements, so applying them alone is the one combination that is
    always wrong."""
    from openreading.evals.leaderboard import run_leaderboard

    dataset = _one_case_dataset(tmp_path)
    report = run_leaderboard(
        dataset,
        ["pymupdf", "reducto"],
        build_registry(),
        policy={"require_local": True},
    )
    rows = {r["backend_id"]: r for r in report.to_schema_dict()["backends"]}
    assert rows["pymupdf"]["errors"] == 0
    assert rows["reducto"]["errors"] == 1  # refused on compliance, scored as an error


def test_leaderboard_attestations_still_qualify_the_requirement_they_belong_to(tmp_path):
    """The other half: an attestation must still readmit what it confirms, or the fix would have
    turned the file into a filter that ignores its own three widening keys."""
    from openreading.evals.leaderboard import run_leaderboard

    dataset = _one_case_dataset(tmp_path)
    refused = run_leaderboard(
        dataset, ["pymupdf", "reducto"], build_registry(), policy={"require_baa": True}
    )
    assert {r["backend_id"]: r["errors"] for r in refused.to_schema_dict()["backends"]}[
        "reducto"
    ] == 1

    confirmed = run_leaderboard(
        dataset,
        ["pymupdf", "reducto"],
        build_registry(),
        policy={"require_baa": True, "baa_tier_confirmed": ["reducto"]},
    )
    row = {r["backend_id"]: r for r in confirmed.to_schema_dict()["backends"]}["reducto"]
    # Readmitted by the attestation: it now fails on the missing key instead of on compliance.
    assert row["errors"] == 1


# --- PF4: one batch, one snapshot ----------------------------------------------------------------


def test_a_platform_batch_reads_the_file_once(tmp_path, monkeypatch):
    """A two-item batch loaded the file three times: once for the batch, then again inside each
    item's own `run()`. Three reads are three chances to see a different file."""
    from openreading import config as config_module

    monkeypatch.chdir(tmp_path)
    (tmp_path / "corpus").mkdir()
    for name in ("a", "b"):
        (tmp_path / "corpus" / f"{name}.pdf").write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text("version: 1\npolicy:\n  require_local: true\n")

    reads = []
    real = config_module.parse

    def counting(text, *, source="<string>"):
        reads.append(source)
        return real(text, source=source)

    monkeypatch.setattr(config_module, "parse", counting)
    api.run_batch(["corpus/"], backend="pymupdf", config="openreading.yaml")
    assert len(reads) == 1, f"the file was parsed {len(reads)} times: {reads}"


def test_editing_the_file_mid_batch_cannot_change_a_later_item(tmp_path, monkeypatch):
    """The snapshot is taken before intake, so a file edited while the batch runs cannot make one
    document travel under a policy a sibling never saw."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "corpus").mkdir()
    for name in ("a", "b"):
        (tmp_path / "corpus" / f"{name}.pdf").write_bytes(build_sample_pdf())
    path = tmp_path / "openreading.yaml"
    path.write_text("version: 1\npolicy:\n  require_local: true\n")

    snapshot = config.load(str(path))
    # Replace the file with one that will not load at all. A batch driven from the snapshot must
    # not notice, because it took its copy before intake and never goes back to disk.
    path.write_text("version: 1\npolicy: {require_locall: true}\n")
    env = api.run_batch(["corpus/"], backend="pymupdf", config=snapshot)
    assert env["summary"]["succeeded"] == 2

    # And the proof the replacement really is unloadable, so the assertion above means something.
    with pytest.raises(config.ConfigError):
        api.run_batch(["corpus/"], backend="pymupdf", config=str(path))


# --- PF6: publisher identity follows content, not a path -----------------------------------------


def test_publisher_identity_changes_when_the_policy_changes(tmp_path):
    """The pipeline name keys the artifact directory and the publisher's resume. Hashing the path
    meant editing the policy at that path left the name unchanged, so a rerun resumed results
    measured under a different policy while labelling them as the current one."""
    from openreading.evals.targets import BenchmarkTarget, pipeline_name

    target = BenchmarkTarget.parse("backend:pymupdf")
    path = tmp_path / "p.yaml"
    path.write_text("version: 1\npolicy:\n  require_local: true\n")
    first = pipeline_name("parsebench", target, config=str(path))
    path.write_text("version: 1\npolicy:\n  require_baa: true\n")
    assert pipeline_name("parsebench", target, config=str(path)) != first


def test_publisher_identity_ignores_formatting(tmp_path):
    """Content, not bytes: reordering keys or changing indentation is not a different measurement,
    and refusing to resume over it would make the identity useless."""
    from openreading.evals.targets import BenchmarkTarget, pipeline_name

    target = BenchmarkTarget.parse("backend:pymupdf")
    path = tmp_path / "p.yaml"
    path.write_text("version: 1\npolicy:\n  require_baa: true\n  require_local: true\n")
    first = pipeline_name("parsebench", target, config=str(path))
    path.write_text("policy:\n    require_local: true\n\n    require_baa: true\nversion: 1\n")
    assert pipeline_name("parsebench", target, config=str(path)) == first


# --- PF3: resume compares policy provenance, not only its effect --------------------------------


def _armed_run(tmp_path, monkeypatch, policy_block: str) -> str:
    import contextlib
    import io

    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.pdf").write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        f"version: 1\n{policy_block}strategies:\n  s: [pymupdf]\n"
    )
    seen: dict[str, str] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        api.run("s.pdf", strategy="s", on_run_armed=lambda r: seen.setdefault("id", r))
    return seen["id"]


def test_removing_a_file_constraint_refuses_the_resume(tmp_path, monkeypatch):
    """The ledger stores the request AFTER the file was folded in, so a removed `require_local`
    stays in the stored request and the recomputed identity matched. The resume then ran under a
    policy the file no longer asks for, and said nothing. It fails closed rather than leaking, but
    an operator who edits the file and resumes has to be told the run no longer matches it."""
    from openreading.ledger.header import HeaderMismatch

    run_id = _armed_run(tmp_path, monkeypatch, "policy:\n  require_local: true\n")
    (tmp_path / "openreading.yaml").write_text("version: 1\nstrategies:\n  s: [pymupdf]\n")
    with pytest.raises(HeaderMismatch):
        api.resume_run(run_id)


def test_reformatting_the_file_still_resumes(tmp_path, monkeypatch):
    """Identity follows content. Reindenting or reordering keys is not a policy change, and
    refusing to resume over it would make the check something people route around."""
    import contextlib
    import io

    run_id = _armed_run(tmp_path, monkeypatch, "policy:\n  require_local: true\n")
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\npolicy: {require_local: true}\nstrategies:\n  s:\n    - pymupdf\n"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        out = api.resume_run(run_id)
    assert out["status"]["state"] == "succeeded"


def test_adding_a_file_constraint_also_refuses_the_resume(tmp_path, monkeypatch):
    """The direction that already worked, pinned beside the one that did not, so a future edit
    cannot fix one by breaking the other."""
    from openreading.ledger.header import HeaderMismatch

    run_id = _armed_run(tmp_path, monkeypatch, "")
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\npolicy:\n  require_local: true\nstrategies:\n  s: [pymupdf]\n"
    )
    with pytest.raises(HeaderMismatch):
        api.resume_run(run_id)


# --- PF2, the remaining reimplementation ---------------------------------------------------------


def test_calibrate_folds_the_whole_block_not_only_its_compliance_half(tmp_path, monkeypatch):
    """`calibrate_strategy` assembled the fold by hand out of the two halves it needed, so the
    file's `optimize_for` reached every other surface and not this one. A surface that rebuilds
    `config.apply` gets whatever that surface's author remembered."""
    import inspect

    from openreading.strategies import calibrate

    source = inspect.getsource(calibrate.calibrate_strategy)
    assert "apply_policy(" in source, "calibrate must call the shared fold, not rebuild it"


def test_native_and_platform_batches_agree_on_the_verdict(tmp_path, monkeypatch):
    """Two dispatch shapes, one snapshot, one answer. The native path builds its requests itself,
    so a policy applied on only one of them is a batch whose verdict depends on which adapter
    happened to declare `batch.native`."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "a.pdf").write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text("version: 1\npolicy:\n  require_local: true\n")

    platform = api.run_batch(["corpus/"], backend="pymupdf", config="openreading.yaml")
    assert platform["summary"]["succeeded"] == 1  # pymupdf is local, so the policy admits it

    refused = api.run_batch(["corpus/"], backend="reducto", config="openreading.yaml")
    assert refused["summary"]["failed"] == 1
    assert "require_local" in refused["items"][0]["error"]["message"]
