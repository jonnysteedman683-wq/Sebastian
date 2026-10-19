# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `ac8123329d7a69ab1463234771a4a01d3298a965d5c1f647aa4720d7ef256766` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `9cde4dcf5e2463b2bcc09c35938b7a04fad82d0a08c72481096da4f55853db95` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
