"""Load immutable schema resources and cache their checked validators.

Both the full engine and client profile use these same validation functions.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

_PACKAGE = "openreading.schemas"


@cache
def _load(name: str) -> dict[str, Any]:
    with resources.files(_PACKAGE).joinpath(name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


_VALIDATOR_CACHE: dict[int, tuple[dict[str, Any], Any]] = {}


def _validator(schema: dict[str, Any]):
    # Cached per (family, version) — i.e. per schema object identity. `_load` (above) is `@cache`d
    # per filename, so every `*_schema()` call site returns the SAME dict object for its family;
    # keying on `id(schema)` here is therefore stable for the process lifetime, and storing the
    # schema itself alongside its validator keeps that object referenced so its id can never be
    # reused by something else. Without this, `cls.check_schema(schema)` re-validated the schema
    # itself against the 2020-12 metaschema on every single call (BL-167): ~15ms per call, paid in
    # full by every request the tiniest envelope included. This correctness depends on `_load`
    # never returning a fresh object for the same filename — do not add `_load.cache_clear()` or a
    # defensive copy there without re-deriving this cache's keying strategy too.
    cached = _VALIDATOR_CACHE.get(id(schema))
    if cached is not None:
        return cached[1]
    from jsonschema.validators import validator_for

    cls = validator_for(schema)
    cls.check_schema(schema)  # raises if the schema itself is not a valid JSON Schema
    validator = cls(schema)
    _VALIDATOR_CACHE[id(schema)] = (schema, validator)
    return validator
