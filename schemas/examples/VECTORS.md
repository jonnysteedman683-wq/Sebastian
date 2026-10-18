# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `19c2c15bd0b82c3bc6b4186f361740d1a34adf8ae2ac89b060d7de16d918b93c` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `571490abedc0a02c863b3fd307083dc2707b786508b374ed67f5a7c1c2b25a8f` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
