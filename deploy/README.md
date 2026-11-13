# Deploying the remote MCP servers

**Deployed by the POS release, not from here.** The compose file, the pinned image
tag and the Caddy routes live in the opengunstore repo:

- `deploy/prod/mcp/compose.yaml` — read-only (8781) and full (8782) servers on
  each store's POS host, host network, loopback only;
  plus the website connector (`GUNSTORE_MCP_MODE=web`), started only on a host
  where the store has put its site credentials;
- `deploy/prod/mcp/version.env` — `MCP_TAG=sha-<7>`, the build that POS release runs;
- `deploy/prod/auto_deploy.sh` — brings them up after every POS deploy;
- `deploy/prod/caddy-connector.caddy` — the routes in each `pos.<domain>` block;
- `deploy/prod/RUNBOOK.md` §15.

## Shipping an MCP change

1. Merge to `main` here → the `image` workflow runs the tests, then pushes
   `ghcr.io/old-steel-arsenal/gunstore-pos-mcp:sha-<7>` (and `:main`).
2. In opengunstore, open a PR that sets `MCP_TAG=sha-<7>` in
   `deploy/prod/mcp/version.env`.
3. It goes out with the next POS release, on both stores; rolling the POS back
   rolls the MCP build back with it.

The servers are switched on per store in the POS (Settings → MCP Settings), and
every call is recorded in Connector Audit Log.
