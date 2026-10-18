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
- [x] **B6 Memory dedup.** In `MemoryStore.record`, replace an episode with the same task text instead of appending a duplicate.
  *Done when:* tests show one episode after recording the same task twice, with the newest outcome kept.
- [x] **B7 Docs coverage.** Every public function and class in `src/sebastian/` has a docstring; add ruff rule `D1` for `src/` only.
  *Done when:* `ruff check .` passes with `D1` enabled for `src/`.
- [x] **B8 Eval harness skeleton (U12).** `evals/` with 5 scripted tasks run through `ScriptedLLM`, a scorer, and a `pytest` marker `eval`; no network.
  *Done when:* `pytest -m eval` runs and reports a score; CI unchanged.
- [x] **B9 Prompt-injection evals.** Add 5 hostile cases to the eval harness (instructions hidden in file contents and tool output) asserting no gated tool is called.
  *Done when:* all 5 pass with the current agent; failures documented in `docs/SECURITY.md` if any.
- [x] 🔒 **B10 Container sandbox for `run_command`** (PROPOSALS #1). Owner chose bubblewrap; opt-in via `--sandbox bwrap`.
- [ ] 🔒 **B11 Gauntlet stages** (PROPOSALS #3). Needs an owner design review.
- [ ] 🔒 **B12 Licence.** Owner decision.

## Proposed

- [x] **B13 Context compaction.** Bound the history sent to the model: cap tool results, elide old ones oldest-first.
  *Done when:* a 12-step run over a large file never sends more than `max_history_chars`; pairing preserved.

- [x] **B14 Planning phase + replan nudge.** Tool-free planning call feeding `<plan>` into the system prompt; nudge after N consecutive tool failures.
  *Done when:* tests show the plan call has no tools, counts as a step and reaches the acting prompt; one nudge after 3 failures.

- [x] **B15 BM25 recall.** Replace keyword-overlap recall with BM25 (no dependencies).
  *Done when:* rare terms outrank common ones; existing recall tests pass unchanged.

- [x] **B16 Parallel read tools.** Run a turn's READ-only tool calls concurrently, preserving result order.
  *Done when:* a 3-way barrier test proves concurrency; a turn with a WRITE stays in order.

- [x] **B17 Model routing.** Separate planner and escalation models (e.g. `claude-opus-5-5`) over a cheaper main model.
  *Done when:* tests show the planner gets only the plan call and the escalation model only the step after a nudge.

- [x] 🔒 **B18 Live eval workflow** (PROPOSALS #6). Weekly + manual live evals behind the `ANTHROPIC_API_KEY` secret.
- [ ] **B19 Sandbox by default.** Once `--sandbox bwrap` has run cleanly on the owner's hosts, make it the default when bubblewrap is present.
  *Done when:* default policy uses bwrap if usable, `--sandbox none` opts out; tests cover both.

- [x] **B22 `edit_file` tool.** Exact-text replace in an existing file (WRITE).
  *Done when:* tests cover unique, ambiguous, replace_all, not found, empty, oversize and jail escape.
- [x] **B23 Undo for writes.** Checkpoint files before a run changes them; `sebastian undo <run_id>`.
  *Done when:* tests cover restore, delete-created, skip-if-changed, `--force`, wrong workspace, corrupt manifest and tampered blob.
- [x] **B24 Scar tissue.** `sebastian scar <run_id>` turns a failed run into a live eval case in `evals/scars/`.
  *Done when:* a failed run produces a scar seeded with the pre-run workspace; the live suite replays it; offline lists it as skipped.
- [x] **B25 Self-improvement loop (Ouroboros).** Sebastian works a backlog item in a sandbox branch and opens a PR only if tests pass and the eval score is at least `main`'s.
  *Done when:* a scripted end-to-end test shows a PR opened on a non-regressing change and refused on a regressing one.
- [x] **B27 Sandboxed evolve gates.** Run `evolve`'s check and eval commands under bubblewrap (no network, worktree-only) when available.
  *Done when:* gate commands run via the bwrap wrapper when usable; tests cover both paths.
- [ ] **B26 Trust ladder.** Autonomy level (L0 suggest .. L3 auto-merge code) derived from the eval and PR record; a failed eval drops a level. Auto-merge levels need owner sign-off.
- [ ] 🔒 **B21 Docstrings for `watch/`.** B7 excluded `src/sebastian/watch/` (guarded). Add docstrings there and drop its `D1` ignore in `pyproject.toml`.
  *Done when:* `ruff check .` passes with no `D1` ignore for `watch/`.

- [x] **B28 Resume.** Save each run's conversation; `sebastian resume <run_id>` continues a stopped run under a fresh budget.
  *Done when:* a budget-stopped run is finished by `resume` with history intact; completed, foreign-workspace and corrupt sessions are refused.

- [x] **B29 Sandboxed agent commands in evolve.** The agent's `run_command` calls during `evolve` share the gates' bubblewrap sandbox.
  *Done when:* with the sandbox on, an agent `pytest` call runs through the wrapper with the Python roots visible; with it off, it does not.

- [x] **B30 Red-team twin.** Attacker model generates canary-goal injection attacks; breaches become live scar cases.
  *Done when:* proposals are validated against the canary, a fooled target is recorded as a breach and saved as a scar that fails until the target resists.

- [x] **B31 Evolve resume.** A budget-stopped agent run inside `evolve` is resumed (up to `--resumes`, default 1) before gating.
  *Done when:* a cycle whose work needs a second budget is accepted with one resume and rejected with `--resumes 0`.

<!-- Agents add new items here: "- [ ] **Bn title.** what. *Done when:* check." -->

- [x] **B20 Un-redact `llm_step` token counts** (was a second B13; renumbered). Trace redaction matches any key containing "token", so `llm_step`'s `tokens=` count is always written as `[REDACTED]`. Rename it to `usage={"input", "output"}` like `run_end` (no change to the redaction rules).
  *Done when:* a `JsonlTracer` test shows the per-step counts in the file; redaction tests unchanged.
