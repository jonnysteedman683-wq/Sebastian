# Backlog

The daily upgrade agent (Jules) takes the **first unchecked item not marked 🔒**, does only that, and ticks it in the same PR. Items are small on purpose: one item = one PR = one day.

Rules:
- 🔒 = needs the owner's sign-off or touches guarded paths. Skip it.
- Add new ideas at the bottom under **Proposed** with a one-line "done when". Never reorder or delete items above.
- If an item turns out bigger than one PR, split it here into smaller items and do the first.

## Queue

- [ ] **B1 Richer mechanical report write-ups.** In `src/sebastian/watch/runner.py::mechanical_draft`, when no capability changed, emit one change per commit (title = commit subject, evidence = that commit's SHA + first changed file) instead of a single "N file(s) changed".
  *Done when:* a test with 2 commits produces 2 changes with the right SHAs; existing tests pass. 🔒 guarded path (`src/sebastian/watch/`); needs `owner-approved`.
- [x] **B2 `read_file` line ranges.** Add optional `start_line: int = 0` and `max_lines: int = 0` (0 = all) to `read_file` in `tools/builtin.py`.
  *Done when:* tests cover first/middle/last ranges and out-of-range; schema example in `schemas/examples/` re-checked (`tests/test_report_schema.py` passes).
- [x] **B3 `search_files` tool (READ).** Substring search across workspace files, returning `path:line: text`, capped at 200 matches, skipping files over `MAX_READ_BYTES` and binary files.
  *Done when:* tests cover match, no match, cap, binary skip, jail escape. Update `schemas/examples/` inventory and re-sign per `schemas/README.md`.
- [x] **B4 Token budget warning.** When a run passes 80% of any budget limit, emit a `budget_warning` trace event once per limit.
  *Done when:* tests with `FakeClock` and `ScriptedLLM` show exactly one warning per limit.
- [x] **B5 `sebastian traces` CLI command.** Print the last N runs from `traces.jsonl` (run_id, start time, stop reason, steps, tokens).
  *Done when:* tests cover empty file, N > runs, malformed line skipped. Update `schemas/examples/` (new CLI command) and re-sign.
- [ ] **B6 Memory dedup.** In `MemoryStore.record`, replace an episode with the same task text instead of appending a duplicate.
  *Done when:* tests show one episode after recording the same task twice, with the newest outcome kept.
- [ ] **B7 Docs coverage.** Every public function and class in `src/sebastian/` has a docstring; add ruff rule `D1` for `src/` only.
  *Done when:* `ruff check .` passes with `D1` enabled for `src/`.
- [x] **B8 Eval harness skeleton (U12).** `evals/` with 5 scripted tasks run through `ScriptedLLM`, a scorer, and a `pytest` marker `eval`; no network.
  *Done when:* `pytest -m eval` runs and reports a score; CI unchanged.
- [x] **B9 Prompt-injection evals.** Add 5 hostile cases to the eval harness (instructions hidden in file contents and tool output) asserting no gated tool is called.
  *Done when:* all 5 pass with the current agent; failures documented in `docs/SECURITY.md` if any.
- [ ] 🔒 **B10 Container sandbox for `run_command`** (PROPOSALS #1). Needs an owner decision on bubblewrap vs nsjail.
- [ ] 🔒 **B11 Gauntlet stages** (PROPOSALS #3). Needs an owner design review.
- [ ] 🔒 **B12 Licence.** Owner decision.

## Proposed

- [x] **B13 Context compaction.** Bound the history sent to the model: cap tool results, elide old ones oldest-first.
  *Done when:* a 12-step run over a large file never sends more than `max_history_chars`; pairing preserved.

<!-- Agents add new items here: "- [ ] **Bn title.** what. *Done when:* check." -->

- [ ] **B13 Un-redact `llm_step` token counts.** Trace redaction matches any key containing "token", so `llm_step`'s `tokens=` count is always written as `[REDACTED]`. Rename it to `usage={"input", "output"}` like `run_end` (no change to the redaction rules).
  *Done when:* a `JsonlTracer` test shows the per-step counts in the file; redaction tests unchanged.
