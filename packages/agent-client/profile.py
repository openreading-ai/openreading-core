"""Select the canonical contracts, retention and stdio modules for agent distribution.

Namespace initializers deliberately expose only the client profile. Full-engine manuals,
parser configuration and public parsing functions belong to the independent server install.
Unknown modules cannot enter this profile through recursive package collection.
"""

import shutil
import tomllib
from pathlib import Path

MODULES = (
    "artifacts.constants",
    "artifacts.limits",
    "artifacts.intake",
    "artifacts.models",
    "artifacts.store",
    "artifacts.passages",
    "artifacts.search",
    "artifacts.document",
    "artifacts.delivery",
    "artifacts.retained",
    "artifacts.retention",
    "artifacts.jobs",
    "mcp_server.session",
    "mcp_server.tools",
    "mcp_server.delivery",
    "mcp_server.selection",
    "mcp_server.selection_pages",
    "mcp_server.transport",
    "types.blocks",
    "types.enums",
    "types.geometry",
    "types.response",
    "types.import_job",
    "types.selection",
    "schemas.client",
    "schemas._validation",
)
SCHEMAS = (
    "response.v0.3.json",
    "local-document.v0.5.json",
    "passage.v0.4.json",
    "selection-tool.v0.2.json",
    "agent-document-tool.v0.5.json",
    "document-tool.v0.4.json",
    "import-job.v0.4.json",
)


def stage_package(target: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    source = root / "src/openreading"
    target.mkdir(parents=True, exist_ok=False)
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    profile = tomllib.loads(Path(__file__).with_name("pyproject.toml").read_text())
    if profile["project"]["version"] != version:
        raise ValueError("Client and Core versions differ.")
    for package in ("", "artifacts", "mcp_server", "types"):
        directory = target / package
        directory.mkdir(exist_ok=True)
        (directory / "__init__.py").write_text(
            '"""OpenReading client contracts and retained document tools."""\n'
            + (f'__version__ = {version!r}\nSCHEMA_VERSION = "0.1"\n' if not package else "")
        )
    (target / "schemas").mkdir()
    (target / "schemas/__init__.py").write_text(
        '"""Expose only client response and document-tool contracts."""\n'
        "from openreading.schemas.client import *\n"
    )
    for name in MODULES:
        relative = name.replace(".", "/") + ".py"
        shutil.copy2(source / relative, target / relative)
    for name in SCHEMAS:
        shutil.copy2(source / "schemas" / name, target / "schemas" / name)
