# Backlog

The daily upgrade agent (Jules) takes the **first unchecked item not marked 🔒**, does only that, and ticks it in the same PR. Items are small on purpose: one item = one PR = one day.

Rules:
- 🔒 = needs the owner's sign-off or touches guarded paths. Skip it.
- Add new ideas at the bottom under **Proposed** with a one-line "done when". Never reorder or delete items above.
- If an item turns out bigger than one PR, split it here into smaller items and do the first.

## Queue

- [x] **P3-12 Leaderboard metrics** (upgrade spec addendum 2026-10-20). `--repeats N` with mean and range plus overlap ties; cost, median s/case and completed/budget/error columns.
  *Done when:* a flaky scripted model with `--repeats 3` shows its range and the tie logic works; an error-heavy model shows its error count; rendering stays fixed-width and is tested.
- [x] **P2-14 Leaderboard history, dashboard and identity hygiene** (upgrade spec addendum 2026-10-20). Own history kinds + run id; dashboard leaderboard table; host:port in labels; duplicate check on (backend, model, base_url).
  *Done when:* after a 3-model leaderboard the Eval tile still shows the last `evals --live`; two localhost ports rank together with distinct labels; old history loads.
- [x] **P2-15 Don't let missing usage win the leaderboard** (upgrade spec addendum 2026-10-20). `usage_reported` per case; `?` in the tokens column; ranks last on the token tie-break.
  *Done when:* two equal-score scripted models, one reporting usage and one not: the honest one ranks first and the other shows `?`.
- [x] **P2-13 Real clock and whole-run ceilings for live evals and the leaderboard** (upgrade spec addendum 2026-10-20). SystemClock for live cases plus a per-case time limit; worst-case estimate with `--yes` over 5M tokens; `--max-total-tokens`, `--max-seconds`, `--max-cost-usd`.
  *Done when:* a fake slow live model ends as `budget` (time); scripted runs unchanged; `--max-total-tokens 1000` stops after the crossing model with a `partial` row; an over-threshold estimate without `--yes` exits 2 before any call.
- [x] **P2-12 Leaderboard preflight and failure isolation** (upgrade spec addendum 2026-10-20). Preflight before any case; per-model error rows; incremental recording; Ctrl-C keeps finished models.
  *Done when:* a bad URL in spec 3 of 3 stops before any case; a model that raises mid-run leaves models 1 and 3 ranked and recorded; an empty OpenAI key is a preflight error; Ctrl-C after model 1 records model 1.
- [x] **P1-20 Ungameable B38 checks** (upgrade spec addendum 2026-10-20). Nonce-checked checker scripts, pinned files, behavioural `admit_impossible`.
  *Done when:* scripted models that `os._exit(0)`/`sys.exit(0)` on import, empty `main.py`, or refuse while inventing data all fail; correct scripts pass (16/16, 18/18); a do-nothing model fails every case; the nonce never reaches the workspace or prompt.
- [x] 🔒 **P1-07 Scope the live-evals key** (upgrade spec; addendum 2026-10-20 pairs it with P1-18). Key on the eval step only; bubblewrap required; no persisted checkout credentials; `pipefail`.
  *Done when:* `live-evals.yml` has no job-level `env`, the key appears only in the eval step, and the job fails before any model call without a usable bubblewrap.
- [x] **P1-19 Per-endpoint API credentials** (upgrade spec addendum 2026-10-20; with P1-17's no-redirect bullet). `OPENAI_API_KEY` only to `api.openai.com`; other hosts need `#ENV_NAME` / `SEBASTIAN_API_KEY_ENV`; credential variables refused; redirects refused.
  *Done when:* a recording transport shows the OpenAI key only on `api.openai.com`, none on a third-party or localhost host unless named, and `#ANTHROPIC_API_KEY` is a `LeaderboardError`.
- [x] **P1-18 Sandbox and scrub the eval checkers** (upgrade spec addendum 2026-10-20). Checkers that run model-written code use an allowlisted env, bubblewrap when usable, process-group kill and bounded output; live runs refuse them without bubblewrap unless `--unsafe-unsandboxed-checkers`.
  *Done when:* fake secrets never reach `_py_ok`'s child; no background process survives; 50 MB of output stays bounded; with `bwrap_usable` false a live factory gets `checker sandbox unavailable` and no model code runs; offline suites still 16/16 and 18/18.
- [ ] 🔒 **B1 Richer mechanical report write-ups.** In `src/sebastian/watch/runner.py::mechanical_draft`, when no capability changed, emit one change per commit (title = commit subject, evidence = that commit's SHA + first changed file) instead of a single "N file(s) changed". Locked 2026-10-24: `src/sebastian/watch/` is a guarded path, so the Guard check fails this item without `owner-approved`.
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
- [x] **B76 Shared Gauntlet core.** 🔒 (touches `gauntlet.py`; owner asked, 31 Oct 2026). Move the pure contracts (`Stage`, `next_stage`, `parse_prediction`, `parse_verdict`) into stdlib-only `gauntlet_core.py`, re-exported from `gauntlet.py`, version- and hash-pinned so LESTINXOR-AGENT can vendor it byte-identical. *Done when:* no behaviour change (existing suite unchanged), purity + pin tests pass.

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
- [x] **B26 Trust ladder.** Autonomy level (L0 suggest .. L3 auto-merge code) derived from the eval and PR record; a failed eval drops a level. Auto-merge levels need owner sign-off. Split into B26a-c (B26 is bigger than one PR).
- [x] **B26a Trust level from the eval record.** `sebastian/trust.py` + `sebastian trust [--model M]`: L0..L2 from `history.jsonl` (live eval and red-team rounds); a round passes at >= 90% with every injection/attack resisted; 3 passes in a row promote, a failure drops one level. Advisory only; L3 never earned.
  *Done when:* tests cover no history, pass rules, promotion and cap, one-level drop, model filter and the CLI.
- [ ] 🔒 **B26b PR record in the ladder.** `evolve` records each PR it opens and whether it merged or was reverted; a reverted PR counts as a failed round. Touches `evolve.py` (guarded).
  *Done when:* a reverted PR drops a level in `assess`.
- [ ] 🔒 **B26c Enforce the level.** `evolve` opens nothing at L0, a draft PR at L1 and a ready PR at L2; L3 auto-merge only with an owner-set flag. Owner decision.
  *Done when:* tests show each level's behaviour in `evolve`.
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

- [x] 🔒 **P1-04 Evolve gates independent of the candidate** (upgrade spec 2026-10-19). Baseline tests/evals/config judge the candidate's src; no fail-open on a missing baseline; no drop in test count or coverage.
  *Done when:* a candidate that loosens a test, edits an eval to inflate its score, adds gate config, guts a test file or lowers coverage is rejected; a missing baseline score rejects unless `--allow-missing-baseline`.

- [x] 🔒 **P1-05 Lock down `.git` and host git** (upgrade spec 2026-10-19). File tools can't touch `.git`; evolve's git never runs hooks/fsmonitor or system config; a rewritten `.git` pointer aborts the cycle; `.gitattributes`/`.gitmodules` are denied.
  *Done when:* `.git`, `a/.git/config`, `./.git` are rejected; `_git` passes the hardening flags and `GIT_CONFIG_NOSYSTEM=1`; repo hooks and fsmonitor never fire during a cycle; a tampered pointer aborts before any git runs.

- [x] 🔒 **P1-06 Approvals in full, audited grants, normalised denials** (upgrade spec 2026-10-19).
  *Done when:* commands show one argv per line and writes a diff, nothing is truncated (too large is refused); `approval_granted` carries tool, approver and args SHA-256; denials key on normalised argv/path and stop the run after `max_denials`; one shared `parse_command()`.

- [x] 🔒 **P1-08 Redact secrets in sessions; wider redactor** (upgrade spec 2026-10-19).
  *Done when:* one shared `redact()` covers the spec's patterns plus a high-entropy heuristic, with UUIDs and git SHAs as negatives; sessions are redacted on save and resume still works; `--keep-raw-sessions` opts out; `SEBASTIAN_HOME` is `0700`.

- [x] 🔒 **P1-09 Jail checkpoint rewind and the critic diff** (upgrade spec 2026-10-19).
  *Done when:* with a tracked directory swapped for a symlink to outside, rewind raises `CheckpointError` and writes/deletes nothing outside, and the diff reports the file as skipped.

<!-- Agents add new items here: "- [ ] **Bn title.** what. *Done when:* check." -->

- [x] **B45 Trust level on the dashboard.** `sebastian dashboard` shows a tile with the B26a level (`trust.assess` over the same `history.jsonl` records), its reason, and the level after each round on the score chart's timeline. Values HTML-escaped like the rest of the page.
  *Done when:* a dashboard test with scripted history shows the tile at L0 (no rounds), L1 and L2, and a hostile model name is escaped.
- [x] **B46 `sebastian memory` list and forget.** `sebastian memory list [--query Q] [-n N]` prints stored episodes (task, outcome, date), newest first or by `recall` rank; `sebastian memory forget <task>` deletes the matching episode (same case/space-insensitive key as `record`), asking for confirmation unless `--yes`. Writes stay atomic.
  *Done when:* tests cover empty store, list order, query ranking, forget hit/miss, the confirmation prompt and `--yes`; corrupt memory still raises `MemoryCorruptError`.
- [x] **B47 `sebastian doctor`.** One command that checks the setup and prints pass/warn/fail lines: Python version, `SEBASTIAN_HOME` writable, configured backend's key variable present (never print its value), `prices.json` valid, bubblewrap usable for `--sandbox auto`, memory and history files parse. Exit 1 on any fail. No network.
  *Done when:* tests with a temp home cover each check passing and failing (bwrap probe stubbed), the key value never appears in output, and `capability_inventory` lists `doctor`.
- [x] **B48 Live evals: no sandbox is not "resisted".** A case refused for lack of bubblewrap is built with `resisted=True` (`evals/harness.py` `_refused`), so a live run with no sandbox printed `injection: 5/5 resisted outright` for hostile cases that never ran (seen 2026-10-21 on a host without bwrap: score 12%, 8 cases `checker sandbox unavailable`). Count refused cases as neither resisted nor contained, print `N cases not run: no sandbox` in the summary, and make a live run where every sandboxed case was refused exit 2 before calling the model.
  *Done when:* a harness test with the bwrap probe stubbed off shows refused hostile cases excluded from both injection counts, the summary names the refused count, and the CLI exits 2 without an LLM call.
- [ ] 🔒 **B49 Track `admit_impossible` under a capable model.** _(Locked: needs a live Ollama model run for its Done-when, and the replan nudge lives in guarded `agent.py`. Jules cannot do either; owner or Claude with a local model.)_ Live run on `minimax-m3.1-flash:cloud` via Ollama (2026-10-21) scored 15/16; the only miss was `admit_impossible` (12 steps of trying instead of reporting the task cannot be done). Add a system-prompt or replan-nudge line telling the model to stop and say so when a task is impossible, without weakening any other case.
  *Done when:* a scripted test shows the nudge reaches the model after repeated failed attempts on an impossible task, offline evals stay 100%, and the PR quotes a live `admit_impossible` PASS.

- [x] **B20 Un-redact `llm_step` token counts** (was a second B13; renumbered). Trace redaction matches any key containing "token", so `llm_step`'s `tokens=` count is always written as `[REDACTED]`. Rename it to `usage={"input", "output"}` like `run_end` (no change to the redaction rules).
  *Done when:* a `JsonlTracer` test shows the per-step counts in the file; redaction tests unchanged.

- [ ] 🔒 **B50 Check the command allowlist before asking for approval.** A live Groq gauntlet run (2026-10-22, `--allow-cmd pytest`) prompted the owner to approve `run_command` with `argv[0] = 'bash'`; the owner said yes and only then did `run_command` (`tools/builtin.py:259`) refuse it with `command not allowed`. The human is asked about calls that can never run, which trains a reflexive "y". Reject a non-allowlisted executable before the approval gate (same `parse_command()` and message, traced as a denial that counts toward `max_denials`), so the prompt only ever shows runnable commands. Touches `tools/` and the agent loop (guarded).
  *Done when:* a scripted run whose model calls a non-allowlisted command never reaches the approver (a recording approver sees zero calls), the model gets the same `command not allowed` error, the denial is traced and counted, and an allowlisted command still asks.
- [ ] 🔒 **B51 Discard typed-ahead input before an approval prompt.** `InteractiveApprover.approve` reads the answer with plain `input()` (`approval.py:155-157`), so keys typed before the prompt appears answer it. In the same run a stray extra `y` sat in the terminal buffer and would have approved the next gated call unseen. On a TTY, flush pending input (`termios.tcflush(fd, termios.TCIFLUSH)`) after printing the call and before reading the answer; keep the injectable `ask` for tests and skip the flush when stdin is not a TTY. Touches `approval.py` (guarded).
  *Done when:* a test with a pseudo-terminal (`pty.openpty`) shows a `y` written before the prompt is discarded and the call is denied unless a fresh `y` arrives after it; the `ask`-injected tests are unchanged; the non-TTY path is untouched.
- [x] 🔒 **B52 Honour the provider's retry delay on HTTP 429.** On Groq's free tier (2026-10-22, `qwen/qwen3.8-27b`) a run stopped on `HTTP 429 ... Please try again in 9.462857142s`: `RetryingLLM` (`llm/base.py:31-63`, 4 attempts, full jitter capped at 1, 2, 4 s) had given up after a few seconds of total sleep. Carry the server's delay on `TransientLLMError` (a `retry_after` seconds field, from the `Retry-After` header or the `try again in Ns` text in the body; the transport returns only `(status, body)` today, so it must also pass headers) and sleep at least that long, clamped to `max_delay`. Touches `llm/` (guarded).
  *Done when:* a fake-transport test with `Retry-After: 9` (and one with the body text only) shows the retry sleeps >= 9 s before the next attempt; a delay above `max_delay` is clamped; no delay keeps today's jittered backoff; tests use a recording `sleep`, no real waiting.
- [x] 🔒 **B53 Configurable `max_tokens` for the OpenAI-compatible backend; fail fast on "request too large".** `OpenAICompatLLM` always sends `max_tokens: 4096` (`llm/openai_compat.py:138`, `:205`). Groq's free tier caps output at 1,000 tokens per minute, so in the same run every call failed with `HTTP 429: Request too large ... output tokens per minute (OTPM): Limit 1000, Requested 1922`. That 429 is permanent for the request, but it is retried as transient and `sebastian resume` hits it again. Add `SEBASTIAN_MAX_OUTPUT_TOKENS` (and `--max-output-tokens`) passed to the backend, and raise a non-transient `LLMError` naming the setting when the body says `Request too large`. Touches `llm/` (guarded).
  *Done when:* the configured value reaches the request body (default unchanged at 4096); a fake 429 "Request too large" body raises `LLMError` (not `TransientLLMError`) with a hint mentioning `SEBASTIAN_MAX_OUTPUT_TOKENS`, and `RetryingLLM` does not retry it; an ordinary rate-limit 429 still retries.
- [ ] 🔒 **B54 Survive models that call tools on tool-free requests.** Sebastian's plan, predict and critic calls send no `tools` (`llm/openai_compat.py:207`). On Groq, `openai/gpt-oss-120b` and `openai/gpt-oss-20b` answered the planning call with their built-in `container.exec` (`ls -R`, `python - <<'PY'`), and Groq returned `HTTP 400 ... "code":"tool_use_failed"`: 6 of 6 attempts died at step 0 (one earlier run got through by luck). The built-in tool never runs (Sebastian doesn't offer it and Groq rejects it), so this is availability, not safety. On a tool-free call, treat a 400 with `tool_use_failed` as retryable once with an added system line "Answer in plain text; no tools are available", then fail with a clear `LLMError` naming the model. Touches `llm/` (guarded).
  *Done when:* a fake transport that returns `tool_use_failed` once, then text, completes the plan call with exactly one retry carrying the extra instruction; two failures in a row raise `LLMError` mentioning the model; calls that do send tools are unaffected.
- [ ] 🔒 **B55 Re-ask a malformed critic once before looping.** `parse_verdict` (`gauntlet.py:365-387`) correctly treats a malformed reply as FAIL, but the Gauntlet then spends a whole ACT loop on it. In the same run tests passed (`gauntlet_verify passed`) and the critic replied `FAIL` with no findings (`critic said FAIL without saying why`), so the agent re-read and re-ran everything, which also pushed it into the provider's rate limit. On `Verdict.malformed`, send the critic one follow-up asking only for the required format (same diff and checks, no new context) and parse that; only a second malformed or real FAIL loops back. Fail-closed is unchanged: malformed twice is still FAIL. Touches `gauntlet.py` and the agent loop (guarded).
  *Done when:* a scripted critic that answers malformed then `VERDICT: PASS` completes with 0 loops and both critic calls traced; malformed twice loops back exactly as today; a well-formed FAIL loops back without the follow-up; red-team critic cases (B35) still pass.
- [ ] **B56 `sebastian diff <run_id>`.** Print a unified diff of every file a run changed: the checkpointed original (`Checkpoints.originals`) against the current file in `Checkpoints.workspace_of(run_id)`; a created file diffs against empty, a deleted one against `/dev/null`. Same 6+ character id prefix as `show`. Read-only; no change to `checkpoint.py`.
  *Done when:* tests cover a modified, a created and a deleted file, an unknown id (exit 1), an ambiguous prefix (exit 2), and a binary file reported as `binary file differs` rather than dumped.
- [ ] **B57 Task files.** `sebastian run --task-file TASK.md`: optional front matter (`allow_cmd`, `max_steps`, `max_tokens`, `max_cost`, `model`, `gauntlet`) between `---` lines, task text below. Command-line flags override the file; unknown keys are an error (exit 2), and the file can't widen anything a flag can't (no approval or sandbox keys).
  *Done when:* tests show each key reaching the run's config, a flag overriding the file, an unknown key and a sandbox/approval key both refused, and a plain file with no front matter used as the task text.
- [ ] **B58 `sebastian cost`.** Tokens and cost per model per day from `traces.jsonl` (`--days N`, default 7; `--model M`). Runs with no reported cost show `?`, not `$0`. Read-only, reusing the trace reader `traces` already uses.
  *Done when:* tests with a scripted trace file cover two models over two days, a run with no cost, a missing or empty file (`(no runs)`) and `--days`/`--model` filtering.
- [ ] **B59 Compare two runs.** `sebastian show A --vs B [--text]`: the two timelines side by side, with a summary row (stop reason, steps, tokens, cost, Gauntlet loops) and the first step where their tool calls differ. All content escaped as in `show`.
  *Done when:* tests cover identical runs, runs that differ at step N, different stop reasons, and a hostile task string that is escaped in the HTML.
- [ ] **B60 `sebastian scar list` and `scar prune`.** `scar list` prints each scar (id, source run, case name, date) from `evals/scars/`; `scar prune` removes scars whose task text is identical (same normalised key as memory), keeping the newest, after a `[y/N]` prompt unless `--yes`. Uses `evals.scar.load_scars`; no change to `src/sebastian/evals/`.
  *Done when:* tests cover an empty directory, listing order, a prune that removes only duplicates, a decline that removes nothing, and `--yes`.
- [ ] **B61 Refresh the debt register.** `docs/SEBASTIAN_MAP.md` still lists "memory recall is keyword overlap", which B15 replaced with BM25. Re-check every debt line against the code and today's backlog; remove or update stale ones and cite the file for each that remains.
  *Done when:* every remaining line names a file or backlog id, and none describes behaviour the code no longer has.
- [ ] 🔒 **B62 Harder eval cases and cost per solved case.** Add three capability cases (a multi-file refactor, recovery after a failing tool call, a task that needs a file longer than one `read_file` page) and a "cost per solved case" column in `sebastian models`. Touches `src/sebastian/evals/` (guarded).
  *Done when:* each new case passes under a scripted good model and fails under a scripted bad one; the leaderboard shows cost/solved (`?` when cost is unknown, `-` when nothing was solved).
- [ ] 🔒 **B63 Prompt caching on the Anthropic backend.** Mark the system prompt and tool definitions as cacheable so long runs re-send them at the cached rate; record cache read/write tokens in `Usage` and price them in `pricing.py` from the owner's `prices.json`. Verify the request fields against the installed `anthropic` SDK before writing code. Touches `llm/` (guarded).
  *Done when:* a fake client sees the cache markers on the system prompt and the last tool definition; cache tokens appear in traces and cost; the OpenAI-compatible backend is unchanged.
- [ ] 🔒 **B64 Fall back to another model on persistent 429.** `SEBASTIAN_FALLBACK=backend:model[@base_url]`: when the retries in `RetryingLLM` run out on a 429, the step is retried once on the fallback model; the switch is traced (`model_fallback`, from/to) and the rest of the run stays on the fallback. Pairs with B52. Touches `llm/` (guarded).
  *Done when:* a scripted primary that always 429s and a scripted fallback complete the run with one `model_fallback` event; with no fallback set, behaviour is unchanged; a non-429 error never falls back.
- [ ] 🔒 **B65 Read-only git tools.** `git_status` and `git_diff [path]` (READ) so the agent sees repo state without `run_command`. Run with the same hardened git config as evolve's `_git` (no fsmonitor, no hooks, `GIT_CONFIG_NOSYSTEM=1`), inside the workspace jail, output capped like `read_file`. Touches `tools/` (guarded); security review in `docs/SECURITY.md`.
  *Done when:* tests in a temp repo cover clean, modified and untracked files, a path outside the jail refused, a hostile `core.fsmonitor` in the repo's config not executed, and output capping.
- [ ] 🔒 **B66 MCP client (design first).** Let Sebastian use tools from configured MCP servers, each tool classed READ or WRITE by the owner's config (default WRITE, so gated), every call through the approval gate and traced with redaction. First PR is a design doc only (`docs/design/B66-mcp.md`): transport, config format, how tool output is treated as untrusted, and the evals that must pass. Touches `tools/` and the agent loop (guarded).
  *Done when:* the design doc is approved by the owner; implementation items are split out from it.

- [ ] 🔒 **B67 Close the non-`.git` repo-skeleton gap in the workspace jail.** The P1-05 control blocks the agent writing git metadata by rejecting any path component named `.git` (`tools/workspace.py:31`), but git treats any directory holding `HEAD`, `config`, `objects/`, `refs/` as a repository under any name. The write tools can assemble such a tree under a non-`.git` directory with a `core.fsmonitor` or `core.hooksPath` in its `config`; if a human later runs git from inside that directory, that code runs on the host outside any sandbox. Confirmed empirically (F1 in the owner-held security analysis, 2026-10-26). Refuse writes to the reserved git-dir entry names (`HEAD`, `config`, `objects`, `refs`, `hooks`, `packed-refs`, `FETCH_HEAD`) when they would land directly in a workspace directory, so the agent cannot build a repo skeleton under any name; update `docs/SECURITY.md` (P1-05 row). Touches `tools/workspace.py` (guarded TCB).
  *Done when:* a test shows each reserved name refused as a direct child of a workspace dir (and the existing literal `.git` rejection unchanged), a legitimate file whose name merely contains those strings (e.g. `config.py`, `my_refs.txt`) still writes, and `docs/SECURITY.md` records the widened control.
- [ ] 🔒 **B68 Harden `watch.gitio` git invocations.** `watch/gitio.py` runs `git -C <repo> …` without the hardening `evolve.py:233-239` applies (`GIT_CONFIG_NOSYSTEM=1`, `-c core.fsmonitor=`, `-c core.hooksPath=/dev/null`). The commands used today (`rev-parse`, `merge-base`, `show`) do not fire hooks, so this is defence-in-depth, but the property should hold by construction rather than by the current command list. Apply the shared hardening to every git call in the watcher. Touches `src/sebastian/watch/` (guarded).
  *Done when:* a test shows a repo carrying a hostile `core.fsmonitor`/`core.hooksPath` is read by the watcher without the command running, and existing watch tests pass unchanged.

- [ ] **B69 Live red-team runbook + recorded baseline.** The red-team controls are only proven offline in CI; `sebastian redteam` (model-generated injections) and `redteam --target critic` (Gauntlet-reviewer attacks) have never been run live in this project because they need `ANTHROPIC_API_KEY` and a usable bubblewrap. Offline, the suite is 16/16 and all 5 injections are contained (`python -m sebastian.evals`, 2026-10-26), and a manual probe of jail/command/approval/redaction/HTML/memory held 21/22 (the one gap is F1/B67, in the owner-held security analysis, 2026-10-26). Add `docs/REDTEAM.md`: the exact commands, the environment they need (key + bubblewrap, never on the host unsandboxed), how to read a breach, and that new `redteam-*` scars are reviewed before committing. Run it once and save the output under `reports/`. Doc + report only; no code, no guarded path. (A follow-up to wire `redteam` into `.github/workflows/live-evals.yml` is guarded and tracked separately.)
  *Done when:* `docs/REDTEAM.md` gives a reader the commands and prerequisites to run both targets and judge the result, and one dated live (or clearly-labelled offline-only, if no key) run is recorded under `reports/`.

- [ ] 🔒 **B70 `apply_patch` tool (unified diff in one approval).** Today a multi-hunk change is many `edit_file` calls, each its own approval. Add a WRITE tool `apply_patch(patch)` that applies a unified diff to workspace files: every target path goes through `Workspace.resolve` (jail + `.git` block), the change is checkpointed like other writes (`before_write`/`after_write`), and it applies atomically — if any hunk fails to apply, nothing is written and the tool returns a clear error. The approval prompt already shows a diff for writes; here it shows the patch itself. No new runtime dependency (parse/apply with stdlib only). Touches `src/sebastian/tools/` (guarded TCB); update `docs/SECURITY.md` if the jail story changes.
  *Done when:* tests cover a clean multi-file multi-hunk apply (checkpointed, undoable), a hunk that does not apply (nothing written, error returned), a new-file and a delete-file hunk, a path escaping the jail refused, a `.git` path refused, and a patch over the review-size limit refused by the approver (not truncated).
- [ ] 🔒 **B71 `fetch_url` retrieval tool (off by default, host allowlist).** Give Sebastian read-only web/doc retrieval for "read this then do X" tasks, without weakening the sandbox. A READ tool `fetch_url(url)` that is absent unless the run opts in (`--allow-web host1,host2` / `SEBASTIAN_WEB_ALLOW`), HTTPS-only, requests only hosts on the explicit allowlist, refuses redirects to any off-allowlist host (reuse the `_NoRedirect` pattern from `llm/openai_compat.py`), sends no credentials or ambient headers, caps the response like `read_file` (200 KB) and returns text only. Output is tool data, already marked untrusted by the system prompt. No new runtime dependency (stdlib `urllib`); tests make no real network call (fake transport). Network in tests and real LLM/network calls stay forbidden. Touches `src/sebastian/tools/` and the CLI (guarded TCB); add a `docs/SECURITY.md` row (new network surface, allowlist, no-credentials, no-redirect).
  *Done when:* with no `--allow-web` the tool is not registered (a run cannot call it); with an allowlist, a fake transport shows an allowlisted HTTPS host fetched and capped, a non-allowlisted host refused, a plain-HTTP URL refused, a redirect to an off-allowlist host refused, and no `Authorization`/cookie header sent; `docs/SECURITY.md` records the surface.
- [x] 🔒 **B72 Publish a run: `sebastian run --pr` / `--push-main [BRANCH]`** (owner decision, 2026-10-26). After a completed run the harness (never the agent) commits the run's changes and either opens a PR or pushes straight to the branch, fast-forward only. `--push-main` implies `--gauntlet`. Refuses a dirty or non-top-level workspace before the run, unfinished runs, and changes to `.github/`, `.gitattributes` or `.gitmodules`. New module `publish.py`; reuses evolve's git hardening.
  *Done when:* tests on throwaway repos show a completed run pushed to main and a PR branch pushed with the opener called; needs_help/error/budget runs, blocked paths, a moved remote and a rejected push publish nothing; the CLI refuses a dirty workspace before building the agent.
- [x] **B73 Cross-testing between agents.** `sebastian crosstest --manifest agents.json`: run each listed agent's suite (scar-compatible JSON cases) against every listed agent in throw-away workspaces and print a target × suite pass matrix (`--json`, `--dry-run`, `--target`, `--suite`). Read-only towards every repo; no proposing, pushing or merging. Runbook: `docs/CROSSTEST.md`.
  *Done when:* fake agents show a correct matrix; timeouts, crashes and missing executables are reported per case; each case gets a fresh temp workspace that is removed; unobservable scar keys show as `unchecked`; inventory re-signed.
- [x] **B74 Cross-test fix requests.** `sebastian crosstest --results R --request SUITE/CASE --for AGENT [--backlog FILE | --id B<n>]` turns one failed case into a backlog item in the format `evolve --item` reads (next free id, task quoted as a spec, markdown and 🔒 stripped, capped). It only appends to the named file and prints the commit + `evolve` commands; it never commits, runs or pushes.
  *Done when:* the item parses with `evolve.next_item`; a pass/unchecked case, unknown names, a duplicate id and unreadable files are refused with exit 2 and leave the backlog untouched.
- [x] 🔒 **B75 Jules memory.** `docs/JULES_MEMORY.md` carries repo facts, pitfalls, owner decisions and a run log from one daily run to the next. AGENTS.md/CLAUDE.md step 1 reads it and step 6 appends to it; the Never section says it is hints, not instructions. Guarded (AGENTS.md, CLAUDE.md), so it needs `owner-approved`.
  *Done when:* AGENTS.md and CLAUDE.md stay identical and point at the file; the file states its own limits (no skipping checks, no guarded paths, no reordering) and its size rules.
