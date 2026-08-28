## What and why

<!-- Describe the problem before the solution. If you rejected an alternative approach, say
     which and why — that context is usually the most valuable part of the review. -->

## Checklist

- [ ] `make verify` is green locally (ruff, pyright, pytest at the coverage floor, schema + extras checks, smokes)
- [ ] New behaviour has a test that fails without the change; hosted-backend behaviour is proven against fixtures, not live calls
- [ ] Rationale for non-obvious code is in a module docstring or a comment, not only in this PR description (see [AGENTS.md](../AGENTS.md))
- [ ] No new markdown file outside the allowlist; nothing added under `docs/`
- [ ] If this touches routing, strategies, or fallbacks: the PR states why the compliance-eligible set cannot widen
- [ ] If this adds an env var, exit code, endpoint, or CLI flag: the owning module's docstring documents it
