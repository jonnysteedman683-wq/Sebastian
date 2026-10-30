# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `b580b1bdfb3856edb730003a73a5ce3a3a65e507bac7426d38cb23834b5dd42f` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `6045bc2986040db1bce3e69e93d9579eebd32eae5afe0450bc721d80f74b1465` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
