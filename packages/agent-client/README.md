# Agent client build profile

This pinned Git build contains document retention, search/read, export, job supervision and stdio MCP tools.
It uses the same source files and schema resources as the full Core distribution.
`profile.py` selects every module and schema explicitly. Namespace initializers expose only client contracts.

Agent Tools selects this profile with the Git URL fragment `subdirectory=packages/agent-client`.
Build its wheel directly from this checkout with `uv build --wheel packages/agent-client`.
The resulting wheel has the distribution name `openreading`, so a resolver cannot install both profiles together.
Use an isolated client environment. Install full Core separately to run `openreading serve`.
Do not publish this profile as the full Core wheel on a package index.

The caller supplies an acquisition service and fixed detached-job execution.
For example, Agent Tools uploads selected bytes to its configured server and retains the response locally.
This profile has no parser, adapter registry, Core HTTP server, routing engine, model provisioning or Core CLI.
Dependencies cover schema validation, the MCP protocol and process lifecycle only.
This profile requires `mcp>=1.30,<2`; the full Core `agent` extra currently permits
`mcp>=1.28,<2`. Keep their floors in view when changing the shared MCP modules.
`uv.lock` pins this profile's resolved dependency graph for `make audit-client`.
