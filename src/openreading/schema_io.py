"""Read an extraction schema file and check it before any document is opened.

`load_schema(path)` is the one reader behind `openreading parse --schema` and
`openreading.load_schema`. It returns the plain dict you pass as
`extraction_schema={"json_schema": ...}`. Every failure raises `SchemaFileError`, a `ValueError`
whose message names the file, so the CLI prints it and exits 2.

The contract, in the order the checks run:

1. The parser follows the extension. `.yaml` and `.yml` use `yaml.safe_load`. Any other name,
   including none, is JSON.
2. The top level is an object.
3. Every value is plain JSON: dict with string keys, list, str, int, float, bool or None.
   `yaml.safe_load` turns an unquoted `2024-01-01` into a date object, and a JSON request cannot
   carry one. The error names the dotted path (`properties.opened.default`) and says to quote it.
   Unquoted `on` or `no` become booleans in YAML 1.1. The schema check below catches that when
   the value lands in a keyword that expects a string.
4. The object is a valid JSON Schema, and every `x-read-criticality` label is a known code.

Checking here avoids a backend's own 400 after a billable upload. A schema that is valid but uses
keywords a backend does not support stays that backend's to refuse.

Design decision: YAML is a file format only. Both formats feed one grammar and one hash, so the
same schema in either file gives the same request and the same `schema_check.schema_hash`.

Sibling modules: `openreading.derive.fields` owns the criticality codes and the hash, and
`openreading.cli.app` maps `SchemaFileError` to exit 2.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_YAML_SUFFIXES = {".yaml", ".yml"}


class SchemaFileError(ValueError):
    """A schema file that cannot be read, parsed or accepted as a JSON Schema."""


def load_schema(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return the parsed, checked JSON Schema held in the file at `path`.

    Raises `SchemaFileError` when the file cannot be read, is not valid JSON or YAML, is not an
    object, holds a value JSON cannot carry, is not a valid JSON Schema, or has an unknown
    `x-read-criticality` label.
    """
    from jsonschema import exceptions, validators

    from openreading.derive.fields import CRITICALITY_CODES, unknown_criticality

    name = os.fspath(path)
    is_yaml = Path(name).suffix.lower() in _YAML_SUFFIXES
    try:
        text = Path(name).read_text()
    except OSError as e:
        raise SchemaFileError(f"cannot read {name}: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise SchemaFileError(f"{name} is not valid UTF-8 text") from e
    schema: Any
    if is_yaml:
        import yaml

        try:
            schema = yaml.safe_load(text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" at line {mark.line + 1} column {mark.column + 1}" if mark else ""
            reason = getattr(e, "problem", None) or "syntax error"
            raise SchemaFileError(f"{name} is not valid YAML: {reason}{where}") from e
    else:
        try:
            schema = json.loads(text)
        except json.JSONDecodeError as e:
            raise SchemaFileError(
                f"{name} is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}"
            ) from e
    if not isinstance(schema, dict):
        kind = "YAML" if is_yaml else "JSON"
        raise SchemaFileError(
            f"{name} must hold a {kind} object (a JSON Schema), not a {type(schema).__name__}"
        )
    if is_yaml:
        bad = _first_non_json(schema)
        if bad:
            where, value = bad
            raise SchemaFileError(
                f"{name} has a {type(value).__name__} value at {where}, which JSON cannot carry; "
                "put quotes around it"
            )
    try:
        validators.validator_for(schema).check_schema(schema)
    except exceptions.SchemaError as e:
        reason = " ".join(e.message.split())
        raise SchemaFileError(f"{name} is not a valid JSON Schema: {reason}") from e
    # The request model refuses an unknown criticality label too. Checking here names the file.
    unknown = unknown_criticality(schema)
    if unknown:
        raise SchemaFileError(
            f"{name} has unknown x-read-criticality {', '.join(unknown)}; "
            f"use one of {', '.join(CRITICALITY_CODES)}"
        )
    return schema


def _first_non_json(value: Any, path: str = "") -> tuple[str, Any] | None:
    """The dotted path and value of the first thing JSON cannot carry, else None."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return None
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                return (f"{path}.{key}" if path else str(key), key)
            found = _first_non_json(child, f"{path}.{key}" if path else key)
            if found:
                return found
        return None
    if isinstance(value, list):
        for i, child in enumerate(value):
            found = _first_non_json(child, f"{path}[{i}]" if path else f"[{i}]")
            if found:
                return found
        return None
    return (path or "(top level)", value)
