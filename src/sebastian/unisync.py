"""Report Sebastian's runs to the shared UniSync ledger (UNISYNCARC on Supabase).

UniSync is the joint protocol all of Jonny's agents share. Its reference implementation and spec
live in LESTINXOR-AGENT (``lestinxor/unisync.py``, ``docs/UNISYNC.md``). Sebastian has no runtime
dependencies, so this is a small stdlib client that writes envelopes with the same canonical
digest. It can only post kinds that are valid for any registered agent at any time (heartbeat,
memory.write, escalate), so nothing it writes can break the ledger's task rules for other readers.

Opt-in and best-effort: it is used only when ``UNISYNC_SUPABASE_URL`` and ``UNISYNC_SUPABASE_KEY``
are set, it sends run metadata only (never the task text, the answer or file contents), and a
failed report never changes a command's exit code.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sebastian.clock import Clock, SystemClock

PROTOCOL_VERSION = "unisync/1"
GENESIS = "0" * 64
SENDER = "sebastian"
TABLE = "unisync_events"
REQUIRED: Mapping[str, tuple[str, ...]] = {
    "heartbeat": (),
    "memory.write": ("key", "value"),
    "escalate": ("reason",),
}

Opener = Callable[..., Any]


class UnisyncError(RuntimeError):
    """A report could not be written or the shared head failed verification."""


def envelope_digest(
    seq: int, sender: str, kind: str, task_id: str, body: Mapping[str, Any], prev: str
) -> str:
    """SHA-256 of the canonical envelope; byte-identical to Lestinxor's ``envelope_digest``."""
    payload = {
        "v": PROTOCOL_VERSION,
        "seq": seq,
        "sender": sender,
        "kind": kind,
        "task_id": task_id,
    }
    payload.update({"body": dict(body), "prev": prev})
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Posted:
    """One envelope this client wrote."""

    seq: int
    kind: str
    digest: str


class Reporter:
    """Writes Sebastian's envelopes at the shared head; retries when another agent wins the race."""

    def __init__(
        self,
        url: str,
        key: str,
        clock: Clock | None = None,
        opener: Opener | None = None,
        timeout: float = 15.0,
        retries: int = 5,
    ) -> None:
        """Point at ``https://<ref>.supabase.co`` with a key allowed to insert into the ledger."""
        if not url.startswith("https://"):
            raise UnisyncError("UNISYNC_SUPABASE_URL must be an https:// URL")
        self.endpoint = url.rstrip("/") + "/rest/v1/" + TABLE
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        self.clock: Clock = clock or SystemClock()
        self.opener: Opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.retries = retries

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw: Any) -> Reporter | None:
        """A reporter from ``UNISYNC_SUPABASE_URL`` and ``_KEY``; None if either is unset."""
        env = os.environ if env is None else env
        url, key = env.get("UNISYNC_SUPABASE_URL", ""), env.get("UNISYNC_SUPABASE_KEY", "")
        return cls(url, key, **kw) if url and key else None

    def _request(
        self, method: str, url: str, payload: Any = None, extra: Mapping[str, str] | None = None
    ) -> Any:
        data = None if payload is None else json.dumps(payload, default=str).encode()
        headers = {**self.headers, **(extra or {})}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 - https enforced
        with self.opener(req, timeout=self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def head(self) -> tuple[int, str]:
        """``(next seq, prev digest)`` at the shared head, after re-checking the head's digest."""
        query = urllib.parse.urlencode(
            {
                "select": "v,seq,sender,kind,task_id,body,prev,digest",
                "order": "seq.desc",
                "limit": "1",
            }
        )
        try:
            rows = self._request("GET", f"{self.endpoint}?{query}") or []
        except urllib.error.HTTPError as exc:
            raise UnisyncError(f"ledger read failed: HTTP {exc.code}") from exc
        if not rows:
            return 0, GENESIS
        row = rows[0]
        expect = envelope_digest(
            row["seq"], row["sender"], row["kind"], row["task_id"], row["body"], row["prev"]
        )
        if row.get("v") != PROTOCOL_VERSION or row["digest"] != expect:
            raise UnisyncError(
                f"ledger head at seq {row['seq']} fails verification; not writing on top of it"
            )
        return int(row["seq"]) + 1, str(row["digest"])

    def post(self, kind: str, **body: Any) -> Posted:
        """Append one envelope. Only heartbeat, memory.write and escalate are allowed from here."""
        if kind not in REQUIRED:
            raise UnisyncError(f"sebastian's reporter may not post {kind!r}")
        missing = [k for k in REQUIRED[kind] if k not in body]
        if missing:
            raise UnisyncError(f"{kind} missing body field(s): {', '.join(missing)}")
        for _ in range(self.retries):
            seq, prev = self.head()
            digest = envelope_digest(seq, SENDER, kind, "", body, prev)
            row = {
                "v": PROTOCOL_VERSION, "seq": seq, "sender": SENDER, "kind": kind, "task_id": "",
                "body": body, "prev": prev, "digest": digest, "ts": self.clock.now(),
            }  # fmt: skip
            try:
                self._request("POST", self.endpoint, row, {"Prefer": "return=minimal"})
            except urllib.error.HTTPError as exc:
                if exc.code == 409:  # another agent wrote at this seq first; re-read the head
                    continue
                raise UnisyncError(f"ledger write failed: HTTP {exc.code}") from exc
            return Posted(seq, kind, digest)
        raise UnisyncError(f"gave up after {self.retries} concurrent-write conflicts")


def run_summary(command: str, outcome: str, **fields: Any) -> dict[str, Any]:
    """The metadata-only ``memory.write`` body for one finished command."""
    value = {
        "command": command,
        "outcome": outcome,
        **{k: v for k, v in fields.items() if v not in (None, "")},
    }
    return {"key": "last_run", "scope": SENDER, "value": value}


def report(
    body: Mapping[str, Any], reporter: Reporter | None = None, warn: Callable[[str], None] = print
) -> bool:
    """Best-effort ``memory.write`` of ``body``. True if written; never raises."""
    try:
        reporter = reporter if reporter is not None else Reporter.from_env()
        if reporter is None:
            return False
        reporter.post("memory.write", **body)
    except (UnisyncError, OSError, ValueError) as exc:
        warn(f"unisync: report not written ({exc})")
        return False
    return True
