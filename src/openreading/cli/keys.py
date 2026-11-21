"""`openreading keys set|list|unset|import`: manage backend keys in the OS keychain.

You get a key that works from every directory without editing a `.env`. `keys set SLUG` asks
for each secret the backend declares with hidden input and saves it to your OS keychain
(`openreading.keychain`). `keys list` shows where each declared secret resolves from. `keys
unset` removes a backend's saved secrets. `keys import PATH` moves the secrets in a `.env` into
the keychain and names the lines to delete. The user-facing chapter is the `keys` section of the
`openreading.cli` docstring.

The rules every verb follows
----------------------------
- A value never crosses argv, stdout, stderr or a log line (law PS4). There is no value flag,
  because argv shows up in `ps` and in shell history. `--stdin` is the scripted path.
- Only secrets a backend declares reach the keychain (law PS5). `keys set` takes a backend slug,
  and an optional variable must be one of that backend's secret variables. A setting such as
  `ANTHROPIC_MODEL` or a knob such as `OPENREADING_LEDGER` is refused with exit 2.
- A provider profile in the project's `openreading.yaml` is a backend like any other
  (`openreading.adapters.registry.known_backends`). Its one secret is the variable its `key_env`
  names, so `keys set openrouter-gpt` saves `OPENROUTER_API_KEY`. A profile with no `key_env`
  has nothing to save and exits 2.
- No acceptable keychain means exit 2 with the sentence `openreading.keychain` raises, and
  nothing is written anywhere else (law PS6).
- A probe is a network call, so `keys set` offers one only at an interactive terminal, and only
  after asking.

`--stdin` reads one line per secret the command selects, in descriptor order. For example,
`aws-textract` reads `AWS_ACCESS_KEY_ID`, then `AWS_SECRET_ACCESS_KEY`, then the optional
`AWS_SESSION_TOKEN`. A blank or absent line skips that variable. More lines than secrets is
refused before anything is saved, because a shifted line would save a key under the wrong name.
Name one variable after the slug to save just that one.

Exit codes: 0 done, 2 usage (unknown slug, undeclared variable, no terminal without `--stdin`,
no keychain), 3 an unreadable `keys import` file, 130 stopped with Ctrl+C.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Callable

from openreading import keychain
from openreading.adapters.registry import known_backends, make_adapter
from openreading.credentials import EnvCredentialBroker, dotenv_entries
from openreading.liveness import check_liveness
from openreading.types.descriptor import AdapterDescriptor, CredentialField
from openreading.types.liveness import LivenessStatus

EPILOGS: dict[str, str] = {
    "keys": """\
Examples:
  openreading keys set anthropic-claude      # hidden prompt, then saved
  openreading keys list                      # where each key comes from
  openreading keys import .env               # move a .env's keys over

Keys live in your OS keychain, never in a file. Your shell and .env still win
over the keychain, so an export you made for one shell is never overridden.

Then:
  openreading backends               # CONFIGURED flips to yes

Exits: 0 done. 2 usage, no terminal, or no keychain. 3 an unreadable import
file. 130 stopped with Ctrl+C.

More: openreading help keys""",
    "keys set": """\
Examples:
  openreading keys set anthropic-claude          # asks, with hidden input
  openreading keys set aws-textract AWS_SECRET_ACCESS_KEY   # one variable
  pass show reducto | openreading keys set reducto --stdin  # from a script

There is no flag that takes a value: argv shows up in ps and shell history.
--stdin reads one line per secret, in the order `keys list` prints them, and
a blank line skips one. At a terminal it offers one probe, after asking.

Then:
  openreading keys list              # SOURCE reads keychain
  openreading backends --check anthropic-claude   # probe it later

Exits: 0 saved or skipped. 2 unknown slug, a variable the backend does not
declare as a secret, no terminal without --stdin, or no keychain. 130 Ctrl+C.

More: openreading help keys""",
    "keys list": """\
Examples:
  openreading keys list              # BACKEND VAR SOURCE
  openreading keys list --json       # the same rows as JSON

SOURCE is env (exported), .env, keychain, or missing. It never shows a value,
a prefix or a length. Reading a keychain that is absent or locked is silent:
those rows read missing.

Then:
  openreading keys set SLUG          # fill a missing row

Exits: 0 always.

More: openreading help keys""",
    "keys unset": """\
Examples:
  openreading keys unset reducto                 # every saved reducto secret
  openreading keys unset aws-textract AWS_SESSION_TOKEN   # just one

A variable can be shared. The four llamaparse slugs read one key, so removing
it for one removes it for all four, and the output names the others.

Then:
  openreading keys list              # the row now reads missing or env

Exits: 0 removed or nothing saved. 2 unknown slug, undeclared variable, or no
keychain.

More: openreading help keys""",
    "keys import": """\
Examples:
  openreading keys import .env       # move declared secrets to the keychain
  openreading keys import ~/old/project/.env

The file is never edited. The output names the lines to delete, because a
.env line still beats the keychain whenever you run from that folder. Settings
such as ANTHROPIC_MODEL stay where they are.

Then:
  openreading keys list              # SOURCE reads keychain once deleted

Exits: 0 moved, or nothing to move. 2 no keychain. 3 the file cannot be read.

More: openreading help keys""",
}


def add_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """Register `keys` and its four verbs on the top-level subparser."""
    keys = sub.add_parser(
        "keys",
        parents=[common],
        help="save, list, remove or import backend keys in the OS keychain",
        description="Save backend keys in your OS keychain once, so every directory and every "
        "session finds them, and see where each key comes from.",
    )
    verbs = keys.add_subparsers(dest="keys_command", required=True, metavar="VERB")

    set_p = verbs.add_parser(
        "set",
        help="save a backend's secrets, asking with hidden input",
        description="Ask for each secret a backend declares, with hidden input, and save it to "
        "the OS keychain.",
    )
    set_p.add_argument("slug", metavar="SLUG", help="the backend (`openreading backends`)")
    set_p.add_argument(
        "vars",
        nargs="*",
        metavar="VAR",
        help="save only these secret variables (default: every secret the backend declares)",
    )
    set_p.add_argument(
        "--stdin",
        action="store_true",
        help="read one line per secret from stdin instead of asking (for scripts)",
    )
    set_p.set_defaults(func=cmd_keys_set)

    list_p = verbs.add_parser(
        "list",
        help="show where each declared secret resolves from, never its value",
        description="Print one row per backend secret: the backend, the variable, and whether "
        "it comes from the environment, .env, the keychain, or nowhere.",
    )
    list_p.add_argument("--json", action="store_true", help="print the rows as a JSON array")
    list_p.set_defaults(func=cmd_keys_list)

    unset_p = verbs.add_parser(
        "unset",
        help="remove a backend's secrets from the OS keychain",
        description="Remove the secrets a backend declares from the OS keychain.",
    )
    unset_p.add_argument("slug", metavar="SLUG", help="the backend (`openreading backends`)")
    unset_p.add_argument(
        "vars", nargs="*", metavar="VAR", help="remove only these secret variables"
    )
    unset_p.set_defaults(func=cmd_keys_unset)

    import_p = verbs.add_parser(
        "import",
        help="move the declared secrets in a .env into the OS keychain",
        description="Copy every declared backend secret in a .env file into the OS keychain, "
        "then name the lines to delete from that file.",
    )
    import_p.add_argument("path", metavar="PATH", help="the .env file to read")
    import_p.set_defaults(func=cmd_keys_import)


# --- shared --------------------------------------------------------------------------------------


def _err(message: str) -> None:
    print(f"[keys] {message}", file=sys.stderr)


def _descriptors() -> dict[str, AdapterDescriptor]:
    """Every known backend's descriptor, provider profiles from `openreading.yaml` included."""
    return {slug: make_adapter(slug).descriptor for slug in sorted(known_backends())}


def _secret_fields(desc: AdapterDescriptor) -> list[CredentialField]:
    return [f for f in desc.credentials_spec if f.secret and f.env]


def _secret_names(desc: AdapterDescriptor) -> list[str]:
    return [name for f in _secret_fields(desc) for name in f.env]


def _sharers(name: str, slug: str, descs: dict[str, AdapterDescriptor]) -> list[str]:
    return [s for s, d in descs.items() if s != slug and name in _secret_names(d)]


def _select(
    slug: str, wanted: list[str], verb: str
) -> tuple[AdapterDescriptor, list[tuple[CredentialField, str]]] | int:
    """Resolve SLUG and optional VARs to `(field, variable)` pairs, or print why not and return
    the exit code. With no VARs, every secret field is selected under its primary variable."""
    descs = _descriptors()
    if slug not in descs:
        from openreading.cli.app import _unknown_backends_line

        print(_unknown_backends_line(f"keys {verb}", [slug], descs), file=sys.stderr)
        return 2
    desc = descs[slug]
    fields = _secret_fields(desc)
    if not fields:
        _err(f"{slug} declares no secret to save. It needs no key.")
        return 2
    declared = ", ".join(_secret_names(desc))
    if not wanted:
        return desc, [(f, f.env[0]) for f in fields]
    settings = {name for f in desc.config_spec for name in f.env}
    pairs: list[tuple[CredentialField, str]] = []
    for name in dict.fromkeys(wanted):
        field = next((f for f in fields if name in f.env), None)
        if field is not None:
            pairs.append((field, name))
        elif name in settings:
            _err(
                f"{name} is a setting, not a secret, so it stays in the environment or "
                f"openreading.yaml. {slug}'s secrets: {declared}."
            )
            return 2
        else:
            _err(f"{name} is not a secret {slug} declares. Its secrets: {declared}.")
            return 2
    return desc, pairs


def _keychain_label() -> str | int:
    try:
        return keychain.backend_name()
    except keychain.KeychainUnavailable as e:
        _err(str(e))
        return 2


# --- keys set ------------------------------------------------------------------------------------


def _read_stdin_values(names: list[str]) -> list[str] | int:
    lines = sys.stdin.read().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) > len(names):
        _err(
            f"stdin has {len(lines)} lines for {len(names)} secret(s) ({', '.join(names)}). "
            "Send one line per secret, in that order."
        )
        return 2
    values = [line.strip() for line in lines] + [""] * (len(names) - len(lines))
    if not any(values):
        _err("stdin carried no value. Send one line per secret.")
        return 2
    return values


def _shadow_note(name: str) -> str | None:
    source = EnvCredentialBroker().var_source(name)
    if source == "env":
        return f"  {name} is also exported in this shell, which takes priority over the keychain."
    if source == ".env":
        return f"  {name} is also set in .env, which takes priority over the keychain."
    return None


def _offer_probe(slug: str) -> None:
    answer = input("Test the key now? This makes one small request. [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        return
    report = check_liveness(make_adapter(slug), broker=EnvCredentialBroker())
    if report.status is LivenessStatus.LIVE:
        latency = f" ({report.latency_ms:.0f} ms)" if report.latency_ms is not None else ""
        print(f"  ✓ {slug} is working{latency}")
    elif report.status is LivenessStatus.CONFIGURED_UNVERIFIED:
        print(f"  - {slug} has no free probe, so the key was not tested. Try a parse.")
    else:
        print(f"  ✗ {slug}: {report.status.value}. {report.detail}")


def cmd_keys_set(args) -> int:
    """`keys set SLUG [VAR ...] [--stdin]`."""
    selected = _select(args.slug, args.vars, "set")
    if isinstance(selected, int):
        return selected
    desc, pairs = selected
    names = [name for _, name in pairs]
    interactive = not args.stdin
    if interactive and not sys.stdin.isatty():
        _err(
            "keys set asks with hidden input, which needs a terminal. Pipe the value in "
            f"with --stdin instead: openreading keys set {args.slug} --stdin"
        )
        return 2
    label = _keychain_label()
    if isinstance(label, int):
        return label

    values: list[str] | None = None
    if not interactive:
        read = _read_stdin_values(names)
        if isinstance(read, int):
            return read
        values = read
    else:
        print(desc.id)
        if desc.signup_url:
            print(f"  Get a key: {desc.signup_url}")

    saved: list[str] = []
    ask: Callable[[str], str] = getpass.getpass
    try:
        for index, name in enumerate(names):
            if values is not None:
                value = values[index]
            else:
                try:
                    value = ask(f"{name} (hidden, Enter to skip): ").strip()
                except EOFError:
                    value = ""
            if not value:
                print(f"  - Skipped {name} (no value given)")
                continue
            keychain.set(name, value)
            saved.append(name)
            print(f"  ✓ Saved {name} to the {label}")
            note = _shadow_note(name)
            if note:
                print(note)
    except KeyboardInterrupt:
        kept = f"Saved before the stop: {', '.join(saved)}. " if saved else ""
        print(f"\n[keys] Stopped. {kept}Nothing else was changed.", file=sys.stderr)
        return 130
    except keychain.KeychainUnavailable as e:
        _err(str(e))
        return 2
    if not saved:
        print("Nothing was saved.")
        return 0
    if interactive:
        try:
            _offer_probe(desc.id)
        except (KeyboardInterrupt, EOFError):
            print()
    return 0


# --- keys list -----------------------------------------------------------------------------------


def cmd_keys_list(args) -> int:
    """`keys list [--json]`: BACKEND, VAR, SOURCE for every declared secret. Never a value."""
    broker = EnvCredentialBroker()
    rows = [
        {"backend": slug, "var": var, "source": source}
        for slug, desc in _descriptors().items()
        for var, source in broker.secret_sources(desc)
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    wb = max([len("BACKEND"), *(len(r["backend"]) for r in rows)])
    wv = max([len("VAR"), *(len(r["var"]) for r in rows)])
    print(f"{'BACKEND':<{wb}}  {'VAR':<{wv}}  SOURCE")
    for r in rows:
        print(f"{r['backend']:<{wb}}  {r['var']:<{wv}}  {r['source']}")
    return 0


# --- keys unset ----------------------------------------------------------------------------------


def cmd_keys_unset(args) -> int:
    """`keys unset SLUG [VAR ...]`: remove every alias of each selected secret."""
    selected = _select(args.slug, args.vars, "unset")
    if isinstance(selected, int):
        return selected
    desc, pairs = selected
    label = _keychain_label()
    if isinstance(label, int):
        return label
    names = [name for _, name in pairs] if args.vars else _secret_names(desc)
    descs = _descriptors()
    removed = 0
    try:
        for name in dict.fromkeys(names):
            if not keychain.delete(name):
                continue
            removed += 1
            others = _sharers(name, desc.id, descs)
            shared = f" (also used by {', '.join(others)})" if others else ""
            print(f"  ✓ Removed {name} from the {label}{shared}")
    except keychain.KeychainUnavailable as e:
        _err(str(e))
        return 2
    if not removed:
        print(f"  - Nothing saved for {desc.id} in the {label}.")
    return 0


# --- keys import ---------------------------------------------------------------------------------


def cmd_keys_import(args) -> int:
    """`keys import PATH`: copy declared secrets from a dotenv file, then name lines to delete."""
    try:
        entries = dotenv_entries(args.path)
    except OSError as e:
        _err(f"cannot read {args.path}: {e.strerror or e}")
        return 3
    declared = {name for d in _descriptors().values() for name in _secret_names(d)}
    first: dict[str, str] = {}
    lines: list[int] = []
    left: list[str] = []
    for number, name, value in entries:
        if name not in declared:
            left.append(name)
        elif value:
            first.setdefault(name, value)
            lines.append(number)
    if not first:
        print(f"No declared secret found in {args.path}. Nothing was moved.")
        return 0
    label = _keychain_label()
    if isinstance(label, int):
        return label
    try:
        for name, value in first.items():
            keychain.set(name, value)
            print(f"  ✓ Saved {name} to the {label}")
    except keychain.KeychainUnavailable as e:
        _err(str(e))
        return 2
    if left:
        print(f"  Left in place, not a backend secret: {', '.join(dict.fromkeys(left))}")
    which = "line" if len(lines) == 1 else "lines"
    print(
        f"Delete {which} {', '.join(map(str, lines))} from {args.path}, so each key lives in one "
        "place. Until then the file's value wins whenever you run from its folder."
    )
    return 0
