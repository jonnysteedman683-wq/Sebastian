# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `f2a90714cd080f277ef6c549aa05500c38156175609ecbf6ab021e45c6789a9e` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `5a0474635d79daf703e77eebacf963794e031e921bd710378031b402e507ed6f` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
