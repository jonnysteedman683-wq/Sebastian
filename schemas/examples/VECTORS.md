# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `629b1b8091346f96c71d6c0dd881d587a122c6bd447d6ed87adfe90648b2b4f1` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `0538159f36c8879c7066e56f1f4d7d2ccafc089d58a84b2e54884c4baff6f409` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
