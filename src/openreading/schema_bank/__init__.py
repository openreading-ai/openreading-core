"""The schema bank: named, versioned extraction schemas that ship inside core.

You get a first extraction that starts from a name, not from a blank JSON Schema file. `openreading
parse --schema rfq` sends the bank's `rfq` schema, and `openreading schema list` shows what is in
the bank. This is the one curated catalog core ships (AGENTS.md, "What belongs in this repo").

The contract
------------
- **Entries.** Each entry is a YAML file in this package, named `NAME.vN.yaml`, and its first line
  is a comment `# NAME.vN: PURPOSE` that carries the one-line purpose. The five entries are
  `generic`, `bank_statement`, `rfq`, `drawing_title_block` and `clinical_trial_agreement`. The
  file is a JSON Schema in YAML form. The same grammar and the same `schema_hash` apply as for a
  file you write yourself (`openreading.schema_io`).
- **Versions.** `bank_statement` means the latest version of that name, and `bank_statement@v1`
  pins one. A released entry never changes in place, because a pinned name must keep meaning the
  same fields. A content change ships as a new `.v2.yaml` file beside the old one.
- **Inclusion rule.** An entry enters the bank only with its own proof. It needs a synthetic
  example document in `examples/bank/` and an offline test that the schema accepts the expected
  record for that document. Each `x-read-criticality` label in a domain schema carries a YAML
  comment saying why that field has that level. `generic` carries no label at all. Before an
  entry is released, `make verify-live` sends it once to every backend whose descriptor claims
  `custom_schema_extraction` and that has a key (`tests/test_schema_bank_live.py`). A strict
  backend can refuse a schema that validates offline, and that failure stays invisible without
  the keyed run.
- **Law SX6.** Core never applies criticality you did not choose. A domain schema's labels apply
  only when you name it, and each label states its reason.

Resolution
----------
`openreading.schema_io.load_schema(VALUE)` and `openreading parse --schema VALUE` share one rule
(law SX7). A value with a path separator, or ending in `.json`, `.yaml` or `.yml`, is a file.
Anything else is a bank name with an optional `@vN` pin. No filesystem lookup decides between
the two, so `--schema rfq` names the same entry in every directory, even where a file named
`rfq` exists. You write `./rfq` for that file.

`resolve(name)` is the name half and takes only `^[a-z][a-z0-9_]*(@v[0-9]+)?$`. It reads package
data and no other file (law SX8). A path, an extension or an unknown name raises
`UnknownSchemaError`, a `ValueError` that carries `name` and `known`. An agent tool or an HTTP
field that receives a name from outside calls `resolve`, never `load_schema`, so a model-chosen
string such as `../notes/keys.yaml` is never read from disk and sent to a hosted backend.

Provenance
----------
`schema_check.schema_ref` is `"bank:NAME@vN"` when the schema a run sent is exactly a bank entry.
`ref_for_hash(schema_hash)` derives it by matching the hash against the entries, so core states
only what it can verify. A request field naming the entry would be a claim a caller could pair
with any other schema. A schema you supply gets `null`, unless it is an exact copy of an entry.
The journaled request carries the resolved `json_schema`, so a replay stays exact after a bank
file changes.

Exports
-------
- `BankEntry`: `name`, `version`, `ref`, `purpose`, `schema_hash`, `label_counts` (one count per
  criticality code) and `schema`. `to_dict(include_schema=False)` is what `schema list --json`
  prints.
- `entries()`: every entry, ordered by name and then version.
- `resolve(name)`: one entry, a copy you may change.
- `ref_for_hash(schema_hash)`: the `ref` of the entry with that hash, else `None`.
- `UnknownSchemaError`.

Known gaps: `--extract` without `--schema` does not yet apply `generic`, and a request field that
names an entry by name is not built. The design records are `design/` in the company repo.

Sibling modules: `openreading.derive.fields` owns the criticality codes and the hash,
`openreading.schema_io` owns the path rule, and `openreading.cli.app` maps the error to exit 2.
The client wheel never loads this package, because it parses YAML.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from openreading.derive.fields import (
    CRITICALITY_CODES,
    CRITICALITY_KEY,
    _walk_nodes,
    schema_hash,
)

__all__ = ["BankEntry", "UnknownSchemaError", "entries", "ref_for_hash", "resolve"]

# The only shape `resolve` accepts. It excludes every separator, dot and extension on purpose.
_NAME = re.compile(r"^[a-z][a-z0-9_]*(@v[0-9]+)?$")
_FILE = re.compile(r"^([a-z][a-z0-9_]*)\.v([0-9]+)\.yaml$")
_PURPOSE = re.compile(r"^#\s*[a-z][a-z0-9_]*\.v[0-9]+:\s*(.+?)\s*$")


class UnknownSchemaError(ValueError):
    """A bank name or version that does not exist, or a value that is not a bank name.

    `name` is the value as given and `known` lists the bank's names, so a caller can answer
    without parsing the message."""

    def __init__(self, name: str, known: list[str], hint: str = "") -> None:
        self.name = name
        self.known = known
        super().__init__(f"unknown schema {name!r}; the bank has {', '.join(known)}{hint}")


@dataclass(frozen=True)
class BankEntry:
    """One schema in the bank."""

    name: str
    version: int
    purpose: str
    schema_hash: str
    label_counts: dict[str, int]
    schema: dict[str, Any]

    @property
    def ref(self) -> str:
        """The name `schema_check.schema_ref` carries, such as `bank:rfq@v1`."""
        return f"bank:{self.name}@v{self.version}"

    def to_dict(self, include_schema: bool = False) -> dict[str, Any]:
        """The entry as plain data. `schema` is included only when asked."""
        out: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "ref": self.ref,
            "purpose": self.purpose,
            "schema_hash": self.schema_hash,
            "label_counts": dict(self.label_counts),
        }
        if include_schema:
            out["schema"] = copy.deepcopy(self.schema)
        return out


def _label_counts(schema: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(CRITICALITY_CODES, 0)
    for node in _walk_nodes(schema):
        label = node.get(CRITICALITY_KEY)
        if label in counts:
            counts[label] += 1
    return counts


@cache
def _load() -> tuple[BankEntry, ...]:
    """Read every `NAME.vN.yaml` in this package. Package data is the only thing opened."""
    import yaml

    found: list[BankEntry] = []
    for item in resources.files("openreading.schema_bank").iterdir():
        match = _FILE.match(item.name)
        if not match:
            continue
        text = item.read_text(encoding="utf-8")
        purpose = _PURPOSE.match(text.splitlines()[0])
        if purpose is None:  # pragma: no cover - the bank tests fail first
            raise ValueError(f"{item.name} must open with '# NAME.vN: PURPOSE'")
        schema = yaml.safe_load(text)
        found.append(
            BankEntry(
                name=match.group(1),
                version=int(match.group(2)),
                purpose=purpose.group(1),
                schema_hash=schema_hash(schema),
                label_counts=_label_counts(schema),
                schema=schema,
            )
        )
    return tuple(sorted(found, key=lambda e: (e.name, e.version)))


def entries() -> list[BankEntry]:
    """Every entry in the bank, ordered by name and then version. Reads package data only."""
    return [copy.deepcopy(e) for e in _load()]


def _names() -> list[str]:
    return sorted({e.name for e in _load()})


def resolve(name: str) -> BankEntry:
    """The entry for `name` or `name@vN`, with a bare name meaning the latest version.

    Accepts only a bank name and reads package data, never a file you point it at (law SX8).
    Raises `UnknownSchemaError` for anything else, a path included."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise UnknownSchemaError(str(name), _names())
    base, _, pin = name.partition("@v")
    versions = [e for e in _load() if e.name == base]
    if pin:
        versions = [e for e in versions if e.version == int(pin)]
    if not versions:
        raise UnknownSchemaError(name, _names())
    return copy.deepcopy(max(versions, key=lambda e: e.version))


def ref_for_hash(hash_: str) -> str | None:
    """The `ref` of the bank entry whose `schema_hash` is `hash_`, else None."""
    for entry in _load():
        if entry.schema_hash == hash_:
            return entry.ref
    return None
