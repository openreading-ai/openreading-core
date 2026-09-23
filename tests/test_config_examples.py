"""Keep the downloadable configuration examples usable without filling in missing fragments.

Every file passes the public loader, world validation, and normalization against the real
backend registry. No references are stubbed and no credentials or network calls are required.
The README index must include each file so new examples remain discoverable.
"""

import json
from pathlib import Path

import pytest

from openreading.strategies.loader import load_config
from openreading.strategies.normalize import normalize_config
from openreading.strategies.validate import validate_config
from openreading.testing.sample_pdf import build_sample_pdf

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "configs"
CONFIGS = sorted(EXAMPLES.glob("*.yaml"))


def test_config_examples_are_present_and_linked():
    assert len(CONFIGS) >= 16
    index = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    for path in CONFIGS:
        assert f"]({path.name})" in index, path.name
    assert "examples/configs/README.md" in (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", CONFIGS, ids=lambda path: path.stem)
def test_standalone_config_loads_validates_and_normalizes(path):
    loaded = load_config(path)
    assert loaded is not None
    issues = validate_config(loaded.config, raw=loaded.raw, plain_info=loaded.plain_info)
    assert not issues, "\n".join(f"{issue.level} {issue.path}: {issue.message}" for issue in issues)
    normalized = normalize_config(loaded.config)
    assert set(normalized) == set(loaded.config.strategies)
    if loaded.config.strategies:
        assert loaded.config.defaults.strategy in normalized
    else:
        assert loaded.config.policy.backends


@pytest.mark.parametrize(
    ("filename", "selection"),
    [
        ("01-single-backend.yaml", ["--no-strategy"]),
        ("04-local-quality.yaml", ["--strategy", "main"]),
    ],
)
def test_documented_cli_selection_parses_locally(
    filename, selection, tmp_path, monkeypatch, capsys
):
    from openreading.cli.app import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    document = tmp_path / "sample.pdf"
    document.write_bytes(build_sample_pdf())
    code = main(["parse", str(document), "--config", str(EXAMPLES / filename), *selection])
    assert code == 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"]["state"] == "succeeded"
    assert response["backend"]["id"] == "pymupdf"
