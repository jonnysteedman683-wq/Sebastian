# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `e920e8f76c98b03372bc616c7ee6de45a47159269ba89a5909f909de7c80b385` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `c93ff9b879abb2c437f7cfd67ee404424ca52403e1e420c2bc7362fd30d98a6f` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
