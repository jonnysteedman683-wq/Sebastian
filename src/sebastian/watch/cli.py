"""``sebastian-watch``: helper CLI for GROK-WATCHER and CO-ORDINATOR-BOT.

    scan      facts for a commit range (watcher reads these to write its draft)
    finalize  merge draft + facts, enforce rules, validate, sign -> report JSON
    canary    build the weekly signed canary
    verify    coordinator check: schema, signature, seq, continuity

The signing key is read from an environment variable (default WATCHER_HMAC_KEY) and is never
printed. Exit code 0 = ok, 1 = rejected/invalid, 2 = usage or environment error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sebastian.watch.gitio import Git, GitError
from sebastian.watch.report import (
    SCHEMA_PATH,
    ReportError,
    canary,
    finalize,
    load_schema,
    scan,
    verify,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sebastian-watch", description=__doc__.splitlines()[0])
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="local clone of Sebastian")
    p.add_argument("--schema", type=Path, help=f"default: <repo>/{SCHEMA_PATH}")
    p.add_argument("--key-env", default="WATCHER_HMAC_KEY", help="env var holding the HMAC key")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("--from", dest="from_ref", help="last reported SHA (omit on first run)")
    s.add_argument("--to", dest="to_ref", default="origin/main")

    f = sub.add_parser("finalize")
    f.add_argument("--draft", type=Path, required=True, help="watcher-authored JSON")
    f.add_argument("--facts", type=Path, required=True, help="output of scan")
    f.add_argument("--seq", type=int, required=True)
    f.add_argument("--branch", default="main")

    c = sub.add_parser("canary")
    c.add_argument("--seq", type=int, required=True)
    c.add_argument("--ref", default="origin/main")

    v = sub.add_parser("verify")
    v.add_argument("report", type=Path)
    v.add_argument("--expect-seq", type=int)
    v.add_argument("--expect-from", help="to_sha of the last accepted report")
    return p


class ConfigError(Exception):
    pass


def _key(env_name: str) -> bytes:
    value = os.environ.get(env_name, "")
    if not value:
        raise ConfigError(f"signing key not set: export {env_name}")
    return value.encode()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _emit(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    schema_path = args.schema or args.repo / SCHEMA_PATH
    try:
        git = Git(args.repo)
        if args.cmd == "scan":
            _emit(scan(git, args.from_ref, args.to_ref))
            return 0
        schema = load_schema(schema_path)
        key = _key(args.key_env)
        if args.cmd == "finalize":
            _emit(
                finalize(
                    _read_json(args.draft),
                    _read_json(args.facts),
                    seq=args.seq,
                    key=key,
                    schema=schema,
                    branch=args.branch,
                )
            )
            return 0
        if args.cmd == "canary":
            _emit(canary(git, seq=args.seq, key=key, schema=schema, ref=args.ref))
            return 0
        problems = verify(
            _read_json(args.report),
            key=key,
            schema=schema,
            expected_seq=args.expect_seq,
            expected_from_sha=args.expect_from,
        )
        _emit({"accepted": not problems, "problems": problems})
        return 1 if problems else 0
    except ReportError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except (ConfigError, GitError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
