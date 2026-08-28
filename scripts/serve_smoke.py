"""`make serve-smoke` — boot the real HTTP server on an ephemeral localhost port, POST the bundled
sample PDF through /v1/parse with the local pymupdf backend, assert the response is schema-valid,
then shut the server down. This exercises the full ASGI stack over a real socket (not TestClient).
Not part of `make verify` (which stays offline + fast); run explicitly."""

from __future__ import annotations

import base64
import socket
import subprocess
import sys
import time

import httpx

from openreading import schemas
from openreading.testing.sample_pdf import build_sample_pdf


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "openreading.server.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):  # up to ~15s for the server to come up
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            print("serve-smoke: FAIL — server did not start", file=sys.stderr)
            return 1

        body = {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": "pymupdf"},
        }
        r = httpx.post(f"{base}/v1/parse", json=body, timeout=30.0)
        r.raise_for_status()
        data = r.json()
        schemas.validate_response(data)
        assert data["backend"]["id"] == "pymupdf", data["backend"]
        n = len(data["document"]["pages"][0].get("blocks", []))
        print(f"serve-smoke: OK — POST /v1/parse pymupdf returned schema-valid JSON ({n} blocks)")

        # Manifest v0.6: /v1/batch over two documents → one schema-valid batch envelope.
        doc = body["document"]
        br = httpx.post(
            f"{base}/v1/batch",
            json={"documents": [doc, doc], "backend": "pymupdf"},
            timeout=30.0,
        )
        br.raise_for_status()
        env = br.json()
        schemas.validate_batch_result(env)
        assert env["summary"]["succeeded"] == 2, env["summary"]
        print(
            f"serve-smoke: OK — POST /v1/batch pymupdf returned {env['summary']['succeeded']}/2 succeeded"
        )
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
