# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `ab3beffabbe2972c24c129bae302162be472a64a3f28e158dfd68efc89f0d3d9` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `d023a8db8a9064ff64149f8164ad38767b9b58d6274fedc706f995dcbadfd253` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
