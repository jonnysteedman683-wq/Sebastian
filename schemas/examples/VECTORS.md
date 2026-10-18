# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `edbadecf5f95b89c2e159564c5c1afe61313f92d819bfdcfe4e4e503b5ca9726` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `575942ca72bae75319f8e944851febc246f7cf4af4d79266e794ddea3be99e9c` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
