"""Re-sign schemas/examples/*.json with the published test key and update VECTORS.md.

Run after editing an example (e.g. a new tool in capability_inventory):

    python scripts/resign_examples.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
from pathlib import Path

TEST_KEY = b"TEST-VECTOR-KEY-NOT-A-SECRET"
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "schemas" / "examples"


def resign(directory: Path = DEFAULT_DIR) -> dict[str, str]:
    vectors = directory / "VECTORS.md"
    text = vectors.read_text(encoding="utf-8")
    signatures: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        body = {k: v for k, v in report.items() if k != "signature"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        sig = hmac.new(TEST_KEY, canonical.encode(), hashlib.sha256).hexdigest()
        report["signature"] = sig
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        row = re.compile(rf"(\| `{re.escape(path.name)}` \| `)[0-9a-f]{{64}}(` \|)")
        if not row.search(text):
            raise SystemExit(f"no row for {path.name} in {vectors}")
        text = row.sub(rf"\g<1>{sig}\g<2>", text)
        signatures[path.name] = sig
    vectors.write_text(text, encoding="utf-8")
    return signatures


if __name__ == "__main__":
    for name, sig in resign(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR).items():
        print(f"{name}: {sig}")
