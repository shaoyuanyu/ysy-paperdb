# DSH integration (source of truth)

This directory **is** the DeepSeek Harness integration: the MCP bundle that
registers `paperdb-mcp` as `mcp__paperdb__*`, and the `my-paperdb` skill. It is
hand-edited here, packaged by `scripts/build_dsh_release.py`, and published as
the GitHub Release asset `paperdb-dsh-<version>.tar.gz` by
`.github/workflows/release.yml` on every `v*` tag.

```
for-agents/
├── mcp/
│   ├── dsh-paperdb-mcp/
│   │   ├── cordis.patch.yml   # the bundle: stdio command + env for the MCP client
│   │   └── package.json       # configuration-only DSH bundle manifest
└── skills/
    └── my-paperdb/
        └── SKILL.md           # the agent skill
```

Two symlinks point here so DSH discovers both halves without copying:

| Link | Points at |
|---|---|
| `~/.agents/skills/my-paperdb` | `for-agents/skills/my-paperdb` (every DSH session) |
| `<repo>/.dsh/skills/my-paperdb` | `../../for-agents/skills/my-paperdb` (sessions in this repo) |
| `~/.dsh/profiles/<profile>/node_modules/@local/dsh-paperdb-mcp` | `for-agents/mcp/dsh-paperdb-mcp` (the bundle) |

## The token is not in the bundle

`cordis.patch.yml` carries no secret. `paperdb-mcp` resolves its token as:

1. `PAPERDB_MCP_TOKEN` environment variable, if set;
2. else the file named by `PAPERDB_MCP_TOKEN_FILE`;
3. else `~/.config/paperdb/mcp-token`.

The rendered bundle points (2) at `~/.config/paperdb/mcp-token`, outside the
repository — so the bundle stays publishable and the secret never touches git
(a repo-local `mcp-token` is git-ignored as a safety net). `chmod 600` the file;
rotate the token in the web UI if it ever leaks.

## Publishing a release

1. Bump the version in `pyproject.toml` **and** `src/paperdb/__init__.py`.
2. Commit, then tag and push:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

3. `release.yml` verifies `v<version>` matches `pyproject.toml`, builds the
   asset from this directory, and creates the GitHub Release.

Asset layout (fixed, what the updater expects):

```
manifest.json                     # version + per-file sha256/size
bundle/package.json
bundle/cordis.patch.yml
skill/my-paperdb/SKILL.md
```

## Installing / updating from a DSH session

`paperdb-mcp` exposes three tools:

| Tool | Effect |
|---|---|
| `dsh_integration_status` | read-only: installed version, local drift, newest release |
| `update_dsh_integration` | verify + install latest (or pinned) release; requires the owner's current TOTP |
| `rollback_dsh_integration` | restore the files backed up by the last update; requires TOTP |

Install target is `PAPERDB_DSH_INTEGRATION_DIR` (the bundle sets it to this
directory's absolute path). Applying an update needs the 6-digit TOTP because it
writes agent instructions and DSH configuration on the machine running the MCP
server; the MCP server proves the code against `POST /v1/totp/verify` and only
then touches disk.

Trust model: the tarball is checked against the GitHub asset digest
(`sha256:...`), falling back to the release's `SHA256SUMS`; `manifest.json` then
pins every extracted file. A compromised GitHub release (or repository write
access) is outside this model — keep the release environment protected.

Updates are written atomically, previous files are kept in
`<target>/.paperdb-update/backups/` (last 3), and state lives in
`<target>/.paperdb-update/state.json`.

Restarts: a changed `cordis.patch.yml` only takes effect after DSH restarts;
skill changes are visible to new sessions.
