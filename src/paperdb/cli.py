from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from paperdb.config import get_settings
from paperdb.db.session import dispose_engine, get_sessionmaker
from paperdb.errors import ApiError
from paperdb.venues.datasets import import_dataset, load_dataset


def _dataset_import(args: argparse.Namespace) -> int:
    async def run() -> int:
        try:
            path = Path(args.file)
            if not path.exists():
                print(f"file not found: {path}", file=sys.stderr)
                return 2
            payload, checksum = load_dataset(path)
            async with get_sessionmaker()() as session:
                try:
                    stats = await import_dataset(
                        session,
                        scheme=args.scheme,
                        version=args.version,
                        payload=payload,
                        checksum=checksum,
                        valid_from=args.valid_from,
                        valid_to=args.valid_to,
                        label=args.label,
                        source_url=args.source_url,
                        force=args.force,
                    )
                    await session.commit()
                except ApiError as exc:
                    await session.rollback()
                    print(json.dumps({"error": exc.code, "detail": exc.detail}), file=sys.stderr)
                    return 1
                except Exception:
                    await session.rollback()
                    raise
            print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
            return 0
        finally:
            await dispose_engine()

    return asyncio.run(run())


def _dataset_list() -> int:
    from sqlalchemy import select

    from paperdb.db.models import DatasetVersion

    async def run() -> int:
        try:
            async with get_sessionmaker()() as session:
                rows = (
                    await session.scalars(
                        select(DatasetVersion).order_by(
                            DatasetVersion.scheme, DatasetVersion.version.desc()
                        )
                    )
                ).all()
                print(
                    json.dumps(
                        [
                            {
                                "scheme": row.scheme.value,
                                "version": row.version,
                                "label": row.label,
                                "checksum": row.checksum,
                                "validFrom": row.valid_from,
                                "validTo": row.valid_to,
                                "isCurrent": row.is_current,
                            }
                            for row in rows
                        ],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return 0
        finally:
            await dispose_engine()

    return asyncio.run(run())


def _db_upgrade() -> int:
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "paperdb.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


def _worker(args: argparse.Namespace) -> int:
    from paperdb.jobs.worker import run_forever

    try:
        asyncio.run(run_forever(interval=args.interval))
    except KeyboardInterrupt:
        return 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paperdb", description="paperdb backend CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    datasets = sub.add_parser("datasets", help="evaluation dataset operations")
    datasets_sub = datasets.add_subparsers(dest="datasets_command", required=True)
    imp = datasets_sub.add_parser("import", help="import CCF/CAS dataset")
    imp.add_argument("--scheme", required=True, choices=["ccf", "cas"])
    imp.add_argument("--version", required=True)
    imp.add_argument("--file", required=True)
    imp.add_argument("--valid-from", type=int, required=True)
    imp.add_argument("--valid-to", type=int)
    imp.add_argument("--label")
    imp.add_argument("--source-url")
    imp.add_argument("--force", action="store_true")
    imp.set_defaults(func=_dataset_import)
    datasets_sub.add_parser("list", help="list imported datasets").set_defaults(
        func=lambda _args: _dataset_list()
    )

    db = sub.add_parser("db", help="database migrations")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("upgrade", help="alembic upgrade head").set_defaults(
        func=lambda _args: _db_upgrade()
    )

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9720)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_serve)

    worker = sub.add_parser("worker", help="run the Postgres job worker")
    worker.add_argument("--interval", type=float, default=1.0)
    worker.set_defaults(func=_worker)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "serve" and settings.env == "prod" and args.reload:
        print("refusing --reload in production", file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
