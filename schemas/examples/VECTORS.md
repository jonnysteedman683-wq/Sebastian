# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `3edd0a753f524bc88eab173ca8f4f5725f0057bf37c0cdd58f8a2c6a06648087` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `41d5eced3a4cdf3dc9bfc5cc9d155470ca6f6c5f05388c2684438d2b3af7aecb` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
