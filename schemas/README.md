# Schemas

## `repo_change_report.schema.json`

The contract between **GROK-WATCHER** (producer) and **CO-ORDINATOR-BOT** (consumer). JSON Schema draft 2020-12.

- The watcher validates every report against it **before sending**.
- The coordinator validates every report **on receipt** and rejects anything invalid.
- Work orders: [`docs/watch/GROK_WATCHER_WORK_ORDER.md`](../docs/watch/GROK_WATCHER_WORK_ORDER.md), [`docs/watch/COORDINATOR_WATCHER_WORK_ORDER.md`](../docs/watch/COORDINATOR_WATCHER_WORK_ORDER.md).
- Contract tests: `tests/test_report_schema.py` (run in CI).

### Rules the schema enforces

| Rule | Why |
|---|---|
| Every change has at least one evidence item (SHA + repo-relative path) | No citation, no report |
| `canary` reports are HIGH, escalated, and carry no changes, alerts or previews | Test alerts can't smuggle real content |
| `risk_score >= 6` requires `escalate: true` | Watcher §8.4 |
| Any HIGH security alert requires `priority: HIGH` | Can't under-report a security issue |
| `escalate: true` requires at least one reason | Coordinator needs to tell Jonny why |
| Unknown fields rejected; bot names and repo are fixed constants | Catches drift and misrouted messages |

### Signing

```
canonical = UTF-8 bytes of JSON(report without "signature",
                                 keys sorted at every level,
                                 separators "," and ":", no extra whitespace,
                                 non-ASCII characters kept as-is, not \u-escaped)
signature = lowercase hex( HMAC-SHA256(key = WATCHER_HMAC_KEY, msg = canonical) )
```

Python reference:

```python
json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

Compare signatures in constant time.

### Test vectors

The examples in `examples/` are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `examples/report.json` | `0965a1d08e818df5b823eb004b0f8a4880a96cc0f15ac7b3b43c8b0dcbbca594` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `examples/canary.json` | `e1ba60f076bd8a164ff43f95e4777ab55341be20241d96b21afcccf037c234fe` |

### Changing the schema

1. Open a PR. Never edit on `main`.
2. Update both work orders in `docs/watch/` in the same PR.
3. Regenerate and re-sign the examples; update the test vectors above.
4. Additive, optional fields are non-breaking. Anything else is breaking: bump the schema `$id` to `..._v2.schema.json` and run both versions in parallel until both bots have switched.
