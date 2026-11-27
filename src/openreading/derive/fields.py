"""Check typed fields against the request's extraction schema (law C13 in ``openreading.derive``).

You get a ``SchemaCheck``: one report saying how a reply measures up against the schema you
sent, field by field. ``check_typed_fields(schema, typed_fields)`` computes it. It is pure and
deterministic, so the same schema and the same fields always give the same report. The router
calls it once per response through ``openreading.router.finalize.apply_schema_check``, never an
adapter.

What the check computes
-----------------------
The check validates a copy of ``{name: field.value}``. The copy omits every object key whose
value is ``null``, at any depth, because an LLM backend answers ``null`` for a field the document
lacks, and ``null`` against ``{"type": "string"}`` would read as a type error. Array elements
stay in place, so an index in an error path still points at the returned row. The response's
own values are never touched. Validation uses ``Draft202012Validator``, the pinned format checker
below, and an empty ``referencing.Registry``, so a remote ``$ref`` resolves to nothing and no
network request is made.

- ``fields`` holds one entry per top-level property, plus one per nested property that carries
  its own ``x-read-criticality``. A label inside ``items`` names a column of each row, reported
  once at its path without an index (``transactions.category``). Labels inside ``$ref`` targets
  are not followed.
- ``status`` is ``present``, ``missing`` or ``invalid``. A key that is absent or ``null``, or a
  blank string, is ``missing``. A value with a counted validation error at or below its path is
  ``invalid``. Missing beats invalid, so a blank string failing ``minLength`` is ``missing``. An
  empty list is ``present``, and a schema that needs rows says ``minItems``. A column is
  ``missing`` only when its list has rows and no row carries it.
- ``errors`` holds up to 50 ``{path, message}`` pairs in the validator's order. A path is dotted
  with numeric indices (``transactions.3.amount``). A ``required`` error reports the parent path
  plus the missing name. A message is cut at 300 characters, because a validator message can
  quote a whole returned array.
- An error under an ``ignore`` path stays in ``errors``. It is excluded from the status of every
  field above that path, and so from ``critical_ok``.
- ``valid`` is true only when the copy validates with no error at all. A schema the validator
  cannot use (an invalid keyword, an unresolvable ``$ref``, a recursive ``$ref``) reports
  ``valid: false`` with one error at path ``""``.
- ``extra`` lists returned top-level keys the schema does not declare, sorted. The check never
  removes them.
- ``schema_hash`` is ``sha256:`` plus the digest of the canonical JSON of the ORIGINAL schema,
  annotations included, so changing a label changes the hash.
- ``schema_ref`` names a bank entry the schema is. It is always ``null`` today.
- ``formats_unchecked`` lists the ``format`` names in the schema that core does not assert.

Format assertion
----------------
``FORMAT_CHECKER`` asserts exactly ``date`` (RFC 3339 full-date) and ``date-time`` (RFC 3339,
offset required), both with the standard library. Every other format is named in
``formats_unchecked`` and never asserted. The ``jsonschema`` default checker varies with the
installed extras, and two installs reporting different verdicts under one ``schema_hash`` is the
failure this avoids.

Criticality
-----------
``x-read-criticality`` labels a property ``critical``, ``expected`` (the default), ``optional``
or ``ignore``. ``unknown_criticality(schema)`` lists every label outside that set. The request
model rejects such a schema before any backend call, because a typo must not fall back to
``expected`` silently. ``strip_annotations`` removes every ``x-read-*`` keyword at any depth,
and only the stripped copy reaches a backend.

Warnings (``check_warnings``)
-----------------------------
``critical_field_missing`` and ``critical_field_invalid`` name a critical path in ``field``.
``intent_all_critical`` fires when two or more fields are labelled and every label is
``critical``, because such a schema has stopped ranking anything. The check never changes a
status, a value or a route.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from openreading.types.blocks import TypedField
from openreading.types.response import FieldCheck, SchemaCheck, SchemaCheckError

CRITICALITY_KEY = "x-read-criticality"
CRITICALITY_CODES = ("critical", "expected", "optional", "ignore")
DEFAULT_CRITICALITY = "expected"
ANNOTATION_PREFIX = "x-read-"
MAX_ERRORS = 50
MAX_MESSAGE_CHARS = 300
ASSERTED_FORMATS = frozenset({"date", "date-time"})
CHECK_WARNING_CODES = frozenset(
    {"critical_field_missing", "critical_field_invalid", "intent_all_critical"}
)

# Keywords whose value maps names to subschemas. A key inside one of them is a property name,
# never a keyword, so a property called `format` or `x-read-notes` is data and stays.
_NAME_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)

_FULL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")

FORMAT_CHECKER = FormatChecker(formats=())


@FORMAT_CHECKER.checks("date", raises=ValueError)
def _is_date(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    if not _FULL_DATE.fullmatch(instance):
        return False
    date.fromisoformat(instance)
    return True


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_date_time(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    m = _DATE_TIME.fullmatch(instance)
    if m is None:
        return False
    # RFC 3339 allows second 60 (a leap second), which datetime cannot hold.
    day, clock, _, offset = m.groups()
    clock = clock[:6] + "59" if clock.endswith(":60") else clock
    offset = "+00:00" if offset in ("Z", "z") else offset
    datetime.fromisoformat(f"{day}T{clock}{offset}")
    return True


# --------------------------------------------------------------------------- schema walking
def _subschemas(node: Mapping[str, Any]) -> Iterator[tuple[str, Any]]:
    """Yield (keyword, value) for every keyword of a schema node, with name maps expanded."""
    for key, value in node.items():
        if key in _NAME_MAPS and isinstance(value, Mapping):
            for sub in value.values():
                yield key, sub
        else:
            yield key, value


def _walk_nodes(value: Any) -> Iterator[Mapping[str, Any]]:
    """Every schema node reachable from `value`, keyword-aware (property names are not nodes)."""
    if isinstance(value, Mapping):
        yield value
        for _, sub in _subschemas(value):
            yield from _walk_nodes(sub)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_nodes(item)


def strip_annotations(schema: Any) -> Any:
    """A deep copy of `schema` without any `x-read-*` keyword, at any depth.

    Property names that start with `x-read-` are kept, because they are data, not keywords."""

    def strip(value: Any) -> Any:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, sub in value.items():
                if isinstance(key, str) and key.startswith(ANNOTATION_PREFIX):
                    continue
                if key in _NAME_MAPS and isinstance(sub, Mapping):
                    out[key] = {name: strip(s) for name, s in sub.items()}
                else:
                    out[key] = strip(sub)
            return out
        if isinstance(value, list):
            return [strip(item) for item in value]
        return copy.deepcopy(value)

    return strip(schema)


def has_annotations(schema: Any) -> bool:
    """True when `schema` carries any `x-read-*` keyword."""
    return any(
        isinstance(k, str) and k.startswith(ANNOTATION_PREFIX)
        for node in _walk_nodes(schema)
        for k in node
    )


def unknown_criticality(schema: Any) -> list[str]:
    """Every `x-read-criticality` value outside the four codes, as `repr` strings, in order."""
    bad: list[str] = []
    for node in _walk_nodes(schema):
        if CRITICALITY_KEY in node and node[CRITICALITY_KEY] not in CRITICALITY_CODES:
            bad.append(repr(node[CRITICALITY_KEY]))
    return bad


def schema_hash(schema: Any) -> str:
    """`sha256:` plus the hex digest of the canonical JSON of `schema` (sorted keys, no spaces)."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unchecked_formats(schema: Any) -> list[str]:
    names = {
        node["format"]
        for node in _walk_nodes(schema)
        if isinstance(node.get("format"), str) and node["format"] not in ASSERTED_FORMATS
    }
    return sorted(names)


def _object_properties(node: Any) -> Mapping[str, Any] | None:
    """The `properties` of an object node, or of an array node's `items`, else None."""
    if not isinstance(node, Mapping):
        return None
    props = node.get("properties")
    if isinstance(props, Mapping):
        return props
    items = node.get("items")
    if isinstance(items, Mapping) and isinstance(items.get("properties"), Mapping):
        return items["properties"]
    return None


def _label(node: Any) -> str | None:
    if isinstance(node, Mapping) and isinstance(node.get(CRITICALITY_KEY), str):
        return node[CRITICALITY_KEY]
    return None


def field_criticality(schema: Any) -> list[tuple[tuple[str, ...], str, bool]]:
    """The fields a check reports: (path, criticality, explicitly labelled), in schema order.

    One entry per top-level property, plus one per nested property with its own label."""
    top = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(top, Mapping):
        return []
    out: list[tuple[tuple[str, ...], str, bool]] = []

    def nested(node: Any, prefix: tuple[str, ...]) -> None:
        props = _object_properties(node)
        for name, sub in (props or {}).items():
            path = (*prefix, str(name))
            label = _label(sub)
            if label is not None:
                out.append((path, label, True))
            nested(sub, path)

    for name, sub in top.items():
        label = _label(sub)
        out.append(((str(name),), label or DEFAULT_CRITICALITY, label is not None))
        nested(sub, (str(name),))
    return out


# --------------------------------------------------------------------------- the check
def _prune_nulls(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _prune_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_prune_nulls(v) for v in value]
    return value


_ABSENT = object()


def _is_missing(value: Any) -> bool:
    return value is _ABSENT or value is None or (isinstance(value, str) and not value.strip())


def _values_at(value: Any, path: tuple[str, ...]) -> list[Any]:
    """Every value at `path` below `value`, fanning out over list elements (one per row)."""
    if not path:
        return [value]
    if isinstance(value, list):
        return [v for item in value for v in _values_at(item, path)]
    if isinstance(value, Mapping):
        return _values_at(value.get(path[0], _ABSENT), path[1:])
    return [_ABSENT]


def _under(path: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return path[: len(prefix)] == prefix


def _message(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 3] + "..."


def _error_path(error: Any) -> list[str | int]:
    path: list[str | int] = list(error.absolute_path)
    if error.validator == "required" and isinstance(error.instance, Mapping):
        for name in error.validator_value or []:
            if name not in error.instance and error.message.startswith(repr(name)):
                path.append(name)
                break
    return path


def _validate(schema: Any, instance: Any) -> tuple[list[list[str | int]], list[SchemaCheckError]]:
    """(error paths, reported errors). Paths cover every error, reports stop at MAX_ERRORS."""
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, registry=Registry(), format_checker=FORMAT_CHECKER)
        paths: list[list[str | int]] = []
        reported: list[SchemaCheckError] = []
        for error in validator.iter_errors(instance):
            path = _error_path(error)
            paths.append(path)
            if len(reported) < MAX_ERRORS:
                dotted = ".".join(str(p) for p in path)
                reported.append(SchemaCheckError(path=dotted, message=_message(error.message)))
        return paths, reported
    except SchemaError as e:
        reason = f"the extraction schema is not a valid JSON Schema: {e.message}"
    except Unresolvable as e:
        reason = f"the extraction schema has a reference core cannot resolve offline: {e}"
    except RecursionError:
        reason = "the extraction schema refers to itself without end"
    return [[]], [SchemaCheckError(path="", message=_message(reason))]


def check_typed_fields(
    schema: dict[str, Any], typed_fields: Mapping[str, TypedField] | None
) -> SchemaCheck:
    """Measure `typed_fields` against `schema` (the ORIGINAL, annotated schema). Pure."""
    fields_in = typed_fields or {}
    record = {name: tf.value for name, tf in fields_in.items()}
    entries = field_criticality(schema)
    error_paths, errors = _validate(strip_annotations(schema), _prune_nulls(record))
    # Error locations in schema terms: numeric indices dropped, so `transactions.3.amount` lands on
    # the column `transactions.amount` a label names.
    located = [tuple(str(p) for p in path if not isinstance(p, int)) for path in error_paths]
    ignored = [path for path, crit, _ in entries if crit == "ignore"]

    def counted(entry_path: tuple[str, ...]) -> bool:
        for loc in located:
            if not loc or not _under(loc, entry_path):
                continue
            # An ignore path strictly below this field hides its errors from this field.
            if any(p != entry_path and _under(p, entry_path) and _under(loc, p) for p in ignored):
                continue
            return True
        return False

    checks: list[FieldCheck] = []
    for path, crit, _ in entries:
        top = record.get(path[0], _ABSENT)
        if _is_missing(top):
            status = "missing"
        else:
            values = _values_at(top, path[1:])
            if values and all(_is_missing(v) for v in values):
                status = "missing"
            elif counted(path):
                status = "invalid"
            else:
                status = "present"
        # cast: the request model has already refused any label outside CRITICALITY_CODES.
        checks.append(FieldCheck(path=".".join(path), criticality=cast(Any, crit), status=status))

    declared = schema.get("properties") if isinstance(schema, Mapping) else None
    declared_names = set(declared) if isinstance(declared, Mapping) else set()
    return SchemaCheck(
        schema_hash=schema_hash(schema),
        schema_ref=None,
        valid=not errors,
        critical_ok=not any(c.criticality == "critical" and c.status != "present" for c in checks),
        fields=checks,
        extra=sorted(name for name in fields_in if name not in declared_names),
        errors=errors,
        formats_unchecked=_unchecked_formats(schema),
    )


def check_warnings(schema: Any, check: SchemaCheck) -> list[tuple[str, str, str | None]]:
    """The (code, message, field) warnings a check adds. Each code appears once per path."""
    out: list[tuple[str, str, str | None]] = []
    for c in check.fields:
        if c.criticality != "critical" or c.status == "present":
            continue
        if c.status == "missing":
            out.append(
                ("critical_field_missing", f"The critical field {c.path} is missing.", c.path)
            )
        else:
            out.append(
                (
                    "critical_field_invalid",
                    f"The critical field {c.path} does not satisfy the extraction schema.",
                    c.path,
                )
            )
    labels = [crit for _, crit, labelled in field_criticality(schema) if labelled]
    if len(labels) >= 2 and all(crit == "critical" for crit in labels):
        out.append(
            (
                "intent_all_critical",
                f"All {len(labels)} labelled fields are critical, so the labels rank nothing.",
                None,
            )
        )
    return out


def fill_field_types(schema: Any, typed_fields: Mapping[str, TypedField] | None) -> None:
    """Set `TypedField.type` from the schema's declared `type` where the adapter left it None.

    Only a single string type is carried. A type list such as `["string", "null"]` has no single
    scalar form, so the field keeps `None`. Values are never touched."""
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(props, Mapping):
        return
    for name, tf in (typed_fields or {}).items():
        spec = props.get(name)
        declared = spec.get("type") if isinstance(spec, Mapping) else None
        if tf.type is None and isinstance(declared, str):
            tf.type = declared
