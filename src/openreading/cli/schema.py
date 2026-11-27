"""`openreading schema list|show`: read the schema bank from the command line.

You get the bank's names before you write a schema. `schema list` prints each entry's name,
version, field counts per criticality code and purpose, and `schema list --json` prints the same
entries as data without the schemas themselves. `schema show NAME` prints one resolved schema as
JSON, labels included, so you can send it as `json_schema` over HTTP or copy it into a file you
edit. The user-facing chapter is the `schema` section of the `openreading.cli` docstring.

Both verbs call `openreading.schema_bank.resolve` and `entries`, so `show` takes a bank name and
never a path (law SX8). An unknown name or version exits 2 and lists the names. Exit 0 covers a
completed command.
"""

from __future__ import annotations

import argparse
import json
import sys

from openreading import schema_bank
from openreading.derive.fields import CRITICALITY_CODES

EPILOGS: dict[str, str] = {
    "schema": """\
Examples:
  openreading schema list                  # the bank's names and purposes
  openreading schema show rfq              # one schema, as JSON
  openreading parse rfq.pdf --backend google-gemini --schema rfq

The bank is the set of extraction schemas that ship inside OpenReading, so a
first extraction starts from a name instead of a blank file. NAME is the
latest version and NAME@v1 pins one.

Then:
  openreading schema show rfq > my_rfq.json   # copy one, then edit it

Exits: 0 ok. 2 an unknown name or version, with the names in the message.

More: openreading help schema""",
    "schema list": """\
Examples:
  openreading schema list           # name, version, fields per code, purpose
  openreading schema list --json    # the same entries as a JSON array

The four count columns are how many fields carry each criticality code. A
bank entry labels only what its reader would act on, and generic labels none.

Then:
  openreading schema show bank_statement   # read one in full
  openreading parse doc.pdf --backend anthropic-claude --schema bank_statement

Exits: 0 always.

More: openreading help schema""",
    "schema show": """\
Examples:
  openreading schema show bank_statement           # latest version, as JSON
  openreading schema show bank_statement@v1        # pin version 1
  openreading schema show rfq > my_rfq.json        # start your own from it

The JSON keeps the x-read-criticality labels, which only core reads. No
backend receives them. Send the output as json_schema to an HTTP server.

Then:
  openreading parse rfq.pdf --backend google-gemini --schema ./my_rfq.json

Exits: 0 ok. 2 an unknown name or version. A path is never a name, so
`schema show ./rfq` exits 2 too.

More: openreading help schema""",
}


def cmd_schema_list(args: argparse.Namespace) -> int:
    """`schema list [--json]`: one row per bank entry."""
    entries = schema_bank.entries()
    if args.json:
        print(json.dumps([e.to_dict() for e in entries], indent=2))
        return 0
    header = ["NAME", "VERSION", *(c.upper() for c in CRITICALITY_CODES), "PURPOSE"]
    rows = [
        [
            e.name,
            f"v{e.version}",
            *(str(e.label_counts[c]) for c in CRITICALITY_CODES),
            e.purpose,
        ]
        for e in entries
    ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header) - 1)]
    for row in [header, *rows]:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])]
        print("  ".join([*cells, row[-1]]))
    return 0


def cmd_schema_show(args: argparse.Namespace) -> int:
    """`schema show NAME`: the resolved schema as JSON, labels included."""
    try:
        entry = schema_bank.resolve(args.name)
    except schema_bank.UnknownSchemaError as e:
        print(f"[schema] {e}", file=sys.stderr)
        return 2
    print(json.dumps(entry.schema, indent=2, ensure_ascii=False))
    return 0


def add_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """Register `schema` and its two verbs on the top-level subparser."""
    schema = sub.add_parser(
        "schema",
        parents=[common],
        help="list or print the extraction schemas that ship with OpenReading",
        description="Read the schema bank: named extraction schemas you pass to parse --schema.",
    )
    verbs = schema.add_subparsers(dest="schema_command", required=True, metavar="VERB")

    list_p = verbs.add_parser(
        "list",
        help="name, version, fields per criticality code and purpose of each entry",
        description="List every bank schema with its version, how many fields carry each "
        "criticality code, and what it is for.",
    )
    list_p.add_argument("--json", action="store_true", help="print the entries as a JSON array")
    list_p.set_defaults(func=cmd_schema_list)

    show_p = verbs.add_parser(
        "show",
        help="print one bank schema as JSON, labels included",
        description="Print one bank schema as JSON, with its x-read-criticality labels.",
    )
    show_p.add_argument(
        "name", metavar="NAME", help="a bank name such as rfq, or rfq@v1 to pin a version"
    )
    show_p.set_defaults(func=cmd_schema_show)
