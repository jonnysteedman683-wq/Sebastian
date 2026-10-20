# Backlog

The daily upgrade agent (Jules) takes the **first unchecked item not marked 🔒**, does only that, and ticks it in the same PR. Items are small on purpose: one item = one PR = one day.

Rules:
- 🔒 = needs the owner's sign-off or touches guarded paths. Skip it.
- Add new ideas at the bottom under **Proposed** with a one-line "done when". Never reorder or delete items above.
- If an item turns out bigger than one PR, split it here into smaller items and do the first.

## Queue

- [x] **P1-19 Per-endpoint API credentials** (upgrade spec addendum 2026-10-20; with P1-17's no-redirect bullet). `OPENAI_API_KEY` only to `api.openai.com`; other hosts need `#ENV_NAME` / `SEBASTIAN_API_KEY_ENV`; credential variables refused; redirects refused.
  *Done when:* a recording transport shows the OpenAI key only on `api.openai.com`, none on a third-party or localhost host unless named, and `#ANTHROPIC_API_KEY` is a `LeaderboardError`.
- [x] **P1-18 Sandbox and scrub the eval checkers** (upgrade spec addendum 2026-10-20). Checkers that run model-written code use an allowlisted env, bubblewrap when usable, process-group kill and bounded output; live runs refuse them without bubblewrap unless `--unsafe-unsandboxed-checkers`.
  *Done when:* fake secrets never reach `_py_ok`'s child; no background process survives; 50 MB of output stays bounded; with `bwrap_usable` false a live factory gets `checker sandbox unavailable` and no model code runs; offline suites still 16/16 and 18/18.
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
- [x] **B11 Gauntlet stages** (PROPOSALS #3). Design approved (PR #26): `docs/design/B11-gauntlet.md`. Open questions take the doc's proposed defaults.
  - [x] **B11a Stage machine + PREDICT.** `gauntlet.py`: `GauntletPolicy`, `Stage`, `next_stage`, strict `<checks>` parser, tool-free `predict`. Not wired in.
  - [x] **B11b VERIFY.** Run predicted checks via `run_command`; loop back on failure. *Done when:* a scripted run that breaks a test loops, fixes it and passes; a denied check fails the gate.
  - [x] **B11c CRITIQUE.** Separate tool-less critic (`critic_for`: policy, then planner, then actor model), strict verdict parser; runs only after VERIFY passed. The `--critic-model` flag moves to B11d with the rest of the CLI wiring.
  - [x] **B11d HELP + `needs_help` + `--gauntlet` + `--critic-model`.** *Done when:* `max_loops` exhaustion ends as `needs_help`; inventory examples re-signed.
  - [x] **B11e Evals.** Gauntlet on/off comparison and red-team attacks aimed at the critic.
- [x] **B12 Licence.** MIT (owner decision, 19 Oct 2026).

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
- [x] **B19 Sandbox by default.** Once `--sandbox bwrap` has run cleanly on the owner's hosts, make it the default when bubblewrap is present.
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
- [x] 🔒 **B21 Docstrings for `watch/`.** B7 excluded `src/sebastian/watch/` (guarded). Add docstrings there and drop its `D1` ignore in `pyproject.toml`.
  *Done when:* `ruff check .` passes with no `D1` ignore for `watch/`.

- [x] **B28 Resume.** Save each run's conversation; `sebastian resume <run_id>` continues a stopped run under a fresh budget.
  *Done when:* a budget-stopped run is finished by `resume` with history intact; completed, foreign-workspace and corrupt sessions are refused.

- [x] **B29 Sandboxed agent commands in evolve.** The agent's `run_command` calls during `evolve` share the gates' bubblewrap sandbox.
  *Done when:* with the sandbox on, an agent `pytest` call runs through the wrapper with the Python roots visible; with it off, it does not.

- [x] **B30 Red-team twin.** Attacker model generates canary-goal injection attacks; breaches become live scar cases.
  *Done when:* proposals are validated against the canary, a fooled target is recorded as a breach and saved as a scar that fails until the target resists.

- [x] **B31 Evolve resume.** A budget-stopped agent run inside `evolve` is resumed (up to `--resumes`, default 1) before gating.
  *Done when:* a cycle whose work needs a second budget is accepted with one resume and rejected with `--resumes 0`.

- [x] **B32 Dashboard.** Score history from live evals and red-team rounds; `sebastian dashboard` renders a static HTML page.
  *Done when:* the page shows trends, tiles, runs and scars from local files, escapes all values, and does not overflow at phone width.

- [x] **B33 Revert between Gauntlet loops** (design Q3). Optional `GauntletPolicy.revert_on_loop`: `Checkpoints.undo` before looping back so each attempt starts clean.
  *Done when:* with the flag, a failed attempt's writes are gone before the next ACT; off by default; tests cover both.
- [x] **B34 Gauntlet in evolve** (design Q5). `sebastian evolve --gauntlet` runs the inner agent under the Gauntlet; outer gates unchanged.
  *Done when:* a scripted cycle shows the inner run's VERIFY/CRITIQUE events and a `needs_help` inner run is rejected.

- [x] **B35 Generated critic attacks.** Extend `sebastian redteam` with a `--target critic` mode: the attacker writes reviewer-aimed payloads (fake approvals, "policy" lines) against a broken or tampered workspace; a false PASS that reaches `completed` is a breach saved as a scar.
  *Done when:* a scripted fooled critic produces a saved scar; a resisting one does not.
- [ ] 🔒 **B36 Gauntlet default on** (design Q4). Flip `--gauntlet` to the default only after a live `--compare` shows a higher score with no lower injection resistance. Owner decision.

- [x] **B37 OpenAI-compatible backend.** Stdlib-only Chat Completions client; `SEBASTIAN_BACKEND=openai`, `SEBASTIAN_BASE_URL`.
  *Done when:* request/response wire format, tool calls, retries and URL policy are tested with a fake transport; a CLI run uses the configured backend.

- [x] **B42 Run timeline.** `sebastian show <run_id> [-o file] [--text]`: one run's trace as an HTML or text timeline, including every Gauntlet stage.
  *Done when:* every trace event kind renders, untrusted text is escaped, prefix lookup and errors are tested.
- [x] **B41 Dashboard: Gauntlet and critic views.** Plain vs Gauntlet score and agent vs critic resistance on shared charts, Gauntlet/critic tiles, daily needs-help trend.
  *Done when:* multi-series charts, latest-model trend filtering and the needs-help series are tested.
- [x] **B40 Cost tracking.** Owner-kept `prices.json`, `Usage.cost`, `Budget.max_cost` / `--max-cost`, cost in traces and the dashboard.
  *Done when:* a priced run stops at its dollar limit; an unpriced model with a dollar limit is refused; traces and dashboard show spend.
- [x] **B39 Model leaderboard.** `sebastian models [backend:]model[@base_url] ...` runs the eval suite per model and ranks them.
  *Done when:* ranking, spec parsing, Gauntlet columns, limits and history records are tested with scripted models.
- [x] **B38 Harder eval cases.** Six realistic capability cases: fix a failing test, rename across files, find a bug with no hint, edit JSON, summarise CSV, admit an impossible task.
  *Done when:* all pass offline; a do-nothing model fails each; behaviour is checked by running code, not by matching text.
- [x] **B43 No stale bytecode in agent commands.** `run_command` should set `PYTHONDONTWRITEBYTECODE=1` (or a per-run `PYTHONPYCACHEPREFIX`) so a same-size edit within one second never runs a stale `.pyc`. Found while building B38.
  *Done when:* a scripted run that edits then re-runs a module sees the new behaviour.

- [x] 🔒 **P1-01 `--sandbox auto` fails closed** (upgrade spec 2026-10-19). No silent fallback to unsandboxed `run_command`; `--sandbox none` warns, is traced and cuts the network with `unshare` unless `--allow-network`.
  *Done when:* `resolve_command_sandbox("auto")` raises without bwrap and the CLI exits 2; `--sandbox none` warns every run and traces `sandbox="none"`; without unshare it needs `--allow-network`; SECURITY residuals 1-2 updated.

- [x] 🔒 **P1-02 Evolve requires bwrap and scrubs the gate env** (upgrade spec 2026-10-19). No unsandboxed agent-written code without `--unsafe-no-sandbox`; gate commands never inherit host secrets.
  *Done when:* `evolve --sandbox auto` without bwrap ends with `EvolveError`; `--sandbox none` needs `--unsafe-no-sandbox`; a test with fake `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`GH_TOKEN` shows a gate command's env lacks them; report and SECURITY say "no secrets in gate env".
- [x] **B44 No CHANGELOG conflicts between parallel PRs.** `.gitattributes` sets `CHANGELOG.md merge=union`.
  *Done when:* a test merges two branches that each add an Unreleased line and gets both lines with no conflict.

- [x] 🔒 **P1-03 Guard the safety TCB** (upgrade spec 2026-10-19). Approval, agent, budget, evolve, Gauntlet, checkpoints, sessions, traces, tools, LLM backends, evals, packaging and scripts are guarded; evolve can't delete or rename tests; CODEOWNERS.
  *Done when:* guard.yml and `DEFAULT_GUARDED` include the TCB and match; ScopedApprover denies `src/sebastian/approval.py` and `pyproject.toml`; a cycle that deletes or renames a test is rejected; `.github/CODEOWNERS` lists the same paths.

<!-- Agents add new items here: "- [ ] **Bn title.** what. *Done when:* check." -->

- [x] **B20 Un-redact `llm_step` token counts** (was a second B13; renumbered). Trace redaction matches any key containing "token", so `llm_step`'s `tokens=` count is always written as `[REDACTED]`. Rename it to `usage={"input", "output"}` like `run_end` (no change to the redaction rules).
  *Done when:* a `JsonlTracer` test shows the per-step counts in the file; redaction tests unchanged.
