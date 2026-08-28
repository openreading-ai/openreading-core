# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security vulnerabilities.

Report through GitHub's private vulnerability reporting
([Security → Report a vulnerability](https://github.com/multiversal-ventures/openreading-core/security/advisories/new)),
or by email to <creativeaisle@gmail.com> if you would rather not use GitHub.

Include the affected version or commit, what an attacker gains, and a reproduction if you have
one. We will acknowledge within a few days and keep you updated as we work on a fix. If you
would like credit in the advisory, say so and tell us how you would like to be named.

## What counts as a vulnerability here

OpenReading forwards documents and *your* credentials to third-party backends, and decides —
by policy — which backends a document is allowed to reach. The interesting boundaries are
those. We are especially interested in reports of:

- **Compliance-filter bypass** — any path by which a request carrying `require_baa`,
  `no_train_on_data`, or a local-only policy reaches a backend the policy should have dropped:
  through a strategy construct, a fallback chain, a route, or a descriptor field that lies.
- **Credential leakage** — a key appearing anywhere other than the outbound request to its own
  backend: logs, error messages, recorded fixtures, `backend_raw`, the ledger journal, the
  HTTP server's responses, a comparison report.
- **Document exfiltration** — document bytes leaving the machine on a path the request did not
  ask for (a "local" backend that phones home, a liveness probe that submits content, a batch
  path that uploads when it should not).
- **Schema-boundary validation gaps** — server input that is accepted but violates the vendored
  request schema, or a response that is emitted without validating against the response schema.
- **Webhook signature bypass** — a webhook accepted without a verified signature, or accepted
  when no signing secret is configured.

## Out of scope

- The quality, safety, or accuracy of any backend's output.
- Attacks that require the operator to have already granted the access being abused — a policy
  that admits a backend, and a document that then reaches it, is working as designed.
- Vulnerabilities in dependencies that do not affect OpenReading. Report those upstream; tell
  us if we should pin or patch around them.

## Supported versions

The project is pre-1.0 and moving quickly. Only the `main` branch and the most recent release
receive fixes.
