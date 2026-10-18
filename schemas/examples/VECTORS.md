# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `65b312fd5de8eb6c8a183ee417615ec85ab3bf65778fad1e9f84a8ea02def2da` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `e75b81b52b0f4860ed26e5c022566cd0ee8aeaaf4320507aee98c264d90e41d5` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
