# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `835f5f1b11dffed709dce7915c2f6285220ffa4eb0bb0b398efdf17a013e78c2` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `ae4fadd822fd54c3c7717f49300037cc7e45646a700c7e008cea9fa310aa317c` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
