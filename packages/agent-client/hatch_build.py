"""Build the client profile from an explicit subset of canonical Core source files.

The artifact uses the openreading distribution name so resolvers cannot co-install
an overlapping full-engine distribution. This profile is consumed from pinned Git,
not published as a replacement for the full Core wheel on a package index.
No module is copied or maintained separately in the source repository.
"""

import importlib.util
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        spec = importlib.util.spec_from_file_location(
            "client_build", Path(self.root) / "profile.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.stage = tempfile.TemporaryDirectory(prefix="openreading-client-wheel-")
        package = Path(self.stage.name) / "openreading"
        module.stage_package(package)
        build_data["force_include"][str(package)] = "openreading"
        build_data["force_include"][str(Path(self.root).parents[1] / "LICENSE")] = (
            "openreading/LICENSE"
        )

    def finalize(self, version, build_data, artifact_path):
        self.stage.cleanup()
