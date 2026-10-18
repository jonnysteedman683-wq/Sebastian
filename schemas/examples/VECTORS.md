# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `bd930c094e9acf1306d9eba0ba56cd5d83ea30579b371082dfe3b1d2fb316dbe` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `5aa948540e78aec908b12c422efcf264d98144e86d5808e6ed87f9c5b7fe0705` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
