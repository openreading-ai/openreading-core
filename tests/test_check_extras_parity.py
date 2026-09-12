"""Offline tests for `scripts/check_extras_parity.py` (BL-158) — the registry/pyproject.toml
extras parity gate.

Failure-mode tests (missing extra, orphaned extra, exception map, allowlist, all-extra mirror
gap) drive the module's pure `check_parity()` with constructed dict fixtures — no file I/O, and
never the real pyproject.toml. One further test drives the full CLI (`main()`) against a
deliberately broken *constructed* pyproject.toml written under `tmp_path`, proving the check's
own failure mode end to end without editing the real file (AC-7). Only the "real repository
state" test (AC-1) reads this repo's actual pyproject.toml and registry, by design.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# scripts/ is a plain directory, not an installed package — load the module by path, the same way
# tests/test_check_backlog_merged.py does for its sibling script.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_extras_parity.py"
_spec = importlib.util.spec_from_file_location("check_extras_parity", _SCRIPT)
assert _spec is not None and _spec.loader is not None
cep = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cep
_spec.loader.exec_module(cep)

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- AC-1: real repository state ---------------------------------------------------------------


def test_real_repo_state_is_clean_and_reports_counts():
    registry_slugs = cep.load_registry_slugs()
    extras = cep.load_pyproject_extras(REPO_ROOT / "pyproject.toml")

    report = cep.check_parity(registry_slugs, extras)

    assert report.ok, cep.format_errors(report)
    assert report.registry_slug_count == 16
    assert report.matching_extra_count == 16
    assert report.exception_count == 1
    # Benchmark integrations are utility extras, like HTTP and server support. They do not map to
    # adapter slugs, so the parity allowlist accounts for both runnable public profiles.
    assert report.allowlisted_extra_count == 6


def test_main_against_real_repo_exits_zero(capsys: pytest.CaptureFixture[str]):
    rc = cep.main(["--repo", str(REPO_ROOT)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "extras-parity: OK" in out
    assert "16 registry slugs" in out


# --- AC-2: missing extra ------------------------------------------------------------------------


def test_missing_extra_is_flagged_by_exact_slug():
    report = cep.check_parity(registry_slugs={"foo-adapter"}, extras={})

    assert not report.ok
    assert report.missing_extras == ["foo-adapter"]
    assert any("foo-adapter" in e for e in cep.format_errors(report))


# --- AC-3: orphaned extra ------------------------------------------------------------------------


def test_orphaned_extra_is_flagged_by_exact_extra_name():
    report = cep.check_parity(
        registry_slugs={"foo"},
        extras={"foo": ["somepkg>=1.0"], "bar-orphan": ["otherpkg>=1.0"]},
    )

    assert not report.ok
    assert report.orphaned_extras == ["bar-orphan"]
    assert any("bar-orphan" in e for e in cep.format_errors(report))


# --- AC-4: the aws-textract/textract exception is explicit, named, and load-bearing -------------


def test_exception_map_has_exactly_the_one_documented_entry():
    assert cep.EXTRA_NAME_EXCEPTIONS == {"aws-textract": "textract"}


def test_exception_map_applied_reconciles_the_naming_mismatch():
    report = cep.check_parity(
        registry_slugs={"aws-textract"},
        extras={"textract": ["boto3>=1.34"]},
    )

    assert report.ok


def test_exception_map_is_load_bearing_not_decorative():
    # Same inputs as above, but with an empty exception map: the mismatch is no longer
    # reconciled, so it must surface on *both* sides (slug missing its extra, extra orphaned) —
    # proving the real EXTRA_NAME_EXCEPTIONS entry is doing real work, not a no-op.
    report = cep.check_parity(
        registry_slugs={"aws-textract"},
        extras={"textract": ["boto3>=1.34"]},
        exceptions={},
    )

    assert report.missing_extras == ["aws-textract"]
    assert report.orphaned_extras == ["textract"]


# --- non-adapter allowlist (http/server/all) -----------------------------------------------------


def test_non_adapter_allowlist_extras_are_not_flagged_as_orphaned():
    report = cep.check_parity(
        registry_slugs={"foo"},
        extras={
            "foo": ["somepkg>=1.0"],
            "http": ["httpx>=0.27"],
            "server": ["fastapi>=0.110"],
            "all": ["somepkg>=1.0"],
        },
    )

    assert report.ok


def test_allowlist_is_load_bearing_not_decorative():
    # Same fixture, but "http" dropped from the allowlist: it must now be flagged, proving the
    # real NON_ADAPTER_EXTRAS entries are doing real work.
    report = cep.check_parity(
        registry_slugs={"foo"},
        extras={
            "foo": ["somepkg>=1.0"],
            "http": ["httpx>=0.27"],
            "server": ["fastapi>=0.110"],
            "all": ["somepkg>=1.0"],
        },
        non_adapter_extras=frozenset({"server", "all"}),
    )

    assert report.orphaned_extras == ["http"]


# --- AC-5: all-extra mirror gap -------------------------------------------------------------------


def test_all_extra_mirror_gap_names_package_and_source_extra():
    report = cep.check_parity(
        registry_slugs={"foo"},
        extras={"foo": ["somepkg>=1.0", "otherpkg>=2.0"], "all": ["somepkg>=1.0"]},
    )

    assert not report.ok
    assert report.unmirrored == [("otherpkg", "foo")]
    (msg,) = cep.format_errors(report)
    assert "otherpkg" in msg and "foo" in msg and "all" in msg


def test_all_extra_mirror_holds_when_every_package_is_mirrored():
    report = cep.check_parity(
        registry_slugs={"foo", "bar"},
        extras={
            "foo": ["somepkg>=1.0"],
            "bar": ["otherpkg>=2.0"],
            "all": ["somepkg>=1.0", "otherpkg>=2.0"],
        },
    )

    assert report.unmirrored == []


def test_all_extra_mirror_ignores_version_pins():
    # Different pin, same bare package name — mirror check is name-only (scope explicitly cuts
    # version-pin correctness).
    report = cep.check_parity(
        registry_slugs={"foo"},
        extras={"foo": ["somepkg>=1.0"], "all": ["somepkg>=2.5"]},
    )

    assert report.unmirrored == []


def test_all_extra_mirror_skips_non_adapter_extras():
    # http/server package names are never required to appear in `all`.
    report = cep.check_parity(
        registry_slugs={"foo"},
        extras={
            "foo": ["somepkg>=1.0"],
            "http": ["httpx>=0.27"],
            "all": ["somepkg>=1.0"],
        },
    )

    assert report.unmirrored == []


# --- package_name() -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("httpx>=0.27", "httpx"),
        ("google-cloud-documentai>=2.29", "google-cloud-documentai"),
        ("pytesseract>=0.3.13", "pytesseract"),
        ("pillow>=10", "pillow"),
        ("svix>=1.0", "svix"),
    ],
)
def test_package_name_strips_version_specifier(requirement: str, expected: str):
    assert cep.package_name(requirement) == expected


def test_package_name_rejects_unparsable_requirement():
    with pytest.raises(cep.ParityError):
        cep.package_name(">=1.0")


# --- load_pyproject_extras() -----------------------------------------------------------------------


def test_load_pyproject_extras_reads_optional_dependencies_table(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\n\n'
        "[project.optional-dependencies]\n"
        'alpha = ["somepkg>=1.0"]\n'
        'all = ["somepkg>=1.0"]\n',
        encoding="utf-8",
    )

    extras = cep.load_pyproject_extras(pyproject)

    assert extras == {"alpha": ["somepkg>=1.0"], "all": ["somepkg>=1.0"]}


def test_load_pyproject_extras_missing_file_raises_parity_error(tmp_path: Path):
    with pytest.raises(cep.ParityError):
        cep.load_pyproject_extras(tmp_path / "does-not-exist.toml")


# --- AC-7: deliberately broken fixture drives the full CLI, real pyproject.toml never touched ----


def test_cli_against_deliberately_broken_fixture_fails_and_names_the_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # A small, self-contained registry — decoupled from this repo's real 15 adapters, so this
    # test stays correct even if the real registry grows or shrinks.
    monkeypatch.setattr(cep, "load_registry_slugs", lambda: {"alpha", "beta-adapter"})

    broken_pyproject = tmp_path / "pyproject.toml"
    broken_pyproject.write_text(
        '[project]\nname = "fixture"\n\n'
        "[project.optional-dependencies]\n"
        # "beta-adapter" deliberately has no matching extra here (AC-2 shape), and "stray" is a
        # deliberate orphan (AC-3 shape) — neither exists in the real pyproject.toml.
        'alpha = ["somepkg>=1.0", "extra-pkg>=1.0"]\n'
        'stray = ["otherpkg>=1.0"]\n'
        # `all` deliberately omits "extra-pkg" (AC-5 shape).
        'all = ["somepkg>=1.0"]\n',
        encoding="utf-8",
    )

    rc = cep.main(["--pyproject", str(broken_pyproject)])
    err = capsys.readouterr().err

    assert rc == 1
    assert "beta-adapter" in err
    assert "stray" in err
    assert "extra-pkg" in err


def test_cli_missing_pyproject_file_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rc = cep.main(["--pyproject", str(tmp_path / "nope.toml")])
    err = capsys.readouterr().err

    assert rc == 2
    assert "not found" in err
