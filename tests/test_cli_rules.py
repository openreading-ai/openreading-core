"""`openreading rules`: generate publisher rules from labels a dataset already carries.

The verb rewrites files a person hand-labeled, so the defaults matter more than the mechanics.
It prints unless told to write, it refuses to clobber rules somebody already wrote, and it says
out loud that the one rule type worth having (`absent`) is the one it cannot generate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.cli import main


def _dataset(root: Path, **expected) -> Path:
    case = root / "invoice"
    case.mkdir(parents=True)
    (case / "case.json").write_text(
        json.dumps({"name": "invoice", "input": {"path": "input.pdf"}, "expected": expected}),
        encoding="utf-8",
    )
    return root


def test_printing_is_the_default_because_it_rewrites_hand_written_files(tmp_path, capsys) -> None:
    dataset = _dataset(tmp_path, text_contains=["Total due"])

    assert main(["rules", str(dataset)]) == 0

    out, err = capsys.readouterr()
    assert '"type": "present"' in out
    assert "nothing written" in err
    # The file is untouched.
    assert "rules" not in json.loads((dataset / "invoice" / "case.json").read_text())["expected"]


def test_write_applies_them_and_says_what_it_cannot_generate(tmp_path, capsys) -> None:
    dataset = _dataset(tmp_path, text_contains=["Total due"])

    assert main(["rules", str(dataset), "--write"]) == 0

    expected = json.loads((dataset / "invoice" / "case.json").read_text())["expected"]
    assert expected["rules"] == [{"type": "present", "id": "contains_0", "text": "Total due"}]
    # `absent` is the reason to use rules at all, and no label implies one.
    assert "must NOT appear" in capsys.readouterr().err


def test_existing_rules_are_not_clobbered_without_force(tmp_path, capsys) -> None:
    dataset = _dataset(
        tmp_path,
        text_contains=["Total due"],
        rules=[{"type": "absent", "id": "mine", "text": "hand written"}],
    )

    assert main(["rules", str(dataset), "--write"]) == 0

    kept = json.loads((dataset / "invoice" / "case.json").read_text())["expected"]["rules"]
    assert kept == [{"type": "absent", "id": "mine", "text": "hand written"}]
    assert "already has rules" in capsys.readouterr().out


def test_force_replaces_them(tmp_path) -> None:
    dataset = _dataset(
        tmp_path,
        text_contains=["Total due"],
        rules=[{"type": "absent", "id": "mine", "text": "hand written"}],
    )

    assert main(["rules", str(dataset), "--write", "--force"]) == 0

    rules = json.loads((dataset / "invoice" / "case.json").read_text())["expected"]["rules"]
    assert [rule["id"] for rule in rules] == ["contains_0"]


def test_a_case_with_nothing_generatable_says_so_rather_than_writing_an_empty_list(
    tmp_path, capsys
) -> None:
    dataset = _dataset(tmp_path, text="a whole page of prose")

    assert main(["rules", str(dataset), "--write"]) == 0

    assert "nothing to generate from text" in capsys.readouterr().out
    assert "rules" not in json.loads((dataset / "invoice" / "case.json").read_text())["expected"]


def test_a_directory_with_no_cases_exits_3(tmp_path, capsys) -> None:
    assert main(["rules", str(tmp_path)]) == 3
    assert "no <case>/case.json" in capsys.readouterr().err


def test_unreadable_case_json_exits_3(tmp_path, capsys) -> None:
    case = tmp_path / "broken"
    case.mkdir()
    (case / "case.json").write_text("{not json", encoding="utf-8")

    assert main(["rules", str(tmp_path)]) == 3
    assert "[rules]" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--write", "--force"])
def test_the_flags_exist_and_are_documented(flag: str) -> None:
    from openreading.cli.app import build_parser

    action = next(
        a
        for a in build_parser()._subparsers._group_actions[0].choices["rules"]._actions
        if flag in (a.option_strings or [])
    )
    assert action.help
