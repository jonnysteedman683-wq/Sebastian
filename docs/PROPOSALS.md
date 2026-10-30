# Proposals (out of scope for v0.1.0)

Ordered by value. Each needs owner sign-off before implementation (WORK_ORDER section 5).

1. **Container/namespace sandbox for `run_command`** (bubblewrap/nsjail, no network). Closes SECURITY residual risks 1-2.
2. **Eval harness (U12)** with a fixed task suite plus hostile prompt-injection cases, scored in CI.
3. **Gauntlet integration (U1/U2/U4)**: plan -> predict -> act -> test -> reflect stages, separate critic model, `pytest` command gates before LLM judgement.
4. **Embedding memory (U5)** with decay and dedup, replacing keyword recall.
5. **Trace viewer / replay (U9)** over `traces.jsonl`.
6. **Live API smoke test** in CI behind a repository secret.
7. **Licence**: done, MIT (B12).
8. **Optimize membership check in _arguments parsing**: Replace the `tuple` literal `(None, "")` with a `set` literal `{None, ""}` in `src/sebastian/llm/openai_compat.py:292` to take advantage of O(1) frozen set lookup performance.
