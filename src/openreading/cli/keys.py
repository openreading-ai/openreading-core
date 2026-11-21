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
  because argv shows up in `ps` and in shell history. A hidden `--value` exists only to refuse
  with that reason and exit 2, because argparse's own error echoed the key it rejected. That
  refusal names the slug only when it is a known backend. A usage error from `keys` or `setup`
  quotes no token (`openreading.cli.app._parse_args_without_echo`). `--stdin` is the scripted
  path.
- Only secrets a backend declares reach the keychain (law PS5). `keys set` takes a backend slug,
  and an optional variable must be one of that backend's secret variables. A setting such as
  `ANTHROPIC_MODEL` or a knob such as `OPENREADING_LEDGER` is refused with exit 2.
- A provider profile in the project's `openreading.yaml` is a backend like any other
  (`openreading.adapters.registry.known_backends`). Its one secret is the variable its `key_env`
  names, saved under an account bound to the profile's origin. `keys set openrouter-gpt` saves
  `OPENROUTER_API_KEY@https://openrouter.ai`, which no other endpoint reads
  (`openreading.credentials`). `keys import` moves a profile's key only when you name the
  profile. Before a profile's key is saved, the output prints `Binding VAR to <origin>`, because
  the profile may come from a file you did not write. A profile with no `key_env` has nothing to
  save and exits 2.
- `keys import` names a line it leaves in place only when its left side reads as a variable
  name, and counts the rest. A bare key line ending in `==` parses with the key on the left.
- No acceptable keychain means exit 2 with the sentence `openreading.keychain` raises, and
  nothing is written anywhere else (law PS6).
- A probe is a network call, so `keys set` offers one only at an interactive terminal, and only
  after asking.

`--stdin` reads one line per secret the command selects, in descriptor order. For example,
`aws-textract` reads `AWS_ACCESS_KEY_ID`, then `AWS_SECRET_ACCESS_KEY`, then the optional
`AWS_SESSION_TOKEN`. A blank or absent line skips that variable. More lines than secrets is
refused before anything is saved, because a shifted line would save a key under the wrong name.
Name one variable after the slug to save only that one.

Exit codes: 0 done, 2 usage (unknown slug, undeclared variable, no terminal without `--stdin`,
no keychain), 3 an unreadable `keys import` file, 130 stopped with Ctrl+C.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from collections.abc import Callable, Iterable

from openreading import keychain
from openreading.adapters.registry import known_backends, make_adapter
from openreading.credentials import (
    EnvCredentialBroker,
    bound_origin,
    dotenv_entries,
    env_ignored_reason,
    keychain_account,
)
from openreading.liveness import check_liveness
from openreading.types.descriptor import AdapterDescriptor, CredentialField
from openreading.types.liveness import LivenessReport, LivenessStatus

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
  openreading keys unset aws-textract AWS_SESSION_TOKEN   # only that one

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
  openreading keys import .env openrouter-gpt   # one backend, a profile too

The file is never edited. The output names the lines to delete, because a
.env line still beats the keychain whenever you run from that folder. Settings
such as ANTHROPIC_MODEL stay where they are. A provider profile's key moves
only when you name the profile, which binds it to that profile's endpoint.

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
    # Hidden and refused (law PS4). Without it a habitual `--value sk-...` reached argparse's
    # generic "unrecognized arguments" error, which echoed the key back to the terminal.
    set_p.add_argument(
        "--value", dest="value_flag", nargs="?", const="", default=None, help=argparse.SUPPRESS
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
    import_p.add_argument(
        "slug",
        nargs="?",
        metavar="SLUG",
        help="move only this backend's secrets. A provider profile's key moves only this way",
    )
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


def _accounts(desc: AdapterDescriptor) -> list[str]:
    return [keychain_account(desc, name) for name in _secret_names(desc)]


def _sharers(account: str, slug: str, descs: dict[str, AdapterDescriptor]) -> list[str]:
    return [s for s, d in descs.items() if s != slug and account in _accounts(d)]


def _for_origin(desc: AdapterDescriptor) -> str:
    """` for <origin>` when `desc` is a provider profile, whose key is bound to its endpoint."""
    origin = bound_origin(desc)
    return f" for {origin}" if origin else ""


def _print_binding(desc: AdapterDescriptor, names: Iterable[str]) -> None:
    """Name the endpoint a provider profile's key is bound to, before the key is saved. The
    profile comes from a file you may not have written, so the origin is shown first."""
    origin = bound_origin(desc)
    if origin is not None:
        for name in dict.fromkeys(names):
            print(f"  Binding {name} to {origin}")


# A dotenv line's left side is printed only when it reads as a variable name. A bare key line,
# such as a base64 value ending in `==`, parses with the key itself on the left.
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")


def _select(
    slug: str, wanted: list[str], verb: str
) -> tuple[AdapterDescriptor, list[tuple[CredentialField, str]]] | int:
    """Resolve SLUG and optional VARs to `(field, variable)` pairs, or print why not and return
    the exit code. With no VARs, every secret field is selected under its primary variable."""
    descs = _descriptors()
    if slug not in descs:
        # Only backends with a secret: a keyless one is never a valid answer here. The argument
        # itself is never printed, because a key pasted in the slug's place would be echoed.
        keyed = [s for s, d in descs.items() if _secret_fields(d)]
        print(
            f"[keys {verb}] unknown backend. The first argument names no backend here "
            f"(known: {', '.join(sorted(keyed))})",
            file=sys.stderr,
        )
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
            # Never echo `name`: an argument that is not a declared variable may be the key itself.
            _err(
                f"An argument is not a secret variable {slug} declares. Name one of: {declared}. "
                "The key itself goes to the hidden prompt or --stdin."
            )
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


def _shadow_note(name: str, descriptor: AdapterDescriptor | None) -> str | None:
    source = EnvCredentialBroker().var_source(name, descriptor)
    if source == "env":
        return f"  {name} is also exported in this shell, which takes priority over the keychain."
    if source == ".env":
        return f"  {name} is also set in .env, which takes priority over the keychain."
    return None


def save_secret(
    name: str, value: str, label: str, descriptor: AdapterDescriptor | None = None
) -> list[str]:
    """Save one secret and return the lines to print: the confirmation, then a note when the
    environment or `.env` will shadow it. With a provider profile's `descriptor`, the key goes
    under the account bound to the profile's origin (`credentials.keychain_account`).
    `openreading setup` calls this too, so both commands save and report a key the same way.
    Raises `keychain.KeychainUnavailable`."""
    account = keychain_account(descriptor, name) if descriptor is not None else name
    keychain.set(account, value)
    where = _for_origin(descriptor) if descriptor is not None else ""
    lines = [f"  ✓ Saved {name}{where} to the {label}"]
    note = _shadow_note(name, descriptor)
    if note:
        lines.append(note)
    return lines


def run_probe(slug: str) -> LivenessReport:
    """One liveness check through a fresh broker, so a key saved a moment ago is the key tested.
    The probe `openreading backends --check` runs, shared with `openreading setup`."""
    return check_liveness(make_adapter(slug), broker=EnvCredentialBroker())


def _offer_probe(slug: str) -> None:
    answer = input("Test the key now? This makes one small request. [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        return
    report = run_probe(slug)
    if report.status is LivenessStatus.LIVE:
        latency = f" ({report.latency_ms:.0f} ms)" if report.latency_ms is not None else ""
        print(f"  ✓ {slug} is working{latency}")
    elif report.status is LivenessStatus.CONFIGURED_UNVERIFIED:
        print(f"  - {slug} has no free probe, so the key was not tested. Try a parse.")
    else:
        print(f"  ✗ {slug}: {report.status.value}. {report.detail}")


def cmd_keys_set(args) -> int:
    """`keys set SLUG [VAR ...] [--stdin]`."""
    if args.value_flag is not None:
        # Never echo the value: it is the key this refusal exists to keep out of sight. The slug
        # is quoted only once it names a backend, because a key pasted in its place would be too.
        slug = args.slug if args.slug in _descriptors() else "SLUG"
        _err(
            "A key never goes on the command line, because argv is visible in `ps` and in "
            f"shell history. Run openreading keys set {slug} to type it at a hidden "
            "prompt, or pipe it in with --stdin."
        )
        return 2
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
    _print_binding(desc, names)

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
            lines = save_secret(name, value, label, desc)
            saved.append(name)
            print("\n".join(lines))
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
    """`keys list [--json]`: BACKEND, VAR, SOURCE for every declared secret. Never a value.

    SOURCE `env-ignored` marks a provider profile's key that is set in the environment but not
    read, because `OPENREADING_PROVIDER_ORIGINS` does not list the profile's origin. One line on
    stderr per such profile names the setting, so the rows a script parses stay rows."""
    broker = EnvCredentialBroker()
    rows: list[dict[str, str]] = []
    hints: list[str] = []
    for slug, desc in _descriptors().items():
        ignored = broker.ignored_env(desc)
        for var, source in broker.secret_sources(desc):
            if source == "missing" and var in ignored:
                source = "env-ignored"
            rows.append({"backend": slug, "var": var, "source": source})
        if ignored:
            hints.append(
                f"{slug}: {', '.join(ignored)} is set in the environment but not sent to "
                f"{bound_origin(desc)}, {env_ignored_reason()} Save the key with openreading "
                f"keys set {slug}, or export the origin in OPENREADING_PROVIDER_ORIGINS."
            )
    for hint in hints:
        _err(hint)
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
            account = keychain_account(desc, name)
            if not keychain.delete(account):
                continue
            removed += 1
            others = _sharers(account, desc.id, descs)
            shared = f" (also used by {', '.join(others)})" if others else ""
            print(f"  ✓ Removed {name}{_for_origin(desc)} from the {label}{shared}")
    except keychain.KeychainUnavailable as e:
        _err(str(e))
        return 2
    if not removed:
        print(f"  - Nothing saved for {desc.id} in the {label}.")
    return 0


# --- keys import ---------------------------------------------------------------------------------


def cmd_keys_import(args) -> int:
    """`keys import PATH [SLUG]`: copy declared secrets from a dotenv file, then name the lines
    to delete.

    Without SLUG, only built-in backends' secrets move, each under its plain account. A provider
    profile's key moves only when SLUG names that profile, because its account is bound to the
    profile's origin (`credentials.keychain_account`). Two profiles may share one variable for
    two hosts, so the variable alone cannot say which endpoint the key belongs to."""
    descs = _descriptors()
    if args.slug is not None:
        selected = _select(args.slug, [], "import")
        if isinstance(selected, int):
            return selected
        scope = {args.slug: selected[0]}
    else:
        scope = {s: d for s, d in descs.items() if bound_origin(d) is None}
    try:
        entries = dotenv_entries(args.path)
    except OSError as e:
        # The path is not echoed: a key pasted in its place would otherwise reach the terminal.
        _err(f"cannot read the file named on the command line ({e.strerror or type(e).__name__})")
        return 3
    # name -> the account it is saved under. With SLUG this is that backend's own account.
    accounts = {
        name: keychain_account(d, name) for d in scope.values() for name in _secret_names(d)
    }
    # A profile variable met without SLUG, mapped to every profile that declares it.
    profile_vars: dict[str, list[str]] = {}
    if args.slug is None:
        for s, d in descs.items():
            if bound_origin(d) is not None:
                for name in _secret_names(d):
                    profile_vars.setdefault(name, []).append(s)
    first: dict[str, str] = {}
    lines: list[int] = []
    left: list[str] = []
    held: list[str] = []
    for number, name, value in entries:
        if name in accounts:
            if value:
                first.setdefault(name, value)
                lines.append(number)
        elif name in profile_vars:
            held.append(name)
        else:
            left.append(name)
    if held:
        named = [
            f"{n} ({', '.join(f'{s} at {bound_origin(descs[s])}' for s in profile_vars[n])})"
            for n in dict.fromkeys(held)
        ]
        example = profile_vars[held[0]][0]
        print(
            f"  Not moved, a provider profile's key: {', '.join(named)}. Name the profile to "
            f"bind the key to its endpoint, such as openreading keys import PATH {example}"
        )
    if not first:
        print(f"No declared secret found in {args.path}. Nothing was moved.")
        return 0
    label = _keychain_label()
    if isinstance(label, int):
        return label
    where = _for_origin(next(iter(scope.values()))) if args.slug is not None else ""
    if args.slug is not None:
        _print_binding(scope[args.slug], first)
    try:
        for name, value in first.items():
            keychain.set(accounts[name], value)
            print(f"  ✓ Saved {name}{where} to the {label}")
    except keychain.KeychainUnavailable as e:
        _err(str(e))
        return 2
    if left:
        named = [n for n in dict.fromkeys(left) if _ENV_NAME.fullmatch(n)]
        others = sum(1 for n in left if not _ENV_NAME.fullmatch(n))
        listing = ", ".join(named)
        if others:
            tail = f"{others} other {'line' if others == 1 else 'lines'}"
            listing = f"{listing}, and {tail}" if listing else tail
        # With SLUG, another backend's real secret is left too, so "not a backend secret" lies.
        what = (
            f"not a secret {args.slug} declares"
            if args.slug is not None
            else "not a backend secret"
        )
        print(f"  Left in place, {what}: {listing}")
    which = "line" if len(lines) == 1 else "lines"
    print(
        f"Delete {which} {', '.join(map(str, lines))} from {args.path}, so each key lives in one "
        "place. Until then the file's value wins whenever you run from its folder."
    )
    return 0
