# Test vectors

The examples in this folder are signed with the published test key below. Use them to check your implementation produces identical bytes. **Never use this key in production.**

| Key | File | Expected signature |
|---|---|---|
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `report.json` | `639f21211ffea588be8370ea2a56a9343ffa85ab158000ea2439dfee29a9bc8a` |
| `TEST-VECTOR-KEY-NOT-A-SECRET` | `canary.json` | `dcfbab98bb34b2a9a72a10fb4a3adc5727deb1115b1c5662093c8c65fd4848d4` |

Updating an example (e.g. after adding a tool or CLI command, so `capability_inventory` matches what Sebastian ships): edit the JSON, re-sign it with the key above using the canonical form in [`../README.md`](../README.md#signing), and update the signature here. `tests/test_report_schema.py` checks all three stay in step.
