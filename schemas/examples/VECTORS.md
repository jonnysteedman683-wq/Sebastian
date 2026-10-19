# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `ad61949d2287309521b4f17a0da7f41f43faa222a9ddcc6bff39a86fc7036bd4` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `d241a45aca11dde5d9975bc3a015eb50362e3465d54c1da58cc6ccab28633fbb` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
