"""`openreading setup`: a guided wizard that gets a hosted backend working without editing a file.

You pick backends from a list, paste each key into a hidden prompt, and see a ✓ or ✗ after every
step. The wizard saves keys to your OS keychain, offers one liveness probe per backend, and offers
to add what worked to `policy.backends` in `openreading.yaml`. A second path adds an LLM provider,
which is any endpoint that speaks the OpenAI chat API, as a named backend. The user-facing chapter
is the `setup` section of the `openreading.cli` docstring.

A thin wizard over the other commands
-------------------------------------
`setup` adds no capability. Readiness comes from `openreading.readiness.backend_readiness` and
`EnvCredentialBroker.secret_sources`, the same calls `openreading backends` and `keys list` make.
A key is saved through `openreading.cli.keys.save_secret`, the `keys set` path. A probe is
`openreading.cli.keys.run_probe`, the check `backends --check` runs. A wizard with its own logic
drifts from the commands a script uses, and then the two report different readiness for one
backend. That is the defect `openreading.readiness.missing_reason` exists to prevent.

The prompts sit behind the `Prompter` protocol (select, checkbox, password, confirm, text). The
questionary implementation is the real one, and the tests drive the flow with a scripted fake.
`questionary` and `rich` come from the `[setup]` extra and are imported only when `setup` runs.

The rules the flow follows
--------------------------
- Enter always takes the safe default: no backend picked, keep a saved key, and no to every
  yes/no question.
- The liveness probe and every write to `openreading.yaml` ask first, because one is a network
  call and the other changes a committed file.
- Every step ends with a ✓ or ✗ line. An ✗ after a probe names the variable to check and offers
  to enter the key again, keep it, or remove it. A skipped variable prints a `-` line, because
  skipping is a choice and not a failure.
- Ctrl+C anywhere exits 130 and names the keys already saved, then says nothing else changed.
- Colour is a hint only. The marks and their words carry the meaning, so `NO_COLOR` loses
  nothing.
- A preset for an LLM provider is a starting `base_url` and key variable name you confirm. It
  carries no price, no model list and no capability claim, because core cannot keep one true.
  A stale URL fails the probe with an ✗ rather than a quieter success.

Editing openreading.yaml
------------------------
The file is committed and often commented, so the wizard edits text rather than re-dumping a
parsed mapping, which would drop every comment. `with_policy_backends` replaces or inserts one
`backends:` line under `policy:`, and `with_provider` appends one entry under `providers:`. Each
then parses the result with `openreading.config.parse` and compares it with the original mapping
plus the one intended change. Any other difference raises `EditRefused`, the file stays as it
was, and the wizard prints the block to add by hand. A flow-style `policy: {...}` is one shape
that is refused rather than rewritten. The file is the one `--config` names, else the one
discovery finds, else `./openreading.yaml`, which is created with `version: 1` when missing.

Environment variables this module reads
---------------------------------------
`NO_COLOR`: read by `rich`. Set to any value, the output carries no colour and keeps every mark
and word.

Exit codes: 0 done, 2 no terminal, no `[setup]` extra, or no keychain, 3 an `openreading.yaml`
that does not load, 130 stopped with Ctrl+C.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from openreading import config, keychain
from openreading.adapters.registry import install_profiles, known_backends, make_adapter
from openreading.cli import keys as keys_cli
from openreading.credentials import EnvCredentialBroker
from openreading.readiness import backend_readiness, primary_secret_env
from openreading.types.descriptor import AdapterDescriptor
from openreading.types.liveness import LivenessStatus

# The checkbox value for the provider step. The colon keeps it out of the backend-id namespace.
PROVIDER_CHOICE = ":add-llm-provider"

# Tests set this to True so `rich` emits colour into a captured stream, which proves `NO_COLOR`.
_FORCE_TERMINAL: bool | None = None

EPILOG = """\
Examples:
  openreading setup                  # pick backends, save keys, test them
  openreading setup --provider       # add an OpenAI-compatible endpoint
  openreading setup --config ~/proj/openreading.yaml

Enter always means the safe choice: skip, keep, or no. Keys go to your OS
keychain and never to a file. The probe and every file edit ask first.

Then:
  openreading backends               # CONFIGURED reads yes
  openreading parse FILE --backend SLUG

Exits: 0 done. 2 no terminal, no [setup] extra, or no keychain. 3 an
openreading.yaml that does not load. 130 stopped with Ctrl+C.

More: openreading help setup"""


def add_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """Register `setup` on the top-level subparser."""
    p = sub.add_parser(
        "setup",
        parents=[common],
        help="a guided setup: pick backends, save their keys, test them",
        description="Get a hosted backend working without editing a file: pick backends, "
        "save each key to your OS keychain, test it, and add it to openreading.yaml.",
    )
    p.add_argument(
        "--provider",
        action="store_true",
        help="go straight to adding an LLM provider (any OpenAI-compatible endpoint)",
    )
    p.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="the openreading.yaml to edit (default: the one other commands read here, "
        "else ./openreading.yaml)",
    )
    p.set_defaults(func=cmd_setup)


# --- prompts and output ---------------------------------------------------------------------------


class Prompter(Protocol):
    """The five questions the wizard asks. Each raises `KeyboardInterrupt` on Ctrl+C. Choices are
    `(value, label)` pairs, and an answer is a value."""

    def select(self, message: str, choices: Sequence[tuple[str, str]], default: str) -> str: ...

    def checkbox(self, message: str, choices: Sequence[tuple[str, str]]) -> list[str]: ...

    def password(self, message: str) -> str: ...

    def confirm(self, message: str, default: bool = False) -> bool: ...

    def text(self, message: str, default: str = "") -> str: ...


class QuestionaryPrompter:
    """`Prompter` over `questionary`. `unsafe_ask` lets Ctrl+C raise instead of returning None, so
    the wizard can name what it already saved."""

    def __init__(self) -> None:
        import questionary

        self._q = questionary

    def _choices(self, choices: Sequence[tuple[str, str]]) -> list[Any]:
        return [self._q.Choice(title=label, value=value) for value, label in choices]

    def select(self, message: str, choices: Sequence[tuple[str, str]], default: str) -> str:
        answer = self._q.select(
            message,
            choices=self._choices(choices),
            default=default,
            use_shortcuts=len(choices) <= 36,  # a number key picks a row, as D6a asks
        ).unsafe_ask()
        return default if answer is None else answer

    def checkbox(self, message: str, choices: Sequence[tuple[str, str]]) -> list[str]:
        answer = self._q.checkbox(
            message,
            choices=self._choices(choices),
            instruction="(space to select, enter to confirm)",
        ).unsafe_ask()
        return list(answer or [])

    def password(self, message: str) -> str:
        return self._q.password(message).unsafe_ask() or ""

    def confirm(self, message: str, default: bool = False) -> bool:
        answer = self._q.confirm(message, default=default).unsafe_ask()
        return default if answer is None else bool(answer)

    def text(self, message: str, default: str = "") -> str:
        answer = self._q.text(message, default=default).unsafe_ask()
        return default if answer is None else answer


def _make_prompter() -> Prompter:
    """The prompter `cmd_setup` uses. Tests replace this function with a scripted fake."""
    return QuestionaryPrompter()


class _Out:
    """Lines on stdout through `rich`. A leading ✓ or ✗ is coloured, and the words stay."""

    _MARKS = {"✓": "green", "✗": "red"}

    def __init__(self) -> None:
        from rich.console import Console

        self._console = Console(
            highlight=False,
            soft_wrap=True,
            emoji=False,
            markup=False,
            force_terminal=_FORCE_TERMINAL,
        )

    def line(self, text: str = "", style: str | None = None) -> None:
        from rich.text import Text

        body = text.lstrip(" ")
        lead = text[: len(text) - len(body)]
        mark = body[:1]
        if mark in self._MARKS:
            out = Text.assemble(lead, (mark, self._MARKS[mark]), body[1:])
        else:
            out = Text(text, style=style or "")
        self._console.print(out)

    def heading(self, text: str) -> None:
        self.line(text, style="bold")

    def ok(self, text: str) -> None:
        self.line(f"  ✓ {text}")

    def fail(self, text: str) -> None:
        self.line(f"  ✗ {text}")

    def note(self, text: str) -> None:
        self.line(f"  {text}")


# --- the openreading.yaml edit --------------------------------------------------------------------


class EditRefused(ValueError):
    """The file could not take the change without another difference. Nothing was written."""


_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")
_PLAIN_SCALAR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$")


def _strip_comment(rest: str) -> str:
    return re.sub(r"(^|\s)#.*$", "", rest).strip()


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


def _block(lines: list[str], key: str) -> tuple[int, int] | None:
    """The top-level `key:` line and the index just past its last indented line. A flush-left
    line, comment included, ends the block, so an insertion lands above the next section."""
    start = next(
        (i for i, ln in enumerate(lines) if (m := _TOP_KEY.match(ln)) and m.group(1) == key), None
    )
    if start is None:
        return None
    last = start
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln[0].isspace():
            break
        if ln.strip():
            last = i
    return start, last + 1


def _content(lines: list[str], start: int, end: int) -> list[int]:
    return [
        i for i in range(start, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")
    ]


def _scalar(value: str) -> str:
    """A YAML scalar for `value`: plain when that reads back as the same string, else quoted."""
    import yaml

    if _PLAIN_SCALAR.match(value) and yaml.safe_load(value) == value:
        return value
    return json.dumps(value)


def _join(lines: list[str]) -> str:
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def _checked(new_text: str, expected: dict) -> str:
    try:
        parsed = config.parse(new_text)
    except config.ConfigError as e:
        raise EditRefused(str(e)) from None
    if parsed != expected:
        raise EditRefused("the edit would change more than the one block")
    return new_text


def with_policy_backends(text: str, backends: Sequence[str]) -> str:
    """`text` with `policy.backends` set to `backends`, every other line kept. Raises
    `EditRefused` when that cannot be done exactly, and `config.ConfigError` when `text` itself
    does not load."""
    old = config.parse(text)
    expected = copy.deepcopy(old)
    expected["policy"] = {**(old.get("policy") or {}), "backends": list(backends)}
    flow = f"backends: [{', '.join(backends)}]"
    lines = text.splitlines()
    found = _block(lines, "policy")
    if found is None:
        return _checked(_join([*lines, "policy:", f"  {flow}"]), expected)
    start, end = found
    if _strip_comment(lines[start].split(":", 1)[1]):
        raise EditRefused("policy is written on one line")
    children = _content(lines, start + 1, end)
    indent = _indent(lines[children[0]]) if children else "  "
    key = next(
        (
            i
            for i in children
            if lines[i].startswith(indent + "backends")
            and _indent(lines[i]) == indent
            and _TOP_KEY.match(lines[i].lstrip())
        ),
        None,
    )
    if key is None:
        lines.insert(start + 1, indent + flow)
        return _checked(_join(lines), expected)
    last = key
    if not _strip_comment(lines[key].split(":", 1)[1]):
        # A block sequence: deeper lines, or `- item` lines at the key's own indent.
        for j in range(key + 1, end):
            stripped = lines[j].strip()
            if not stripped or stripped.startswith("#"):
                continue
            depth = len(_indent(lines[j]))
            if depth > len(indent) or (depth == len(indent) and stripped.startswith("-")):
                last = j
                continue
            break
    lines[key : last + 1] = [indent + flow]
    return _checked(_join(lines), expected)


def with_provider(text: str, name: str, profile: dict[str, str]) -> str:
    """`text` with one more `providers:` entry, every other line kept. Raises `EditRefused` when
    that cannot be done exactly."""
    old = config.parse(text)
    expected = copy.deepcopy(old)
    expected["providers"] = {**(old.get("providers") or {}), name: dict(profile)}
    lines = text.splitlines()
    found = _block(lines, "providers")
    if found is None:
        entry = [f"  {name}:", *(f"    {k}: {_scalar(v)}" for k, v in profile.items())]
        return _checked(_join([*lines, "providers:", *entry]), expected)
    start, end = found
    if _strip_comment(lines[start].split(":", 1)[1]):
        raise EditRefused("providers is written on one line")
    children = _content(lines, start + 1, end)
    child = _indent(lines[children[0]]) if children else "  "
    deeper = next(
        (_indent(lines[i]) for i in children if len(_indent(lines[i])) > len(child)), child * 2
    )
    entry = [f"{child}{name}:", *(f"{deeper}{k}: {_scalar(v)}" for k, v in profile.items())]
    lines[end:end] = entry
    return _checked(_join(lines), expected)


def _write(path: Path, text: str) -> None:
    """Replace the file in one step, so an interrupted write never leaves half a config."""
    tmp = path.with_name(path.name + ".openreading-setup.tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


# --- the flow -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """A starting point for a provider profile: a URL and a key variable name, nothing more."""

    value: str
    title: str
    base_url: str
    key_env: str


PRESETS: tuple[Preset, ...] = (
    Preset("openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    Preset("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    Preset("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    Preset("ollama", "Ollama, on this machine", "http://localhost:11434/v1", ""),
    Preset("lm-studio", "LM Studio, on this machine", "http://localhost:1234/v1", ""),
    Preset("other", "other", "", ""),
)


@dataclass
class _Row:
    slug: str
    outcome: str
    ok: bool | None  # None: removed or skipped, so it is neither a success nor a failure


class _Wizard:
    def __init__(self, prompter: Prompter, out: _Out, label: str, target: Path, shown: str):
        self.ask = prompter
        self.out = out
        self.label = label
        self.target = target
        self.shown = shown
        self.saved: list[str] = []
        self.written: list[str] = []
        self.rows: list[_Row] = []

    # -- what is here

    def _ready_without_key(self, broker: EnvCredentialBroker) -> list[str]:
        ready = []
        for slug, factory in known_backends().items():
            adapter = factory()
            if keys_cli._secret_fields(adapter.descriptor):
                continue
            if backend_readiness(adapter, broker=broker).ready:
                ready.append(slug)
        return ready

    @staticmethod
    def _status(broker: EnvCredentialBroker, desc: AdapterDescriptor) -> str:
        sources = [src for _, src in broker.secret_sources(desc)]
        if "keychain" in sources:
            return "set up, from keychain"
        if "env" in sources:
            return "set, from your shell"
        if ".env" in sources:
            return "set, from .env"
        primary = primary_secret_env(desc) or keys_cli._secret_names(desc)[0]
        return f"needs {primary}"

    # -- one backend

    def set_up(self, slug: str, index: int, total: int) -> None:
        desc = make_adapter(slug).descriptor
        self.out.line()
        self.out.heading(slug if total == 1 else f"{slug} ({index} of {total})")
        fields = keys_cli._secret_fields(desc)
        broker = EnvCredentialBroker()
        in_keychain = [f.env[0] for f in fields if broker.var_source(f.env[0]) == "keychain"]
        ask_keys = True
        if in_keychain:
            action = self.ask.select(
                f"{slug} is set up, from keychain. What do you want to do?",
                [("keep", "Keep it"), ("replace", "Replace it"), ("remove", "Remove it")],
                "keep",
            )
            if action == "remove":
                self._remove(desc)
                return
            if action == "keep":
                for name in in_keychain:
                    self.out.ok(f"Kept {name} in the {self.label}")
                ask_keys = False
        while True:
            if ask_keys and fields:
                if desc.signup_url:
                    self.out.note(f"Get a key: {desc.signup_url}")
                saved = self._ask_keys(desc)
                if saved is None:
                    return
                if not saved:
                    self.rows.append(_Row(slug, "skipped", None))
                    return
            ok, outcome = self._offer_probe(desc, keyed=bool(fields))
            if ok or not fields:
                self.rows.append(_Row(slug, outcome, ok))
                return
            choice = self.ask.select(
                "What now?",
                [
                    ("retry", "Enter the key again"),
                    ("keep", "Keep it and continue"),
                    ("remove", "Remove it"),
                ],
                "keep",
            )
            if choice == "retry":
                ask_keys = True
                continue
            if choice == "remove":
                self._remove(desc)
            else:
                self.rows.append(_Row(slug, outcome, False))
            return

    def _ask_keys(self, desc: AdapterDescriptor) -> int | None:
        """Ask for each secret, save what was typed. The count saved, or None when the keychain
        refused a write (already reported)."""
        count = 0
        for f in keys_cli._secret_fields(desc):
            name = f.env[0]
            value = (self.ask.password(f"{name}  (hidden, Enter to skip)") or "").strip()
            if not value:
                self.out.note(f"- Skipped {name}, nothing saved")
                continue
            try:
                lines = keys_cli.save_secret(name, value, self.label)
            except keychain.KeychainUnavailable as e:
                self.out.fail(f"Could not save {name}. {e}")
                self.rows.append(_Row(desc.id, "not saved", False))
                return None
            if name not in self.saved:
                self.saved.append(name)
            for ln in lines:
                self.out.line(ln)
            count += 1
        return count

    def _offer_probe(self, desc: AdapterDescriptor, *, keyed: bool) -> tuple[bool, str]:
        slug = desc.id
        what = "the key" if keyed else "it"
        if not self.ask.confirm(f"Test {what} now? This makes one small request.", default=False):
            return True, "not tested"
        report = keys_cli.run_probe(slug)
        detail = (report.detail or "").strip()
        if report.status is LivenessStatus.LIVE:
            latency = f" ({report.latency_ms:.0f} ms)" if report.latency_ms is not None else ""
            self.out.ok(f"{slug} is working{latency}")
            return True, "working"
        if report.status is LivenessStatus.CONFIGURED_UNVERIFIED:
            self.out.ok(f"{slug} has no free test call, so the key was not tested. Try a parse.")
            return True, "not tested"
        if report.status is LivenessStatus.UNAUTHORIZED:
            var = primary_secret_env(desc) or "its key"
            self.out.fail(f"{slug} rejected the key. Check {var}.")
            return False, "key rejected"
        self.out.fail(f"{slug} did not answer ({report.status.value}). {detail}".rstrip())
        return False, report.status.value.replace("_", " ")

    def _remove(self, desc: AdapterDescriptor) -> None:
        removed = False
        for name in dict.fromkeys(keys_cli._secret_names(desc)):
            if keychain.delete(name):
                removed = True
                self.out.ok(f"Removed {name} from the {self.label}")
                if name in self.saved:
                    self.saved.remove(name)
        if not removed:
            self.out.ok(f"Nothing saved for {desc.id} in the {self.label}")
        self.rows.append(_Row(desc.id, "removed", None))

    # -- the provider step

    def add_provider(self) -> str | None:
        self.out.line()
        self.out.heading("Add an LLM provider")
        self.out.note("Any endpoint that speaks the OpenAI chat API works.")
        self.out.note("A starting point fills in a URL and a key variable for you to check.")
        value = self.ask.select(
            "Starting point?", [(p.value, p.title) for p in PRESETS], PRESETS[0].value
        )
        preset = next(p for p in PRESETS if p.value == value)
        base_url = self.ask.text("Base URL", default=preset.base_url).strip()
        if not base_url:
            self.out.fail("No base URL given. No provider was added.")
            return None
        key_env = self.ask.text(
            "Key variable (blank when the endpoint needs no key)", default=preset.key_env
        ).strip()
        model = self.ask.text("Model id, as the provider names it").strip()
        if not model:
            self.out.fail("No model given. No provider was added.")
            return None
        profile = {"kind": "openai-compatible", "base_url": base_url, "model": model}
        if key_env:
            profile["key_env"] = key_env
        try:
            config.validate_providers({"candidate": profile})
        except config.ConfigError as e:
            self.out.fail(f"{_why(e)}. No provider was added.")
            return None
        default_name = preset.value if preset.value != "other" else "my-llm"
        while True:
            name = self.ask.text("Backend name for this provider", default=default_name).strip()
            if not name:
                self.out.fail("No name given. No provider was added.")
                return None
            try:
                config.validate_providers({name: profile})
            except config.ConfigError as e:
                self.out.fail(_why(e))
                continue
            if name in known_backends():
                self.out.fail(f"{name} is already a backend here. Choose another name.")
                continue
            break
        block = [
            "providers:",
            f"  {name}:",
            *(f"    {k}: {_scalar(v)}" for k, v in profile.items()),
        ]
        self.out.line()
        for ln in block:
            self.out.note(ln)
        if not self.ask.confirm(f"Add {name} to providers in {self.shown}?", default=False):
            self.out.note(f"- Left {self.shown} as it was. No provider was added.")
            return None
        text = self.target.read_text(encoding="utf-8") if self.target.exists() else "version: 1\n"
        try:
            _write(self.target, with_provider(text, name, profile))
        except EditRefused:
            self.out.fail(f"Could not change {self.shown} without touching its other content,")
            self.out.note("so it was left as it was. Add this block by hand, then run setup again.")
            return None
        self.written.append(self.shown)
        self.out.ok(f"Written to {self.shown}.")
        loaded = config.load(self.target)
        install_profiles(loaded.providers if loaded is not None else None)
        self.set_up(name, 1, 1)
        return name

    # -- the end

    def summary(self) -> None:
        if not self.rows:
            return
        broker = EnvCredentialBroker()
        width = max(len(r.slug) for r in self.rows)
        self.out.line()
        self.out.heading("Summary")
        for r in self.rows:
            desc = make_adapter(r.slug).descriptor
            source = next((s for _, s in broker.secret_sources(desc) if s != "missing"), "-")
            if not keys_cli._secret_fields(desc):
                source = "no key"
            mark = {True: "✓", False: "✗", None: "-"}[r.ok]
            self.out.line(f"  {mark} {r.slug:<{width}}  {source:<9}  {r.outcome}")

    def offer_policy(self, fallback: list[str]) -> bool:
        good = [r.slug for r in self.rows if r.ok]
        if not good:
            return False
        loaded = config.load(self.target) if self.target.exists() else None
        policy = (loaded.policy if loaded is not None else None) or {}
        current = policy.get("backends")
        if current is None:
            # One backend that needs no key goes last, so the chain still reads a document
            # when every hosted backend in it is down or out of credit.
            extra = [s for s in fallback if s not in good][:1]
            new = [*good, *extra]
        else:
            new = [*current, *(g for g in good if g not in current)]
            if new == list(current):
                return False
        self.out.line()
        self.out.note("policy:")
        self.out.note(f"  backends: [{', '.join(new)}]")
        if not self.ask.confirm(f"Add these to policy.backends in {self.shown}?", default=False):
            self.out.note(f"- Left {self.shown} as it was")
            return False
        text = self.target.read_text(encoding="utf-8") if self.target.exists() else "version: 1\n"
        try:
            _write(self.target, with_policy_backends(text, new))
        except EditRefused:
            self.out.fail(f"Could not change {self.shown} without touching its other content,")
            self.out.note("so it was left as it was. Add the block above by hand.")
            return False
        self.written.append(self.shown)
        self.out.ok(f"Written to {self.shown}.")
        return True

    def run(self, provider_only: bool) -> int:
        self.out.heading("OpenReading setup")
        self.out.line(
            f"Keys are saved to the {self.label}. Your shell and .env still take priority."
        )
        broker = EnvCredentialBroker()
        fallback = self._ready_without_key(broker)
        if provider_only:
            self.add_provider()
        else:
            if fallback:
                self.out.line()
                self.out.heading("Ready on this machine")
                width = max(len(s) for s in fallback)
                for slug in fallback:
                    self.out.ok(f"{slug:<{width}}  no key needed")
            choices: list[tuple[str, str]] = []
            keyed = {
                s: f().descriptor
                for s, f in known_backends().items()
                if keys_cli._secret_fields(f().descriptor)
            }
            width = max([len(s) for s in keyed] or [0])
            for slug, desc in keyed.items():
                choices.append((slug, f"{slug:<{width}}  {self._status(broker, desc)}"))
            choices.append(
                (PROVIDER_CHOICE, "Add an LLM provider (OpenAI, OpenRouter, Groq, Ollama, ...)")
            )
            self.out.line()
            picked = self.ask.checkbox("Which backends do you want to set up?", choices) or []
            slugs = [p for p in picked if p != PROVIDER_CHOICE]
            for index, slug in enumerate(slugs, 1):
                self.set_up(slug, index, len(slugs))
            if PROVIDER_CHOICE in picked:
                self.add_provider()
        self.summary()
        wrote_policy = self.offer_policy(fallback)
        good = [r.slug for r in self.rows if r.ok]
        self.out.line()
        if wrote_policy:
            self.out.line("Next: openreading parse FILE")
        elif good:
            self.out.line(f"Next: openreading parse FILE --backend {good[0]}")
        else:
            self.out.line("Next: openreading backends   # what runs here, and what each one needs")
        return 0


def _why(exc: config.ConfigError) -> str:
    """A `ConfigError` sentence without its `<providers>: invalid config at '...':` locator,
    which names a node of a file the reader has not written yet."""
    text = str(exc)
    return re.sub(r"^[^:]*: invalid config at '[^']*': ", "", text).rstrip(".")


def _err(message: str) -> None:
    print(f"[setup] {message}", file=sys.stderr)


def _target(explicit: str | None) -> tuple[Path, str]:
    """The file to edit and how to name it on screen: `--config`, else the file discovery finds,
    else `./openreading.yaml`. Raises `config.ConfigError` for an `OPENREADING_CONFIG` that
    points at nothing."""
    path = Path(explicit) if explicit else (config.discover() or Path("openreading.yaml"))
    try:
        shown = f"./{path.resolve().relative_to(Path.cwd().resolve())}"
    except ValueError:
        shown = str(path)
    return path, shown


def cmd_setup(args) -> int:
    """`setup [--provider] [--config PATH]`."""
    try:
        import questionary  # noqa: F401
        import rich  # noqa: F401
    except ImportError:
        _err(
            "setup needs the [setup] extra. Install it with pip install 'openreading[setup]', "
            "or put each key in an environment variable or .env instead."
        )
        return 2
    if not sys.stdin.isatty():
        _err(
            "setup asks questions, which needs a terminal. In a script, save a key with "
            "openreading keys set SLUG --stdin, and see what is set with openreading keys list."
        )
        return 2
    try:
        label = keychain.backend_name()
    except keychain.KeychainUnavailable as e:
        _err(str(e))
        return 2
    try:
        target, shown = _target(args.config)
        if target.exists():
            config.load(target)
    except config.ConfigError as e:
        _err(f"{e}. Fix the file, then run setup again.")
        return 3
    wizard = _Wizard(_make_prompter(), _Out(), label, target, shown)
    try:
        return wizard.run(provider_only=bool(args.provider))
    except (KeyboardInterrupt, EOFError):
        done = []
        if wizard.saved:
            done.append(f"Already saved: {', '.join(wizard.saved)}.")
        if wizard.written:
            done.append(f"Already written: {', '.join(dict.fromkeys(wizard.written))}.")
        print(file=sys.stderr)
        _err(f"Stopped. {' '.join(done)}{' ' if done else ''}Nothing else was changed.")
        return 130
