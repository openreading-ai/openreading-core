"""What `scripts/new_adapter.py` writes must be constructible on the contributor's first day.

The generator promises a scaffold that runs before any placeholder is resolved, so this renders
one adapter per `--type` and proves the rendered class can be imported and instantiated. The
placeholder markers stay in the rendered text, and every test below runs offline with no key.

`tests/test_scaffold_sentinel.py` only greps for the placeholder marker, so it cannot see a
descriptor field the generator forgot or a method signature that drifted from
`openreading.adapters.base.BackendAdapter`. Both defects surface as a stack trace on the
contributor's first run, which is the failure these tests exist to catch at build time instead.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from openreading.adapters.base import BackendAdapter

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR_PATH = REPO_ROOT / "scripts" / "new_adapter.py"

# The four methods `BackendAdapter` supplies a default for. An adapter that overrides one with a
# different parameter list still registers, and it breaks only when the router calls it.
CONTRACT_METHODS = ("poll", "resolve_webhook", "cancel", "normalize")


def _load_generator():
    """Import `scripts/new_adapter.py` by path, because `scripts/` is not an importable package."""
    spec = importlib.util.spec_from_file_location("_new_adapter_under_test", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GENERATOR = _load_generator()

# One case per `--type`, using that type's first template slug in sorted order. Every type renders
# a different set of methods, so all seven have to be exercised.
_CASES = sorted(
    (type_name, sorted(slugs)[0]) for type_name, slugs in _GENERATOR.TYPE_TEMPLATES.items()
)


def _build_scaffolded_adapter(template_slug: str, tmp_path: Path) -> BackendAdapter:
    """Render the adapter source for slug `foo-vendor`, execute it, and return an instance."""
    shape = _GENERATOR.read_template_shape(template_slug)
    source = _GENERATOR.render_adapter_py("foo-vendor", template_slug, shape)
    path = tmp_path / "scaffolded_adapter.py"
    path.write_text(source, encoding="utf-8")
    namespace: dict = {"__name__": "scaffolded_adapter", "__file__": str(path)}
    exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    return namespace["FooVendorAdapter"]()


@pytest.mark.parametrize(("type_name", "template_slug"), _CASES, ids=[c[0] for c in _CASES])
def test_scaffold_constructs(type_name: str, template_slug: str, tmp_path: Path) -> None:
    adapter = _build_scaffolded_adapter(template_slug, tmp_path)
    assert isinstance(adapter, BackendAdapter)
    # `protocol_version` has no default on AdapterDescriptor, so a scaffold that omits it raises a
    # pydantic ValidationError at construction, long before any marker can be read.
    assert adapter.descriptor.protocol_version == 2


@pytest.mark.parametrize(("type_name", "template_slug"), _CASES, ids=[c[0] for c in _CASES])
def test_scaffold_method_signatures_match_the_contract(
    type_name: str, template_slug: str, tmp_path: Path
) -> None:
    adapter = _build_scaffolded_adapter(template_slug, tmp_path)
    for name in CONTRACT_METHODS:
        rendered = inspect.signature(getattr(type(adapter), name))
        declared = inspect.signature(getattr(BackendAdapter, name))
        assert rendered == declared, f"{name}: scaffold renders {rendered}, contract is {declared}"


@pytest.mark.parametrize(("type_name", "template_slug"), _CASES, ids=[c[0] for c in _CASES])
def test_scaffold_caches_no_client_on_self(
    type_name: str, template_slug: str, tmp_path: Path
) -> None:
    # R2 in openreading.testing.conformance: the constructor-injected `_client` is the only
    # client-shaped attribute an adapter may hold, because anything else strands a resumed job on
    # a fresh instance. A scaffold that seeds a second one is inviting the violation.
    adapter = _build_scaffolded_adapter(template_slug, tmp_path)
    client_attrs = {name for name in vars(adapter) if name.endswith("_client")}
    assert client_attrs <= {"_client"}, f"scaffold seeds extra client state: {client_attrs}"


def test_report_names_both_manual_touchpoints(tmp_path: Path, monkeypatch) -> None:
    """Two files a new backend needs are hand-edited, so both have to reach the printed report."""
    adapters_dir = tmp_path / "src" / "openreading" / "adapters"
    (adapters_dir / "reducto").mkdir(parents=True)
    template = REPO_ROOT / "src" / "openreading" / "adapters" / "reducto" / "adapter.py"
    (adapters_dir / "reducto" / "adapter.py").write_text(
        template.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("# openreading\n", encoding="utf-8")
    monkeypatch.setattr(_GENERATOR, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_GENERATOR, "ADAPTERS_DIR", adapters_dir)
    monkeypatch.setattr(_GENERATOR, "TESTS_DIR", tmp_path / "tests")
    monkeypatch.setattr(_GENERATOR, "_run_ruff", lambda paths: None)
    # The EDIT helpers each rewrite a tracked file. This test is about the report, not the edits,
    # so every one of them is stubbed out rather than pointed at a copy of the tree.
    for name in (
        "edit_registry",
        "edit_pyproject",
        "edit_env_example",
        "edit_test_descriptor_specs",
        "edit_test_server",
    ):
        monkeypatch.setattr(_GENERATOR, name, lambda *args, **kwargs: False)

    report = _GENERATOR.generate("foo-vendor", "reducto", "hosted_webhook", False)

    hand_lines = [line for line in report if line.startswith("HAND")]
    assert any("src/openreading/credentials.py" in line for line in hand_lines)
    assert any("src/openreading/adapters/README.md" in line for line in hand_lines)
    # The root README.md carries no adapter count, so the run must never claim to have edited it.
    assert not any(line.startswith("EDIT") and "README.md" in line for line in report)


def test_unsupported_template_message_does_not_claim_full_backend_coverage() -> None:
    # google-gemini is a registered backend with no scaffold template, so the message may not read
    # as though anthropic-claude were the only one left out.
    with pytest.raises(_GENERATOR.GeneratorError) as exc:
        _GENERATOR.generate("foo-vendor", "google-gemini", "hosted_api", False)
    message = str(exc.value)
    assert "Not every registered backend is a template." in message
    assert "—" not in message


def test_help_points_at_a_path_a_reader_can_open(capsys) -> None:
    with pytest.raises(SystemExit):
        _GENERATOR.main(["--help"])
    printed = " ".join(capsys.readouterr().out.split())
    assert "the module docstring of src/openreading/adapters/__init__.py" in printed
    assert "from the picker table in src/openreading/adapters/__init__.py §0" in printed
