## What and why

<!-- Describe the problem before the solution. If you rejected an alternative approach, say
     which and why. That context is usually the most valuable part of the review. -->

## Checklist

- [ ] `make verify` is green locally (ruff, pyright, pytest at the coverage floor, schema +
      extras checks, smokes)
- [ ] New behaviour has a test that fails without the change
- [ ] Hosted-backend behaviour is proven against fixtures, not live calls
- [ ] Rationale for non-obvious code is in a module docstring or a comment, not only in this PR
      description (see
      [AGENTS.md](https://github.com/multiversal-ventures/openreading-core/blob/main/AGENTS.md))
- [ ] No new markdown file outside the allowlist, and nothing added under `docs/`
- [ ] If this touches routing, strategies, or fallbacks: the PR states why the compliance-eligible
      set cannot widen
- [ ] Every row of the "Where a change gets documented" table in AGENTS.md that applies to this
      change is done (a docstring, a README row, an `.env.example` block, a re-run output)
- [ ] If a reader can see the change (a flag, an env var, a default, an endpoint, a schema
      version, a backend): one entry under `## [Unreleased]` in `CHANGELOG.md`
- [ ] The title reads `type(scope): description` and stays under 70 characters, because it becomes
      the commit subject on `main`
