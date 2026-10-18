# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `d2a663239e9b46a4eaa66d9f5512d573ee083801a8aa29d28d3da42391a2c15e` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `0e37cb3acd19b71c9279c4a650b13d0d1120129b8e476ff9815425f520b78bba` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
