"""Report backend readiness to an agent by variable name, never by value.

The `openreading_backends` tool returns one row per backend this process knows: the built-ins
plus any provider profile the launcher's `--config` file installed. A row looks like
`{"backend": "anthropic-claude", "ready": true, "extra_installed": true, "missing": [],
"found": [{"var": "ANTHROPIC_API_KEY", "source": "keychain"}]}`. A source is `env` or
`keychain`, because the launcher never loads `.env`. `env_ignored` names a provider profile's
key variable that is set in the environment but not read, because `OPENREADING_PROVIDER_ORIGINS`
does not list the profile's origin (`openreading.credentials`). It is empty for every other
backend. The report also states whether this process reads the keychain, and ends with the
sentence `openreading.mcp_server.tools.NEXT_STEP`.

Only the engine launcher, `openreading.mcp_server.main.serve`, passes this report to
`create_server`. The client-only wheel ships no adapter registry, so its server has no such tool.
A launcher with an ImportExecution omits it too, because the Core server it sends documents to
holds the keys, and that server's `GET /v1/backends` answers for them.

The tool takes no arguments, so no key can enter through it. A key typed into an agent
conversation reaches the model provider and stays in the transcript, which is why configuring a
backend happens in a terminal instead. Readiness is the offline judgement of
`openreading.readiness.backend_readiness`. It never proves that a provider accepts the key.
"""

from __future__ import annotations

from typing import Any

from openreading.adapters.registry import known_backends
from openreading.credentials import EnvCredentialBroker, keychain_in_use
from openreading.mcp_server.tools import NEXT_STEP
from openreading.readiness import backend_readiness


def backends_report() -> dict[str, Any]:
    """Every known backend's readiness, with variable names and their sources only."""
    broker = EnvCredentialBroker()
    rows = []
    for slug, factory in sorted(known_backends().items()):
        adapter = factory()
        descriptor = adapter.descriptor
        readiness = backend_readiness(adapter, broker=broker)
        found = [
            {"var": var, "source": source}
            for var, source in broker.secret_sources(descriptor)
            if source != "missing"
        ]
        secrets = {f.env[0] if f.env else f.key for f in descriptor.credentials_spec if f.secret}
        # The keychain holds declared secrets only, so every other resolved field came from the
        # process environment.
        found += [
            {"var": var, "source": "env"} for var in readiness.creds_found if var not in secrets
        ]
        rows.append(
            {
                "backend": slug,
                "ready": readiness.ready,
                "extra_installed": readiness.extra_installed,
                "missing": readiness.creds_missing,
                "found": found,
                "env_ignored": readiness.env_ignored,
            }
        )
    return {"backends": rows, "keychain": keychain_in_use(), "next_step": NEXT_STEP}
