"""Distribution selections and isolated dependency audits must match their release inputs."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_wheel_includes_only_existing_explicit_schema_sources():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/openreading"]
    for source in wheel.get("force-include", {}):
        assert (ROOT / source).exists(), source
    assert len(list((ROOT / "src/openreading/schemas").glob("*.json"))) >= 7


def test_sdist_selects_build_inputs_without_repository_data():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sources = config["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]
    assert set(sources) == {"src/openreading", "pyproject.toml", "README.md", "LICENSE"}
    assert all((ROOT / source).exists() for source in sources)


def test_client_lock_has_a_pinned_ci_audit_and_dependency_updates():
    lock = tomllib.loads((ROOT / "packages/agent-client/uv.lock").read_text())
    assert "mcp" in {package["name"] for package in lock["package"]}
    assert not any("git" in package["source"] for package in lock["package"])
    makefile = (ROOT / "Makefile").read_text()
    audit = makefile.split("\naudit-client:\n", 1)[1].split("\n\n", 1)[0]
    assert "--project packages/agent-client --locked" in audit
    assert "pip-audit==2.10.1 pip-audit --strict" in audit
    assert "make audit-client" in (ROOT / ".github/workflows/ci.yml").read_text()
    assert "directory: /packages/agent-client" in (ROOT / ".github/dependabot.yml").read_text()
