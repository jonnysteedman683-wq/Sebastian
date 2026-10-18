"""``sebastian-watch``: helper CLI for GROK-WATCHER and CO-ORDINATOR-BOT.

    scan      facts for a commit range (watcher reads these to write its draft)
    finalize  merge draft + facts, enforce rules, validate, sign -> report JSON
    canary    build the weekly signed canary
    verify    coordinator check: schema, signature, seq, continuity
    run       full unattended run (scan, draft, finalize, deliver, state) for CI schedules

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
from sebastian.watch.runner import Endpoints, State, StateError, run


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

    r = sub.add_parser("run")
    r.add_argument("--state", type=Path, required=True, help="watch state JSON (created if absent)")
    r.add_argument("--mode", choices=["change", "digest", "canary"], default="change")
    r.add_argument("--ref", default="origin/main")
    r.add_argument("--baseline", help="start SHA when no state exists yet")
    r.add_argument("--out", type=Path, help="also write the report here")

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


def _run(args: argparse.Namespace, git: Git, schema: dict[str, Any]) -> int:
    key = os.environ.get(args.key_env, "").encode() or None
    url = os.environ.get("COORDINATOR_URL", "")
    endpoints = (
        Endpoints(
            coordinator_url=url,
            coordinator_token=os.environ.get("COORDINATOR_TOKEN", ""),
            grok_url=os.environ.get("GROK_DRAFT_URL", ""),
            grok_token=os.environ.get("GROK_DRAFT_TOKEN", ""),
        )
        if url
        else None
    )
    state = State.load(args.state)
    result = run(
        git,
        state,
        args.mode,
        key=key,
        schema=schema,
        endpoints=endpoints,
        ref=args.ref,
        baseline=args.baseline,
    )
    if result.action in ("sent", "failed"):
        result.state.save(args.state)
    if args.out and result.report is not None:
        args.out.write_text(json.dumps(result.report, indent=2, ensure_ascii=False) + "\n")
    report = result.report or {}
    _emit(
        {
            "action": result.action,
            "detail": result.detail,
            "notes": result.notes,
            "seq": report.get("seq"),
            "to_sha": (report.get("range") or {}).get("to_sha"),
            "priority": report.get("priority"),
            "escalate": report.get("escalate"),
        }
    )
    return 1 if result.action == "failed" else 0


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911 - one exit per command
    args = _parser().parse_args(argv)
    schema_path = args.schema or args.repo / SCHEMA_PATH
    try:
        git = Git(args.repo)
        if args.cmd == "scan":
            _emit(scan(git, args.from_ref, args.to_ref))
            return 0
        schema = load_schema(schema_path)
        if args.cmd == "run":
            return _run(args, git, schema)
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
    except (ConfigError, GitError, StateError, ValueError, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
