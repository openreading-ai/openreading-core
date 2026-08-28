"""`python -m openreading.cli` entrypoint. The implementation lives in `app` so importing the
package (`__init__`) never pulls in the `__main__` module, which would trigger a runpy double-import
warning."""

from __future__ import annotations

from openreading.cli.app import main

if __name__ == "__main__":
    raise SystemExit(main())
