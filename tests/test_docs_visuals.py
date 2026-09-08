"""Keep README diagrams accessible, scalable, and independent of rendering services.

Committed vectors must work in both GitHub color schemes without scripts, remote assets,
or HTML labels. The website may copy them, but core never needs a website checkout.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets" / "diagrams"
DIAGRAMS = json.loads((ASSETS / "manifest.json").read_text())
SVG = "{http://www.w3.org/2000/svg}"


@pytest.mark.parametrize("diagram", DIAGRAMS, ids=lambda item: item["name"])
def test_diagrams_are_accessible_standalone_vectors(diagram):
    source = (ASSETS / diagram["file"]).read_text()
    root = ElementTree.fromstring(source)
    assert root.tag == SVG + "svg"
    assert root.attrib["role"] == "img"
    title, description = root.find(SVG + "title"), root.find(SVG + "desc")
    assert title is not None and title.text
    assert description is not None and description.text
    assert "prefers-color-scheme:dark" in source
    _, _, width, height = map(float, root.attrib["viewBox"].split())
    assert width > 0 and height > 0
    for element in root.iter():
        assert element.tag not in {SVG + "script", SVG + "image", SVG + "foreignObject"}
        for attr, value in element.attrib.items():
            if attr.endswith("href"):
                assert value.startswith("#"), "vectors must not fetch external resources"
    readme = (ROOT / diagram["source"]).read_text()
    assert diagram["file"] in readme
