# DeepSeek Harness adapter (source of truth)

This directory is the DSH half of the installation: the configuration-only
bundle that registers `paperdb-mcp` as `mcp__paperdb__*`, and the `my-paperdb`
skill. It is hand-edited here, packaged by `scripts/build_adapter_release.py`,
and published as the GitHub Release asset
`paperdb-adapter-dsh-<version>.tar.gz` by `.github/workflows/release.yml`.

The MCP server itself does not know this directory exists. DSH-specific things
(a host config path, a bundle patch, a skill) stop at this boundary.

```
adapters/dsh/
├── bundle/
│   ├── cordis.patch.yml   # the bundle: stdio command + neutral env for the MCP client
│   └── package.json       # configuration-only DSH bundle manifest
└── skills/
    └── my-paperdb/
        └── SKILL.md       # the agent skill
```

## Source vs installed copy

DSH never reads this directory. It reads the **installed copy** at
`PAPERDB_ADAPTER_DIR` (default here: `~/.dsh/paperdb-integration/`), rendered
from this source:

```
adapters/dsh/                     # SOURCE — edit here, in the repo
  bundle/*.yml                    #   template, placeholders, no secret
  skills/my-paperdb/SKILL.md

        │  scripts/install_adapter.py --host dsh     (local)
        │  paperdb adapter update --host dsh          (from a release)
        ▼

~/.dsh/paperdb-integration/       # INSTALLED — what DSH actually loads
  mcp/dsh-paperdb-mcp/cordis.patch.yml   # rendered for this machine
  mcp/dsh-paperdb-mcp/package.json
  skills/my-paperdb/SKILL.md             # skill snapshot
  .paperdb-adapter/state.json            # version + per-file sha256
```

The installed layout is pinned by DSH, not by the build: the profile has a
`link:` dependency on `mcp/dsh-paperdb-mcp`, and the skill is symlinked from
`~/.agents/skills/my-paperdb`. Both must keep resolving, which is why the
default target is still `~/.dsh/paperdb-integration` and `.paperdb-update/`
state written by older installs is still read.

| Consumer | Link target |
|---|---|
| `~/.agents/skills/my-paperdb` | `~/.dsh/paperdb-integration/skills/my-paperdb` |
| `<repo>/.dsh/skills/my-paperdb` | `~/.dsh/paperdb-integration/skills/my-paperdb` |
| `~/.dsh/profiles/<profile>/node_modules/@local/dsh-paperdb-mcp` | `~/.dsh/paperdb-integration/mcp/dsh-paperdb-mcp` |

The two skill links are absolute and machine-local: on a fresh clone they
dangle until the adapter is installed.

### Development loop

```bash
# 1. edit adapters/dsh/skills/my-paperdb/SKILL.md or the bundle template
# 2. render + install into ~/.dsh/paperdb-integration
.venv/bin/python scripts/install_adapter.py --host dsh --target ~/.dsh/paperdb-integration
# 3. skill changes: picked up by new DSH sessions
#    bundle changes: need a fresh process to see (restart DSH)
```

`install_adapter.py` builds the same asset the release workflow builds, verifies
it against its own manifest, renders the template, and writes atomically
(previous files kept in `.paperdb-adapter/backups/`, last 3). `--dry-run` shows
what would change.

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
manifest.json                        # kind, host, version, per-file sha256/size
bundle/package.json
bundle/cordis.patch.yml
skills/my-paperdb/SKILL.md
```

## Installing / updating

```bash
paperdb adapter status   --host dsh            # offline: version, files, local edits
paperdb adapter check    --host dsh            # installed vs latest release
paperdb adapter update   --host dsh --totp 123456
paperdb adapter rollback --host dsh --totp 123456
```

Applying an update needs the 6-digit TOTP because it writes agent instructions
and DSH configuration on this machine; the CLI proves the code against
`POST /v1/totp/verify` and only then touches disk. `--dry-run` needs no code.

Trust model: the tarball is checked against the GitHub asset digest
(`sha256:...`), falling back to the release's `SHA256SUMS`; `manifest.json` then
pins every extracted file. A compromised GitHub release (or repository write
access) is outside this model — keep the release environment protected.

Updates are written atomically, previous files are kept in
`<target>/.paperdb-adapter/backups/` (last 3), and state lives in
`<target>/.paperdb-adapter/state.json`. State from older installs lives in
`<target>/.paperdb-update/` and is still read for status and rollback.

Restarts: a changed `cordis.patch.yml` only takes effect after DSH restarts;
skill changes are visible to new sessions.
