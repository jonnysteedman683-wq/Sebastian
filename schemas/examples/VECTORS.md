# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `5b423308bc7cd05a11751e462cfdd35153d083f53a5187f6e3f47d2b241c038a` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `09243adc426881cf737912b2ae8762377d1262f0c1bb38c68eb920baebc6f938` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
