# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `803c1777ef84f471e39e44390a961d720744827b642fa57949e98ebea6bfb85a` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `5f50e52a0976bb58f9dec3f71f619cf45dd579564dd1c8efd573b675de3a6d0e` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
