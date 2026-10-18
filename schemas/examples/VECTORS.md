# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `599a06c0808d2921cf8a442ecd97f8b782a1850f0f0a4b6ad68d0a9a7c7e7e36` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `7b2fa05fb37465af0e1df969ce59cd550f91248ab8437298ccbbf6c7673afa3d` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
