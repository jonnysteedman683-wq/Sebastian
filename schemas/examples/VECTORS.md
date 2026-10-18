# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `75b38c09ca136e235609b072e35aec81daf6050b0d0603f54c1572ecd0427b5d` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `9edbe12ee86d7a3d1281adfcbfd1aa4cfade6d3dbccd548d90ba525bbeb3afea` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
