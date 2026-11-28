"""The DeepSeek Harness adapter.

DSH loads two things from one directory: a configuration-only bundle that
registers ``paperdb-mcp`` as ``@local/dsh-paperdb-mcp`` (a ``link:`` dependency
of the active profile), and the ``my-paperdb`` skill.  The installed layout is
what DSH resolves, so it is pinned here rather than in the generic installer:

    <target>/mcp/dsh-paperdb-mcp/package.json
    <target>/mcp/dsh-paperdb-mcp/cordis.patch.yml
    <target>/skills/my-paperdb/SKILL.md

The default target keeps the pre-rename path (``~/.dsh/paperdb-integration``)
so an existing profile link keeps resolving; ``PAPERDB_DSH_INTEGRATION_DIR`` and
the ``.paperdb-update`` state directory are still read for continuity, but new
writes use ``PAPERDB_ADAPTER_DIR`` and ``.paperdb-adapter``.
"""

from __future__ import annotations

from pathlib import Path

from paperdb.adapters.base import Adapter

DSH = Adapter(
    host="dsh",
    source_dir="adapters/dsh",
    install_map={
        "bundle/package.json": "mcp/dsh-paperdb-mcp/package.json",
        "bundle/cordis.patch.yml": "mcp/dsh-paperdb-mcp/cordis.patch.yml",
        "skills/my-paperdb/SKILL.md": "skills/my-paperdb/SKILL.md",
    },
    templated=frozenset({"bundle/cordis.patch.yml"}),
    restart_prefixes=("mcp/",),
    state_dirname=".paperdb-adapter",
    legacy_state_dirname=".paperdb-update",
    skill_name="my-paperdb",
    package_manifest="mcp/dsh-paperdb-mcp/package.json",
    default_target=Path.home() / ".dsh" / "paperdb-integration",
    legacy_dir_env="PAPERDB_DSH_INTEGRATION_DIR",
)
