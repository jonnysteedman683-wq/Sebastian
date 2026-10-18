# Schemas

## `repo_change_report.schema.json`

The contract between **GROK-WATCHER** (producer) and **CO-ORDINATOR-BOT** (consumer). JSON Schema draft 2020-12.

- The watcher validates every report against it **before sending**.
- The coordinator validates every report **on receipt** and rejects anything invalid.
- Work orders: [`docs/watch/GROK_WATCHER_WORK_ORDER.md`](../docs/watch/GROK_WATCHER_WORK_ORDER.md), [`docs/watch/COORDINATOR_WATCHER_WORK_ORDER.md`](../docs/watch/COORDINATOR_WATCHER_WORK_ORDER.md).
- Contract tests: `tests/test_report_schema.py` (run in CI).
- Tooling: `sebastian-watch finalize` builds valid signed reports; `sebastian-watch verify` checks them. Prefer these over hand-rolled signing.

### Rules the schema enforces

| Rule | Why |
|---|---|
| Every change has at least one evidence item (SHA + repo-relative path) | No citation, no report |
| `canary` reports are HIGH, escalated, and carry no changes, alerts or previews | Test alerts can't smuggle real content |
| `risk_score >= 6` requires `escalate: true` | Watcher §8.4 |
| Any HIGH security alert requires `priority: HIGH` | Can't under-report a security issue |
| `escalate: true` requires at least one reason | Coordinator needs to tell Jonny why |
| `escalate: true` requires `priority: HIGH` | Escalation and priority never disagree |
| Unknown fields rejected; bot names and repo are fixed constants | Catches drift and misrouted messages |

### Signing

`signature` is always 64 lowercase hex characters over the same canonical bytes:

```
canonical = UTF-8 bytes of JSON(report without "signature",
                                 keys sorted at every level,
                                 separators "," and ":", no extra whitespace,
                                 non-ASCII characters kept as-is, not \u-escaped)
```

| Mode | `signature` | Proves | When |
|---|---|---|---|
| **Keyless** (default) | `hex(SHA-256(canonical))` | Integrity: the report wasn't altered after it was built | Reports read from the `watch-state` branch; GitHub vouches for who wrote them |
| **Keyed** (optional) | `hex(HMAC-SHA256(WATCHER_HMAC_KEY, canonical))` | Integrity **and** sender | Reports pushed to a coordinator endpoint |

Python reference:

```python
json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

Compare signatures in constant time. `sebastian-watch verify` uses keyed mode when `WATCHER_HMAC_KEY` is set and keyless otherwise.

### Test vectors

Published in [`examples/VECTORS.md`](examples/VECTORS.md) (kept next to the examples so updating an example and its vector is one unguarded change).

### Changing the schema

1. Open a PR. Never edit on `main`.
2. Update both work orders in `docs/watch/` in the same PR.
3. Regenerate and re-sign the examples; update `examples/VECTORS.md`.
4. Additive, optional fields are non-breaking. Anything else is breaking: bump the schema `$id` to `..._v2.schema.json` and run both versions in parallel until both bots have switched.
