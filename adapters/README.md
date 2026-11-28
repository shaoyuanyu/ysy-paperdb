# Host adapters

The MCP server is **harness-neutral**: `paperdb-mcp` speaks stdio MCP, reads only
`PAPERDB_MCP_*` configuration, and exposes the same tools no matter which agent
host launched it. Nothing in the tool surface names a host.

## Terms

Three words do all the work here; keep them apart when naming things.

- **host** — the concrete harness/agent that launches the server, e.g. `dsh`.
  It never appears in the tool surface, in a command name or in an `Adapter`
  field name: in commands it is a value (`--host dsh`), and in artifacts it is
  only the per-host key (`adapters/dsh/`, `paperdb-adapter-dsh-<version>.tar.gz`).
- **adapter** — the per-host layer that makes the server loadable there: the
  payload in `adapters/<host>/` plus the install/update/rollback logic in
  `src/paperdb/adapters/`. An adapter is installed, updated and rolled back; it
  is not a "client", not a "plugin", and not the agent itself.
- **skill** — the instruction snapshot the host's agent reads
  (`skills/my-paperdb/SKILL.md`). It ships inside the adapter, so it shares the
  adapter's version, trust boundary and update cycle.

The rule that follows: capability names are host-free, host names are data.
That is why the command group is `paperdb adapter … --host dsh` rather than
`paperdb dsh …`, and why adding a host is a directory plus one `Adapter`
literal instead of a new tool or a new command group.

What *is* host-specific is the glue that registers that server into a host: the
config file the host loads, the template that points it at the `paperdb-mcp`
launcher, and the skill text the host's agent reads. Each host's glue lives in
one directory here and ships as one release asset:

```
adapters/<host>/                 # payload — what the adapter installs
  bundle/package.json            #   host bundle manifest (version rendered)
  bundle/cordis.patch.yml        #   template, rendered for this machine
  skills/my-paperdb/SKILL.md     #   the skill snapshot

paperdb-adapter-<host>-<version>.tar.gz
```

| Host | Directory | Install target (default) | Release asset |
|---|---|---|---|
| DeepSeek Harness | [`dsh/`](dsh/README.md) | `~/.dsh/paperdb-integration` | `paperdb-adapter-dsh-<version>.tar.gz` |

## Commands

Installing and updating is an owner action on the local machine, so it lives on
the `paperdb` CLI rather than on the MCP tool surface:

```bash
paperdb adapter status   --host dsh          # installed version, files, local edits (offline)
paperdb adapter check    --host dsh          # installed vs latest published release
paperdb adapter update   --host dsh --totp 123456
paperdb adapter rollback --host dsh --totp 123456
```

Writes need the owner's current 6-digit TOTP code, verified against the same
`POST /v1/totp/verify` endpoint the site uses. `--json` gives machine-readable
output; `--dry-run` previews an update without a code or a write.

Install directory resolution, in order: `--dir`, `PAPERDB_ADAPTER_DIR`, the
host's legacy variable (`PAPERDB_DSH_INTEGRATION_DIR` for DSH), then the host
default above.

## Tool groups

The server registers tools by workflow group (`import`, `read`, `organize`,
`workflow`, `library`) and starts with `import,read,organize`. Narrow or widen
that per host session with `PAPERDB_MCP_TOOLSETS=all` (or a comma list); inside a
session, `toolset(op="enable", groups=["workflow"])` loads more. An adapter's
bundle may pin the startup set for its host.

## Local file access

`upload_pdf` / `import_pdf_file` read a path on the host, so the server confines
reads to PDFs (absolute path, `%PDF-` header, ≤ 200 MiB) under a small set of
roots:

- `~/Downloads`, `~/Desktop`, `~/Documents`, the OS temp directory;
- plus every directory in `PAPERDB_MCP_LOCAL_ROOTS` (`:`-separated, `~` expanded
  per entry).

The path is resolved before the check, so a symlink cannot escape a root.
Rendered pages are cached under `PAPERDB_MCP_CACHE_DIR` (default
`~/.cache/paperdb/mcp`) — that is a write cache, not a read root.

An adapter's bundle should pin `PAPERDB_MCP_LOCAL_ROOTS` when the owner keeps
PDFs outside those defaults (say `~/Projects/papers`): the **host** spawns the
server, so exporting the variable in a shell does not reach it.

## Adding a host

1. Create `adapters/<host>/` with the payload files the host needs.
2. Add one `Adapter` literal in `src/paperdb/adapters/<host>.py` — payload
   directory, asset→installed path map, templated files, restart semantics,
   state directory, default target.
3. Register it in `ADAPTERS` in `src/paperdb/adapters/__init__.py`.

No change to `paperdb.mcp_server` is required or wanted: if a host needs a
host-specific *tool*, the design is wrong — that behaviour belongs in the
adapter's install/update path, or in the host's own skill.

## Build

```bash
uv run python scripts/build_adapter_release.py --host dsh --out dist
```

The release workflow (`.github/workflows/release.yml`) runs this on every `v*`
tag and publishes the asset plus `SHA256SUMS`; `paperdb adapter update` verifies
the asset digest and then every file against `manifest.json`.
