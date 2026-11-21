"""`openreading setup`, the guided wizard (`openreading.cli.setup`).

A scripted `Prompter` drives every flow, the keychain is the in-memory fake from
`tests/conftest.py`, and a probe is either a patched `check_liveness` or respx. No test reaches a
real keychain, a terminal, or the network.
"""

from __future__ import annotations

import io
import sys
from typing import Any

import httpx
import pytest
import respx

from openreading import config, keychain
from openreading.cli import app as cli
from openreading.cli import keys as cli_keys
from openreading.cli import setup as cli_setup
from openreading.types.liveness import LivenessStatus

KEY = "sk-setup-canary-0003"
KEY2 = "sk-setup-canary-0004"
DEFAULT = object()  # answer a prompt with Enter, which takes its default


class ScriptedPrompter:
    """Answers each prompt from a script of `(kind, answer)` pairs, in order.

    An answer may be `DEFAULT`, an exception class to raise, or a callable that receives the
    prompt's message and choices and returns the answer. Every prompt is recorded."""

    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = list(script)
        self.seen: list[dict[str, Any]] = []

    def _next(self, kind: str, message: str, choices=None, default=None) -> Any:
        self.seen.append({"kind": kind, "message": message, "choices": choices, "default": default})
        assert self.script, f"unscripted {kind} prompt: {message!r}"
        want, answer = self.script.pop(0)
        assert want == kind, f"expected a {want} prompt, got {kind}: {message!r}"
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer
        if answer is DEFAULT:
            return default
        if callable(answer):
            return answer(message, choices)
        return answer

    def select(self, message, choices, default):
        return self._next("select", message, choices, default)

    def checkbox(self, message, choices):
        return self._next("checkbox", message, choices, [])

    def password(self, message):
        return self._next("password", message)

    def confirm(self, message, default=False):
        return self._next("confirm", message, None, default)

    def text(self, message, default=""):
        return self._next("text", message, None, default)


class _Report:
    def __init__(self, status: LivenessStatus, latency_ms: float | None = 412.0, detail: str = ""):
        self.status = status
        self.latency_ms = latency_ms
        self.detail = detail


def _probe_script(monkeypatch, *statuses: LivenessStatus) -> list[str]:
    seen: list[str] = []
    queue = list(statuses)

    def probe(adapter, *, broker, timeout_s=None):
        seen.append(adapter.descriptor.id)
        return _Report(queue.pop(0))

    monkeypatch.setattr(cli_keys, "check_liveness", probe)
    return seen


@pytest.fixture
def wizard(monkeypatch, fake_keychain):
    """A terminal on stdin, the fake keychain, and a way to run `setup` with a script."""
    stream = io.StringIO("")
    stream.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stream)

    def run(script, argv=(), capsys=None):
        prompter = ScriptedPrompter(script)
        monkeypatch.setattr(cli_setup, "_make_prompter", lambda: prompter)
        code = cli.main(["setup", *argv])
        out, err = capsys.readouterr() if capsys else ("", "")
        assert not prompter.script, f"script left unanswered: {prompter.script}"
        return code, out, err, prompter

    return run


def _pick(*values: str):
    return lambda _message, _choices: list(values)


def _saved(fake, name: str) -> str | None:
    return fake.store.get(("openreading", name))


# --- the happy path ------------------------------------------------------------------------------


def test_happy_path_saves_probes_and_writes_the_policy(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    probed = _probe_script(monkeypatch, LivenessStatus.LIVE)
    code, out, err, prompter = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", True),  # test the key
            ("confirm", True),  # write policy.backends
        ],
        capsys=capsys,
    )
    assert code == 0, err
    assert _saved(fake_keychain, "ANTHROPIC_API_KEY") == KEY
    assert probed == ["anthropic-claude"]
    assert "OpenReading setup" in out
    assert "Keys are saved to the" in out and "Your shell and .env still take priority" in out
    assert "Get a key: https://" in out
    assert "✓ Saved ANTHROPIC_API_KEY to the" in out
    assert "✓ anthropic-claude is working (412 ms)" in out
    assert "policy:" in out and "backends: [anthropic-claude" in out
    assert "✓ Written to ./openreading.yaml" in out
    assert "openreading parse" in out.splitlines()[-1]
    assert KEY not in out + err
    loaded = config.load(tmp_path / "openreading.yaml")
    assert loaded is not None and loaded.policy is not None
    assert loaded.policy["backends"][0] == "anthropic-claude"
    # The menu offers the provider step, and Enter on every yes/no means no.
    labels = [label for _, label in prompter.seen[0]["choices"]]
    assert any("Add an LLM provider" in label for label in labels)
    assert all(p["default"] is False for p in prompter.seen if p["kind"] == "confirm")


def test_enter_everywhere_changes_nothing(wizard, monkeypatch, capsys, fake_keychain, tmp_path):
    code, out, _, _ = wizard([("checkbox", DEFAULT)], capsys=capsys)
    assert code == 0
    assert fake_keychain.store == {}
    assert not (tmp_path / "openreading.yaml").exists()
    assert "openreading" in out.splitlines()[-1]  # still ends with the next command


def test_a_rejected_key_offers_retry_and_the_second_key_wins(
    wizard, monkeypatch, capsys, fake_keychain
):
    _probe_script(monkeypatch, LivenessStatus.UNAUTHORIZED, LivenessStatus.LIVE)
    code, out, _, prompter = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", True),
            ("select", "retry"),
            ("password", KEY2),
            ("confirm", True),
            ("confirm", False),  # leave openreading.yaml alone
        ],
        capsys=capsys,
    )
    assert code == 0
    assert "✗ anthropic-claude rejected the key. Check ANTHROPIC_API_KEY." in out
    what_now = next(p for p in prompter.seen if p["kind"] == "select")
    assert [v for v, _ in what_now["choices"]] == ["retry", "keep", "remove"]
    assert what_now["default"] == "keep"
    assert _saved(fake_keychain, "ANTHROPIC_API_KEY") == KEY2
    assert "✓ anthropic-claude is working" in out
    assert "Summary" in out


def test_a_rejected_key_can_be_removed(wizard, monkeypatch, capsys, fake_keychain):
    _probe_script(monkeypatch, LivenessStatus.UNAUTHORIZED)
    code, out, _, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", True),
            ("select", "remove"),
        ],
        capsys=capsys,
    )
    assert code == 0
    assert _saved(fake_keychain, "ANTHROPIC_API_KEY") is None
    assert "✓ Removed ANTHROPIC_API_KEY" in out


def test_a_declined_probe_is_untested_and_stays_out_of_the_policy_by_default(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    probed = _probe_script(monkeypatch)
    code, out, _, prompter = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", False),  # no probe
            ("confirm", DEFAULT),  # Enter on "add the untested ones?"
        ],
        capsys=capsys,
    )
    assert code == 0
    assert probed == []
    assert "- anthropic-claude saved, not tested" in out
    summary = out.split("Summary", 1)[1]
    row = next(ln for ln in summary.splitlines() if "anthropic-claude" in ln)
    assert row.strip().startswith("-") and "not tested" in row
    assert "✓ anthropic-claude" not in out
    ask = prompter.seen[-1]
    assert "saved, not tested" in ask["message"] and ask["default"] is False
    assert not (tmp_path / "openreading.yaml").exists()
    assert "openreading backends --check anthropic-claude" in out.splitlines()[-1]


def test_a_backend_without_a_free_probe_is_untested_not_working(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    _probe_script(monkeypatch, LivenessStatus.CONFIGURED_UNVERIFIED)
    code, out, _, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", True),  # test it, but it has no free probe
            ("confirm", True),  # add the untested one anyway
            ("confirm", True),  # write policy.backends
        ],
        capsys=capsys,
    )
    assert code == 0
    assert "- anthropic-claude has no free test call" in out
    assert "✓ anthropic-claude has no free test call" not in out
    loaded = config.load(tmp_path / "openreading.yaml")
    assert loaded is not None and loaded.policy is not None
    assert loaded.policy["backends"][0] == "anthropic-claude"


def test_ctrl_c_mid_flow_names_what_was_saved_and_exits_130(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    code, out, err, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude", "mistral-ocr")),
            ("password", KEY),
            ("confirm", False),
            ("password", KeyboardInterrupt),
        ],
        capsys=capsys,
    )
    assert code == 130
    assert "Nothing else was changed" in err
    assert "ANTHROPIC_API_KEY" in err
    assert _saved(fake_keychain, "ANTHROPIC_API_KEY") == KEY
    assert _saved(fake_keychain, "MISTRAL_API_KEY") is None
    assert not (tmp_path / "openreading.yaml").exists()


def test_a_rerun_with_everything_configured_keeps_by_default(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    original = "version: 1\npolicy:\n  backends: [anthropic-claude, pymupdf]\n"
    (tmp_path / "openreading.yaml").write_text(original)

    def pick(_message, choices):
        label = dict(choices)["anthropic-claude"]
        assert "set up, from keychain" in label
        return ["anthropic-claude"]

    code, out, _, prompter = wizard(
        [("checkbox", pick), ("select", DEFAULT), ("confirm", False)], capsys=capsys
    )
    assert code == 0
    action = next(p for p in prompter.seen if p["kind"] == "select")
    assert [v for v, _ in action["choices"]] == ["keep", "replace", "remove"]
    assert action["default"] == "keep"
    assert not any(call[0] in ("set", "delete") for call in fake_keychain.calls)
    assert "✓ Kept ANTHROPIC_API_KEY" in out
    # The policy already names it, so there is nothing to write and no question about it.
    assert (tmp_path / "openreading.yaml").read_text() == original
    assert not any("policy.backends" in p["message"] for p in prompter.seen)


def test_no_color_keeps_every_mark_and_its_words(wizard, monkeypatch, capsys, fake_keychain):
    monkeypatch.setattr(cli_setup, "_FORCE_TERMINAL", True)
    _probe_script(monkeypatch, LivenessStatus.UNAUTHORIZED, LivenessStatus.UNAUTHORIZED)
    script = [
        ("checkbox", _pick("anthropic-claude")),
        ("password", KEY),
        ("confirm", True),
        ("select", "keep"),
    ]
    monkeypatch.delenv("NO_COLOR", raising=False)
    _, coloured, _, _ = wizard(list(script), capsys=capsys)
    assert "\x1b[3" in coloured  # a colour code, so the next run proves NO_COLOR removes it

    fake_keychain.store.clear()
    keychain._reset()
    monkeypatch.setenv("NO_COLOR", "1")
    code, plain, _, _ = wizard(list(script), capsys=capsys)
    assert code == 0
    assert "\x1b[3" not in plain
    assert "✓ Saved ANTHROPIC_API_KEY to the" in plain
    assert "✗ anthropic-claude rejected the key. Check ANTHROPIC_API_KEY." in plain


# --- refusals ------------------------------------------------------------------------------------


def test_no_terminal_is_usage_and_names_the_script_commands(monkeypatch, capsys, fake_keychain):
    stream = io.StringIO("")
    stream.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stream)
    code = cli.main(["setup"])
    _, err = capsys.readouterr()
    assert code == 2
    assert "keys set" in err and "--stdin" in err and "keys list" in err


def test_without_the_setup_extra_it_names_the_install_command(monkeypatch, capsys, fake_keychain):
    monkeypatch.setitem(sys.modules, "questionary", None)
    code = cli.main(["setup"])
    _, err = capsys.readouterr()
    assert code == 2
    assert "pip install 'openreading[setup]'" in err


def test_without_a_keychain_it_says_so(monkeypatch, capsys):
    stream = io.StringIO("")
    stream.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(cli_setup, "_make_prompter", lambda: ScriptedPrompter([]))
    code = cli.main(["setup"])  # the suite's OPENREADING_KEYCHAIN=off
    _, err = capsys.readouterr()
    assert code == 2 and "OPENREADING_KEYCHAIN=off" in err


def test_a_broken_config_file_exits_3(wizard, capsys, tmp_path):
    (tmp_path / "openreading.yaml").write_text("version: [")
    code, _, err, _ = wizard([], capsys=capsys)
    assert code == 3 and "openreading.yaml" in err


# --- the openreading.yaml edit --------------------------------------------------------------------

EXISTING = """\
# Team settings. Keep this comment.
version: 1
providers:
  local-llm:
    kind: openai-compatible
    base_url: http://localhost:11434/v1   # Ollama
    model: llava

policy:
  # Tried in this order.
  backends:
    - pymupdf
strategies:
  quick: [pymupdf]
"""


def test_policy_edit_keeps_comments_and_every_other_key(tmp_path):
    path = tmp_path / "openreading.yaml"
    path.write_text(EXISTING)
    before = config.load(path)
    assert before is not None
    new = cli_setup.with_policy_backends(EXISTING, ["pymupdf", "anthropic-claude"])
    assert "# Team settings. Keep this comment." in new
    assert "# Tried in this order." in new
    assert "base_url: http://localhost:11434/v1   # Ollama" in new
    after = config.parse(new)
    expected = dict(before.raw)
    expected["policy"] = {"backends": ["pymupdf", "anthropic-claude"]}
    assert after == expected


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\n",
        "version: 1\npolicy:\n  backends: [docling]\n",
        "version: 1\npolicy:\n  backends: [docling]\n",
        "version: 1\npolicy:\n  backends:\n  - docling\n# trailing\n",
    ],
)
def test_policy_edit_handles_each_shape(text):
    want = ["docling", "anthropic-claude", "pymupdf"] if "docling" in text else ["pymupdf"]
    new = cli_setup.with_policy_backends(text, want)
    assert config.parse(new)["policy"]["backends"] == want


BLOCK_WITH_COMMENTS = """\
version: 1
policy:
  backends:
    # Hosted first.
    - reducto   # the team's contract
    # Local fallback.
    - pymupdf   # always works
    # More go here.
  # Trailing note under policy.
strategies:
  quick: [pymupdf]
"""


def test_policy_edit_appends_to_a_block_list_and_touches_nothing_else():
    new = cli_setup.with_policy_backends(
        BLOCK_WITH_COMMENTS, ["reducto", "pymupdf", "anthropic-claude", "tesseract"]
    )
    old_lines = BLOCK_WITH_COMMENTS.splitlines()
    new_lines = new.splitlines()
    at = old_lines.index("    - pymupdf   # always works") + 1
    assert new_lines == [
        *old_lines[:at],
        "    - anthropic-claude",
        "    - tesseract",
        *old_lines[at:],
    ]


def test_policy_edit_refuses_to_reorder_or_drop_a_block_list_item():
    with pytest.raises(cli_setup.EditRefused):
        cli_setup.with_policy_backends(BLOCK_WITH_COMMENTS, ["pymupdf", "reducto"])
    with pytest.raises(cli_setup.EditRefused):
        cli_setup.with_policy_backends(BLOCK_WITH_COMMENTS, ["reducto"])


def test_policy_edit_refuses_a_flow_list_that_carries_a_comment():
    text = "version: 1\npolicy:\n  backends: [docling]   # keep docling first\n"
    with pytest.raises(cli_setup.EditRefused):
        cli_setup.with_policy_backends(text, ["docling", "pymupdf"])


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\npolicy: {backends: [docling]}\n",
        # The parsed result is the only guard for these two: the line edit itself succeeds.
        "version: 1\npolicy:\n  backends: [docling,\n    tesseract]\n",
        "version: 1\npolicy:\n  backends: [docling]\npolicy:\n  backends: [tesseract]\n",
    ],
)
def test_policy_edit_refuses_a_shape_it_cannot_change_safely(text):
    with pytest.raises(cli_setup.EditRefused):
        cli_setup.with_policy_backends(text, ["pymupdf"])


def test_providers_edit_refuses_a_one_line_block():
    with pytest.raises(cli_setup.EditRefused):
        cli_setup.with_provider(
            "version: 1\nproviders: {}\n",
            "x-llm",
            {"kind": "openai-compatible", "base_url": "http://h/v1", "model": "m"},
        )


PROFILE_X = {"kind": "openai-compatible", "base_url": "http://h/v1", "model": "m"}


@pytest.mark.parametrize("quote", ['"', "'"])
def test_policy_edit_finds_a_quoted_top_level_key(quote):
    text = f"version: 1\n{quote}policy{quote}:\n  backends: [docling]\n"
    new = cli_setup.with_policy_backends(text, ["docling", "pymupdf"])
    assert config.parse(new)["policy"] == {"backends": ["docling", "pymupdf"]}
    assert sum(1 for ln in new.splitlines() if "policy" in ln) == 1


@pytest.mark.parametrize("quote", ['"', "'"])
def test_providers_edit_finds_a_quoted_top_level_key(quote):
    text = f"version: 1\n{quote}providers{quote}:\n  a-llm:\n" + "".join(
        f"    {k}: {v}\n" for k, v in PROFILE_X.items()
    )
    new = cli_setup.with_provider(text, "x-llm", PROFILE_X)
    assert set(config.parse(new)["providers"]) == {"a-llm", "x-llm"}
    assert sum(1 for ln in new.splitlines() if "providers" in ln) == 1


def test_an_edit_refuses_a_key_it_cannot_locate_instead_of_appending_a_second_block():
    # YAML reads the escape as "policy", so the parsed mapping has the key, but no line spells it.
    policy = 'version: 1\n"pol\\x69cy":\n  backends: [docling]\n'
    assert config.parse(policy)["policy"] == {"backends": ["docling"]}
    with pytest.raises(cli_setup.EditRefused):
        cli_setup.with_policy_backends(policy, ["docling", "pymupdf"])
    providers = (
        'version: 1\n"provider\\x73":\n  a-llm: {kind: openai-compatible, '
        'base_url: "http://h/v1", model: m}\n'
    )
    assert "a-llm" in config.parse(providers)["providers"]
    with pytest.raises(cli_setup.EditRefused):
        cli_setup.with_provider(providers, "x-llm", PROFILE_X)


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\r\n",
        "version: 1\r\npolicy:\r\n  backends: [docling]\r\n",
        "version: 1\r\npolicy:\r\n  backends:\r\n    - docling\r\n",
    ],
)
def test_an_edit_keeps_crlf_line_endings(text):
    for new in (
        cli_setup.with_policy_backends(text, ["docling", "pymupdf"]),
        cli_setup.with_provider(text, "x-llm", PROFILE_X),
    ):
        assert new.count("\r\n") == new.count("\n")


def test_the_wizard_keeps_a_crlf_file_crlf(wizard, monkeypatch, capsys, fake_keychain, tmp_path):
    _probe_script(monkeypatch, LivenessStatus.LIVE)
    path = tmp_path / "openreading.yaml"
    path.write_bytes(b"# team file\r\nversion: 1\r\npolicy:\r\n  backends: [pymupdf]\r\n")
    code, _, err, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", True),  # test the key
            ("confirm", True),  # write policy.backends
        ],
        capsys=capsys,
    )
    assert code == 0, err
    data = path.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") == 4
    assert b"backends: [pymupdf, anthropic-claude]" in data


def test_the_questionary_prompter_asks_with_unsafe_ask(monkeypatch):
    import questionary

    calls: list[tuple[str, str, dict]] = []

    def fake(kind, answer):
        def build(message, **kwargs):
            calls.append((kind, message, kwargs))
            return type("Q", (), {"unsafe_ask": lambda self: answer})()

        return build

    monkeypatch.setattr(questionary, "select", fake("select", None))
    monkeypatch.setattr(questionary, "checkbox", fake("checkbox", ["a"]))
    monkeypatch.setattr(questionary, "password", fake("password", None))
    monkeypatch.setattr(questionary, "confirm", fake("confirm", None))
    monkeypatch.setattr(questionary, "text", fake("text", "typed"))
    q = cli_setup.QuestionaryPrompter()
    assert q.select("Pick?", [("keep", "Keep it")], "keep") == "keep"
    assert q.checkbox("Which?", [("a", "A")]) == ["a"]
    assert q.password("KEY") == ""
    assert q.confirm("Sure?") is False
    assert q.text("Name", default="x") == "typed"
    assert calls[0][2]["use_shortcuts"] is True
    assert "space to select" in calls[1][2]["instruction"]


def test_the_wizard_writes_into_an_existing_file_without_losing_anything(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    path = tmp_path / "openreading.yaml"
    path.write_text(EXISTING)
    code, out, _, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", False),  # no probe
            ("confirm", True),  # add it although it is untested
            ("confirm", True),  # write policy.backends
        ],
        capsys=capsys,
    )
    assert code == 0
    text = path.read_text()
    assert "# Team settings. Keep this comment." in text and "quick: [pymupdf]" in text
    assert config.parse(text)["policy"]["backends"] == ["pymupdf", "anthropic-claude"]


def test_a_refused_edit_leaves_the_file_and_prints_the_block(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    path = tmp_path / "openreading.yaml"
    original = "version: 1\npolicy: {backends: [pymupdf]}\n"
    path.write_text(original)
    code, out, _, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", False),  # no probe
            ("confirm", True),  # add it although it is untested
            ("confirm", True),  # write policy.backends
        ],
        capsys=capsys,
    )
    assert code == 0
    assert path.read_text() == original
    assert "✗ Could not change ./openreading.yaml" in out
    assert "backends: [pymupdf, anthropic-claude]" in out


def test_providers_edit_appends_a_profile_and_keeps_the_rest():
    profile = {
        "kind": "openai-compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-5.1",
        "key_env": "OPENROUTER_API_KEY",
    }
    new = cli_setup.with_provider(EXISTING, "openrouter", profile)
    raw = config.parse(new)
    assert raw["providers"]["openrouter"] == profile
    assert "local-llm" in raw["providers"] and raw["strategies"] == {"quick": ["pymupdf"]}
    assert "# Ollama" in new
    fresh = cli_setup.with_provider("version: 1\n", "odd", {**profile, "model": "a: b #c"})
    assert config.parse(fresh)["providers"]["odd"]["model"] == "a: b #c"


# --- adding an LLM provider -----------------------------------------------------------------------


@respx.mock
def test_provider_preset_writes_a_valid_profile_saves_its_key_and_probes(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    models = respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    code, out, err, prompter = wizard(
        [
            ("select", "openrouter"),
            ("text", DEFAULT),  # base_url, as the preset fills it
            ("text", DEFAULT),  # key variable
            ("text", "openai/gpt-5.1"),  # model
            ("text", DEFAULT),  # profile name
            ("confirm", True),  # write the providers entry
            ("password", KEY),
            ("confirm", True),  # test it
            ("confirm", True),  # add to policy.backends
        ],
        argv=["--provider"],
        capsys=capsys,
    )
    assert code == 0, err
    loaded = config.load(tmp_path / "openreading.yaml")
    assert loaded is not None
    assert loaded.providers["openrouter"] == {
        "kind": "openai-compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-5.1",
        "key_env": "OPENROUTER_API_KEY",
    }
    assert loaded.policy == {"backends": ["openrouter", "pymupdf"]}
    assert _saved(fake_keychain, "OPENROUTER_API_KEY") == KEY
    assert models.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert "✓ openrouter is working" in out
    presets = next(p for p in prompter.seen if p["kind"] == "select")["choices"]
    assert [v for v, _ in presets] == [
        "openai",
        "openrouter",
        "groq",
        "ollama",
        "lm-studio",
        "other",
    ]
    assert KEY not in out + err


def test_a_provider_name_that_shadows_a_built_in_is_asked_again(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    code, out, _, _ = wizard(
        [
            ("select", "ollama"),
            ("text", DEFAULT),
            ("text", DEFAULT),  # Ollama needs no key, so the variable defaults to blank
            ("text", "llava"),
            ("text", "pymupdf"),
            ("text", ""),  # give up on a name
        ],
        argv=["--provider"],
        capsys=capsys,
    )
    assert code == 0
    assert "✗" in out and "built-in backend id" in out
    assert "No provider was added" in out
    assert not (tmp_path / "openreading.yaml").exists()


def test_the_menu_choice_runs_the_provider_step(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    _probe_script(monkeypatch)
    code, out, _, _ = wizard(
        [
            ("checkbox", _pick(cli_setup.PROVIDER_CHOICE)),
            ("select", "lm-studio"),
            ("text", DEFAULT),
            ("text", DEFAULT),
            ("text", "qwen2-vl"),
            ("text", DEFAULT),
            ("confirm", True),  # write the providers entry
            ("confirm", False),  # no probe
            ("confirm", False),  # leave the untested backend out of policy.backends
        ],
        capsys=capsys,
    )
    assert code == 0
    loaded = config.load(tmp_path / "openreading.yaml")
    assert loaded is not None
    assert loaded.providers["lm-studio"]["base_url"] == "http://localhost:1234/v1"
    assert "key_env" not in loaded.providers["lm-studio"]


def test_replace_asks_again_and_other_probe_outcomes_are_named(
    wizard, monkeypatch, capsys, fake_keychain
):
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    _probe_script(monkeypatch, LivenessStatus.UNREACHABLE, LivenessStatus.CONFIGURED_UNVERIFIED)
    code, out, _, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("select", "replace"),
            ("password", KEY2),
            ("confirm", True),
            ("select", "retry"),
            ("password", KEY2),
            ("confirm", True),
            ("confirm", False),
        ],
        capsys=capsys,
    )
    assert code == 0
    assert "✗ anthropic-claude did not answer (unreachable)" in out
    assert "has no free test call" in out
    assert _saved(fake_keychain, "ANTHROPIC_API_KEY") == KEY2


def test_a_bad_base_url_adds_no_provider(wizard, capsys, fake_keychain, tmp_path):
    code, out, _, _ = wizard(
        [
            ("select", "other"),
            ("text", "ftp://nowhere"),
            ("text", DEFAULT),
            ("text", "m"),
        ],
        argv=["--provider"],
        capsys=capsys,
    )
    assert code == 0
    assert "✗ must be an http or https URL" in out and "No provider was added" in out
    assert not (tmp_path / "openreading.yaml").exists()


def test_declining_the_providers_write_adds_nothing(wizard, capsys, fake_keychain, tmp_path):
    code, out, _, _ = wizard(
        [
            ("select", "groq"),
            ("text", DEFAULT),
            ("text", DEFAULT),
            ("text", "llama-vision"),
            ("text", DEFAULT),
            ("confirm", DEFAULT),
        ],
        argv=["--provider"],
        capsys=capsys,
    )
    assert code == 0
    assert "No provider was added" in out
    assert not (tmp_path / "openreading.yaml").exists()


def test_every_keyed_preset_passes_the_profile_checks():
    from openreading import config
    from openreading.cli.setup import PRESETS

    for preset in PRESETS:
        if not preset.base_url:
            continue
        profile = {"kind": "openai-compatible", "base_url": preset.base_url, "model": "m"}
        if preset.key_env:
            profile["key_env"] = preset.key_env
        assert config.validate_providers({"preset-check": profile})


# --- every failure is a ✗ line, never a traceback ----------------------------------------------


def test_a_keychain_that_refuses_a_remove_is_a_cross_line(
    wizard, monkeypatch, capsys, fake_keychain
):
    _probe_script(monkeypatch, LivenessStatus.UNAUTHORIZED)

    def refuse(name):
        raise cli_setup.keychain.KeychainUnavailable("The keychain refused to remove it.")

    monkeypatch.setattr(cli_setup.keychain, "delete", refuse)
    code, out, err, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", True),
            ("select", "remove"),
        ],
        capsys=capsys,
    )
    assert code == 0, err
    assert "✗ Could not remove ANTHROPIC_API_KEY" in out
    assert "Traceback" not in err


def test_a_probe_that_raises_is_a_cross_line(wizard, monkeypatch, capsys, fake_keychain):
    def boom(adapter, *, broker, timeout_s=None):
        raise RuntimeError(f"connection reset {KEY}")

    monkeypatch.setattr(cli_keys, "check_liveness", boom)
    code, out, err, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", True),  # test the key
            ("select", "keep"),
        ],
        capsys=capsys,
    )
    assert code == 0, err
    assert "✗ anthropic-claude could not be tested (RuntimeError)" in out
    assert KEY not in out + err


def test_a_file_that_cannot_be_written_is_a_cross_line(
    wizard, monkeypatch, capsys, fake_keychain, tmp_path
):
    _probe_script(monkeypatch, LivenessStatus.LIVE)

    def deny(path, text):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(cli_setup, "_write", deny)
    code, out, err, _ = wizard(
        [
            ("checkbox", _pick("anthropic-claude")),
            ("password", KEY),
            ("confirm", True),
            ("confirm", True),  # write policy.backends
        ],
        capsys=capsys,
    )
    assert code == 0, err
    assert "✗ Could not write ./openreading.yaml (Permission denied)" in out
    assert not (tmp_path / "openreading.yaml").exists()
