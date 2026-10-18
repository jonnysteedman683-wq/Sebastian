# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `f4c195c4aa65b7605027ee5ef726aa3c4008579ba9e1664647a3953803b26355` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `e5ca53d6e44e90f9bb6504a0fe7269c5753faf366fc5197b6d36d2c7a6ceba12` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
