# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `0965a1d08e818df5b823eb004b0f8a4880a96cc0f15ac7b3b43c8b0dcbbca594` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `e1ba60f076bd8a164ff43f95e4777ab55341be20241d96b21afcccf037c234fe` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
