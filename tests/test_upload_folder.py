"""Exercise the standalone folder upload recipe through an offline curl executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upload_folder.py"


@pytest.fixture
def client(tmp_path: Path):
    source = tmp_path / "input"
    source.mkdir()
    output = tmp_path / "output"
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "calls.jsonl"
    fake = binary / "curl"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CURL_TEST_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if '--header' in args and args[args.index('--header') + 1] == '@-':\n"
        "    pathlib.Path(os.environ['CURL_TEST_LOG'] + '.headers').write_text(sys.stdin.read())\n"
        "form = args[args.index('--form') + 1]\n"
        "source = pathlib.Path(json.loads(form[6:]))\n"
        "with open(os.environ['CURL_TEST_LOG'] + '.bytes', 'a') as payloads:\n"
        "    payloads.write(json.dumps(source.read_bytes().hex()) + '\\n')\n"
        "target = pathlib.Path(args[args.index('--output') + 1])\n"
        "if 'unreadable' in source.name:\n"
        "    sys.exit(26)\n"
        "if 'transport' in source.name:\n"
        "    sys.exit(7)\n"
        "body = b'{\"error\":\"refused\"}' if 'http-fail' in source.name else b'{\"ok\":true}'\n"
        "target.write_bytes(body)\n"
        "print('422' if 'http-fail' in source.name else '200', end='')\n"
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": str(binary), "CURL_TEST_LOG": str(log)}
    env.pop("OPENREADING_API_KEY", None)

    def run(*extra: str):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                str(source),
                "--url",
                "http://example.invalid/v1/parse",
                "--backend",
                "docling",
                "--output",
                str(output),
                *extra,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    return source, output, log, env, run


def test_serial_mapping_selection_and_multipart(client):
    source, output, log, env, run = client
    names = ["b/report odd;é.txt", "a/report odd;é.txt", 'Z,quote".bin']
    for name in names + [".hidden", ".private/no", "a/.secret"]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"actual file bytes")
    (source / "linked").symlink_to(source / "a", target_is_directory=True)
    (source / "alias").symlink_to(source / names[0])
    env["OPENREADING_API_KEY"] = "test-token"
    result = run()
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["path"] for row in records[:-1]] == sorted(names, key=os.fsencode)
    assert records[-1] == {
        "total": 3,
        "success": 3,
        "http_error": 0,
        "transport_error": 0,
        "local_error": 0,
    }
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 3
    assert [json.loads(line) for line in Path(str(log) + ".bytes").read_text().splitlines()] == [
        b"actual file bytes".hex()
    ] * 3
    for name, args in zip(sorted(names, key=os.fsencode), calls, strict=True):
        assert "--retry" not in args
        assert "--form-string" in args
        assert json.loads(args[args.index("--form-string") + 1].removeprefix("request=")) == {
            "backend": {"id": "docling"}
        }
        quoted = str(source / name).replace("\\", "\\\\").replace('"', '\\"')
        assert args[args.index("--form") + 1] == f'file=@"{quoted}"'
        assert args[args.index("--header") + 1] == "@-"
        assert all("test-token" not in arg for arg in args)
        assert Path(str(log) + ".headers").read_text() == "Authorization: Bearer test-token\n"
        assert (output / name).read_bytes() == b'{"ok":true}'


def test_failures_continue_without_retries_and_keep_http_body(client):
    source, output, log, _, run = client
    for name in ["a-http-fail", "b-transport", "c-unreadable", "d-ok"]:
        (source / name).write_bytes(b"document")
    result = run()
    assert result.returncode == 1
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["outcome"] for row in records[:-1]] == [
        "http_error",
        "transport_error",
        "local_error",
        "success",
    ]
    assert records[-1] == {
        "total": 4,
        "success": 1,
        "http_error": 1,
        "transport_error": 1,
        "local_error": 1,
    }
    assert records[0]["http_status"] == 422
    assert records[1]["curl_exit"] == 7
    assert records[2]["curl_exit"] == 26
    assert len(log.read_text().splitlines()) == 4
    assert (output / "a-http-fail").read_bytes() == b'{"error":"refused"}'


def test_output_inside_input_rejected_before_upload(client):
    source, _, log, _, run = client
    (source / "doc").write_bytes(b"doc")
    result = run("--output", str(source / "results"))
    assert result.returncode == 2
    assert "outside" in result.stderr
    assert not log.exists()


def test_missing_curl_is_reported_per_file(client):
    source, _, log, env, run = client
    (source / "doc").write_bytes(b"doc")
    env["PATH"] = str(source)
    result = run()
    assert result.returncode == 1
    assert json.loads(result.stdout.splitlines()[0])["outcome"] == "local_error"
    assert not log.exists()


def test_failed_rerun_does_not_leave_old_response(client):
    source, output, _, _, run = client
    (source / "transport").write_bytes(b"doc")
    output.mkdir()
    response = output / "transport"
    response.write_bytes(b"old success")
    assert run().returncode == 1
    assert not response.exists()


def test_output_symlink_cannot_overwrite_input(client):
    source, output, log, _, run = client
    original = source / "document"
    original.write_bytes(b"original")
    output.mkdir()
    (output / "document").symlink_to(original)
    result = run()
    assert result.returncode == 1
    assert original.read_bytes() == b"original"
    assert not log.exists()


def test_empty_selection_has_zero_totals(client):
    _, _, log, _, run = client
    result = run()
    assert result.returncode == 0
    assert json.loads(result.stdout)["total"] == 0
    assert not log.exists()


def test_response_mapping_preserves_tree_without_suffix_collisions(client):
    source, output, _, _, run = client
    (source / "a").write_bytes(b"document")
    (source / "a.response.json").mkdir()
    (source / "a.response.json" / "b").write_bytes(b"document")
    long_name = "x" * 255
    (source / long_name).write_bytes(b"document")
    result = run()
    assert result.returncode == 0, result.stdout
    for relative in ["a", "a.response.json/b", long_name]:
        assert (output / relative).read_bytes() == b'{"ok":true}'


def test_unreadable_directory_reports_failure_and_uploads_siblings(client):
    if os.geteuid() == 0:
        pytest.skip("Root bypasses directory permissions")
    source, output, log, _, run = client
    inaccessible = source / "a-private"
    inaccessible.mkdir()
    (inaccessible / "doc").write_bytes(b"document")
    (source / "b-readable").write_bytes(b"document")
    inaccessible.chmod(0)
    try:
        result = run()
    finally:
        inaccessible.chmod(0o700)
    assert result.returncode == 1
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(row["path"], row["outcome"]) for row in records[:-1]] == [
        ("a-private", "local_error"),
        ("b-readable", "success"),
    ]
    assert records[-1] == {
        "total": 2,
        "success": 1,
        "local_error": 1,
        "http_error": 0,
        "transport_error": 0,
    }
    assert (output / "b-readable").read_bytes() == b'{"ok":true}'
    assert len(log.read_text().splitlines()) == 1
