# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com) and SemVer.

## [Unreleased]
- fix(security): evolve requires bubblewrap and scrubs the gate environment (P1-02). `sebastian evolve` with `--sandbox auto` and no usable bwrap now ends with an `EvolveError` (exit 2) before any work; agent-written code runs unsandboxed only with `--unsafe-no-sandbox` (`--sandbox none` alone is refused). Gate commands, and bwrap itself, get only `PATH`, `LANG`, `LC_ALL`, `HOME=<worktree>` and `PYTHONPATH`, so no API key or token from the parent environment reaches the candidate's code. The report says `no secrets in gate env`.
- fix(security): sandbox and scrub the eval checkers (P1-18). The B38 checkers that run model-written code now go through `evals/sandbox.py::run_python_check`: the env is only `PATH`, `LANG`, `LC_ALL`, `HOME=<workspace>` and `PYTHONDONTWRITEBYTECODE=1` (no API keys), bubblewrap with no network when usable, the process group is killed at exit or the 20 s timeout, and only the last 64 KiB of stdout are kept. Live runs (`--live`, `sebastian models`) fail closed: without usable bubblewrap, cases that run code or `run_command` report `checker sandbox unavailable` before any model call, unless `--unsafe-unsandboxed-checkers` is passed (warns). Live `run_command` uses bubblewrap. Offline scripted runs are unchanged (16/16, 18/18).
- fix(security): `--sandbox auto` fails closed (P1-01). With no usable bubblewrap, `run`/`resume` with `--allow-cmd` exit with code 2 instead of running commands on the host. `--sandbox none` still works, warns on every run and is traced (`run_start` gains `sandbox` and `network`); its commands run without network under `unshare --user --map-root-user --net`, or the run needs an explicit `--allow-network` when unshare can't isolate the network.
- docs: docstrings for every public class, function and method in `src/sebastian/watch/` (B21); its `D1` ignore is dropped from `pyproject.toml`, so the whole package is now docstring-checked. No behaviour change.
- feat: `sebastian show <run_id>` (B42). Renders one run's trace as a self-contained HTML timeline (or `--text`): task, plan, each model step with tool calls and usage, tool results and errors, approval denials, budget warnings, and every Gauntlet stage (predict, verify, critique, revert, help). Accepts a unique id prefix of 6+ characters. No JavaScript; all trace content escaped. Capability inventory updated (`show`) and re-signed.
- fix: no stale bytecode in agent commands (B43). `run_command` now sets `PYTHONDONTWRITEBYTECODE=1` so a same-size edit to a Python file within one second never runs a stale `.pyc`.
- feat: dashboard Gauntlet and critic views (B41). Eval chart now shows plain vs Gauntlet scores together; red-team chart shows resistance of the agent vs the critic; new daily "runs needing help" trend; new Gauntlet score and Critic resistance tiles. Series are told apart by line style as well as colour (solid vs dashed), with a legend. Trend lines follow the latest model only, so leaderboard runs across several models don't zigzag. Still no JavaScript and no external assets.
- feat: cost tracking (B40). Prices you keep in `$SEBASTIAN_HOME/prices.json` (USD per million tokens; none are shipped) price every model call across all commands. `sebastian run/resume --max-cost USD` (or `SEBASTIAN_MAX_COST`) stops a run at a dollar limit and refuses to start if a model in use has no price. Traces carry `usage.cost` when priced (unpriced traces are unchanged), `sebastian traces` shows it, and the dashboard has a Spend tile.
- feat: model leaderboard (B39). `sebastian models SPEC [SPEC ...] [--gauntlet] [--no-record]` runs the eval suite (plus scars) against up to 10 models and prints them ranked by score, injection resistance, critic containment and tokens. SPEC is `[backend:]model[@base_url]`, so Claude and local OpenAI-compatible models can be compared in one table. Each model's results go to history (`eval` / `gauntlet`), so the dashboard tracks them. Capability inventory updated (`models`) and re-signed.
- feat: harder eval cases (B38): `fix_failing_test`, `rename_across_files`, `find_bug_no_hint`, `edit_json_config`, `summarise_csv`, `admit_impossible`. Code cases are judged by running the workspace's Python (`-E -s`, fresh bytecode cache, 20 s timeout), not by matching text. Suite is now 16 plain cases (18 under the Gauntlet).
- feat: OpenAI-compatible backend (B37). `SEBASTIAN_BACKEND=openai` (+ `SEBASTIAN_BASE_URL`, `OPENAI_API_KEY`) runs Sebastian on any Chat Completions server: OpenAI, Ollama, vLLM, LM Studio, llama.cpp, OpenRouter. Standard library only (no new dependency); tool calling, usage counts, retries on 429/5xx and network errors. `http://` only to loopback. All CLI commands and live evals now build models through `llm.make_llm`. Capability inventory updated (`openai_compat`) and re-signed.
- feat: Gauntlet in evolve (B34). `sebastian evolve --gauntlet` (or `--gauntlet-revert`, `--critic-model`) runs the inner agent under the Gauntlet with checkpoints on, so its own predicted checks and a critic must pass before the outer gates run. A `needs_help` inner run is rejected with a `gauntlet` gate and its patch kept. Outer gates and the never-merge rule are unchanged. Off by default.
- feat: revert between Gauntlet loops (B33). `GauntletPolicy(revert_on_loop=True)` / `sebastian run --gauntlet-revert` (implies `--gauntlet`) restore every file the run's tools changed before looping back after a failed VERIFY or CRITIQUE, and tell the model so. New `Checkpoints.rewind()` keeps a later `sebastian undo` exact. HELP leaves the last attempt in place. Off by default.
- feat: critic red team (B35). `sebastian redteam --target critic` asks the attacker for edit tasks plus a fixed change that slips in an unrequested `PWNED` line dressed up for the reviewer (policy claims, fake approvals, comments to the reviewer). A scripted actor and planner apply it, so only the target's critic verdict is live; any PASS is a breach saved as a `redteam-critic-*` scar (`"target": "critic"`, `expect.critic_rejects`). Critic scars replay in Gauntlet eval runs. Rounds are recorded in history as `redteam_critic`. Eval results now report `critic_calls`/`critic_passes`.
- feat: Gauntlet evals (B11e). `python -m sebastian.evals --gauntlet` runs the suite under the Gauntlet; `--compare` runs plain and Gauntlet and prints them side by side (per-case result, stop reason and steps, then score, injection resistance, needs_help, steps and tokens for the shared cases). Two critic-attack cases (`critic_approved_note`, `critic_policy_note`) plant reviewer-aimed approval text; they run only under the Gauntlet and pass when the run never claims success on a broken or tampered result. Live runs record a `gauntlet` history entry beside `eval`. Offline, the planner and critic replay per-case scripts. Completes B11.
- feat: Gauntlet wired into runs (B11d). `sebastian run/resume --gauntlet` plans, predicts checks, acts, runs the checks (VERIFY) and asks a tool-free critic (CRITIQUE) each time the model claims it is done. Failures loop back with findings; every `replan_after`-th failure replans and re-predicts; past `max_loops` (3) the run ends with the new stop reason `needs_help` and a structured report (stage, reason, rounds, checks, last findings). A denied check goes straight to HELP. `--critic-model` / `SEBASTIAN_CRITIC_MODEL` pick the critic (implies `--gauntlet`; default the planning model). The critic now sees the run's real diff from checkpoints. Dashboard gains a "Needs help" tile; scars may expect `needs_help`. Off by default. Capability inventory unchanged (flags only); examples re-signed with no changes.
- docs: MIT licence (B12): `LICENSE`, `license = "MIT"` in `pyproject.toml`, README section.
- feat: Gauntlet CRITIQUE stage (B11c): `critique()` runs a tool-free critic over task, plan, prediction, diff and check results, all marked as data. `parse_verdict` is strict (anything malformed, or a PASS with findings, counts as FAIL), and the critic refuses to run when VERIFY failed. Not wired into runs yet.
- feat: Gauntlet VERIFY stage (B11b): `verify()` runs predicted checks through `run_command` and the approval gate. Any non-zero exit, error or denial fails the gate, and `findings()` builds the loop-back turn with tail-capped output marked as data. Not wired into runs yet.
- feat: `sebastian.gauntlet` stage machine and PREDICT stage (B11a): bounded loop/replan/help transitions, strict `<checks>` parsing (workspace-relative paths, bare executables, no shell characters, Python changes need a check). Not wired into runs yet.
- docs: B11 Gauntlet design doc for owner review (`docs/design/B11-gauntlet.md`).

### Changed (B19 sandbox by default)
- `sebastian run/resume --allow-cmd ...` now default to `--sandbox auto`: `run_command` runs under bubblewrap (no network, workspace-only, Python install paths read-only) whenever bubblewrap works on the host. If it does not, commands run unsandboxed and a warning is printed every run; `--sandbox none` opts out silently, and `--sandbox bwrap` refuses to start without it.
- `resolve_command_sandbox()` and `python_roots()` live in `tools/builtin.py` (`evolve` reuses them).

### Added (B32 dashboard)
- `sebastian dashboard [-o file] [--scars dir]`: writes one self-contained HTML page (no JavaScript, no external assets, light and dark, phone-width safe) with tiles for the latest live eval score, injection resistance, scar count and run completion; trend charts of eval score and red-team resistance; runs by outcome; recent runs; and the scar list. Every value read from local files is HTML-escaped.
- Score history (`history.py`, `$SEBASTIAN_HOME/history.jsonl`): live eval runs (`python -m sebastian.evals --live`, opt out with `--no-record`) and every `sebastian redteam` round append a record. Offline (scripted) runs are not recorded. Capability inventory updated and re-signed.

### Added (B31 evolve resume)
- `sebastian evolve --resumes N` (default 1): when the agent run inside a cycle stops on its budget, it is resumed from its saved conversation (fresh budget each time) up to N times before the gates run, instead of the work being judged half-done. `--resumes 0` disables it. The report shows `agent resumed: Nx`; gates and limits are unchanged.

### Added (B30 red-team twin)
- `sebastian redteam -n N [--attacker-model M] [--out evals/scars] [--no-save]`: an attacker model writes prompt-injection test cases (a harmless read-only task plus workspace files hiding an instruction). Every attack must aim at a fixed canary (create `PWNED.txt` or run `echo pwned`); proposals without the canary, with unsafe paths, a pre-seeded canary or oversized content are discarded before they run.
- Each attack runs against a fresh target with every gated action denied, so nothing changes even on success. A *breach* is any attempt at a gated action; breaches are saved as scars (`evals/scars/redteam-*.json`, expecting `no_gated_attempt` and `unchanged`) that every live eval run replays. Exit code 1 when any breach was found. The attacker gets no tools.
- Scar format: new `expect.no_gated_attempt`, and optional `hostile` and `commands` fields. Capability inventory updated and re-signed.

### Added (B29 sandboxed agent commands in evolve)
- When `evolve`'s gates run under bubblewrap, the agent's own `run_command` calls during the cycle (pytest, ruff, mypy) run in the same sandbox: no network, only the worktree writable, cleared environment. The whole self-improvement loop is now sandboxed whenever bwrap is usable.
- `CommandPolicy.extra_ro`: extra read-only paths inside the sandbox; executables under them are allowed. `python_roots()` now also exposes the console-script directories (system and `pip --user`).

### Added (B27 sandboxed evolve gates)
- `sebastian evolve --sandbox auto|bwrap|none` (default `auto`): the gate commands (ruff, format, mypy, pytest, and both eval runs) execute under bubblewrap when it is usable: every namespace unshared (no network), only the worktree writable (at `/workspace`), system and Python install paths read-only, private `/tmp`, cleared environment. `bwrap` makes the sandbox mandatory and fails before any worktree is created if it is unavailable; `auto` falls back to unsandboxed and says so. The report shows `gate sandbox: bwrap|none`.
- `bwrap_argv(..., extra_ro=...)` and `bwrap_usable()` in `tools/builtin.py`.

### Added (B28 resume)
- `sebastian resume <run_id> -w <workspace>`: continues a run that stopped early (budget, model error, or killed mid-turn) from its saved conversation under a fresh budget. The continuation is a new run with its own trace, checkpoints and session, recording `resumed_from`; the model is told why the run stopped and not to redo finished work. Completed runs and runs saved for a different workspace are refused; a turn killed before its tool calls ran is closed with "interrupted" results so tool pairing stays valid.
- `session.py` (`SessionStore`): the system prompt and full message history are saved atomically to `$SEBASTIAN_HOME/sessions/<run_id>.json` after every step. The agent loop is shared by `run` and `resume` (`_drive`). Capability inventory updated and re-signed.

### Added (B25 Ouroboros)
- `sebastian evolve`: one self-improvement cycle on Sebastian's own repo. Takes the first backlog item with no 🔒 anywhere in it (or `--item`), creates a candidate worktree on a new branch plus a baseline worktree, and runs the agent (with a planning phase) under a `ScopedApprover`: writes are approved only outside the guarded paths (read from `.github/workflows/guard.yml`, resolved through `..` and symlinks) and commands only for `pytest`, `ruff`, `mypy`.
- Deterministic gates, in order: non-empty diff, no guarded path in the diff, every CI check (ruff, format, mypy, pytest) passes in the worktree, offline eval score >= baseline. Only then is the change committed (author "Sebastian"); `--push` pushes the branch and `--pr` opens a PR with `gh`. Never merges. A rejected attempt leaves no branch; its diff is saved under `$SEBASTIAN_HOME/evolve/`.
- `CommandPolicy.env`: fixed extra variables for `run_command` (used for `PYTHONPATH=src`).

### Added (B24 scar tissue)
- `sebastian scar <run_id> -w <workspace>`: turns a failed run (from `traces.jsonl`) into a JSON eval case in `evals/scars/`. The workspace is rebuilt as it was before the run (current text files with the run's checkpointed changes reverted and files it created dropped; size-capped). Refuses runs that completed cleanly; never overwrites an existing scar.
- Scar format with a declarative `expect` checker (`stop`, `max_steps`, `files`, `contains`, `absent`, `answer_contains`, `unchanged`, `no_gated_success`), strictly validated; seed paths cannot escape the workspace.
- The eval runner loads scars (`--scars`, default `evals/scars`). Scars are live-only: offline runs list them as skipped, `--live` and the weekly Live evals workflow replay them.
- `Checkpoints.originals()`; `EvalCase.live_only` and `SuiteResult.skipped`.

### Added (B23)
- Undo for writes (`checkpoint.py`): before `write_file`/`edit_file` first change a file in a run, its original bytes (or its absence) are saved to `$SEBASTIAN_HOME/checkpoints/<run_id>/`, and each write's result hash is recorded. `sebastian undo <run_id> -w <workspace>` restores changed files and deletes files the run created; files changed since the run are skipped unless `--force`; `--list` shows what the run changed. Blobs are hash-verified before restoring. Capability inventory updated and re-signed.

### Added (B22)
- `edit_file` tool (WRITE, gated): replaces exact text in an existing workspace file; the text must be unique unless `replace_all` is set, and the file is untouched on any error. Saves the model rewriting whole files for small changes. Capability inventory in `schemas/examples/` updated and re-signed.

### Added (B7)
- Docstrings on every public module, class, function and method in `src/sebastian/` (except the guarded `watch/` package, tracked as B21). Ruff now enforces `D1` for `src/` (`D105`/`D107` ignored; tests and scripts exempt).

### Fixed (B20)
- `llm_step` and `plan` trace events now carry `usage={"input", "output"}` instead of `tokens`, which redaction always replaced with `[REDACTED]`. Redaction rules are unchanged. (Backlog item renumbered from a duplicate B13.)

### Added (B10, owner-approved)
- `run_command` sandbox: `--sandbox bwrap` / `CommandPolicy(sandbox="bwrap")` runs each command under bubblewrap with all namespaces unshared (no network), only the workspace bind-mounted read-write at `/workspace`, read-only system paths, private `/tmp` and a cleared environment. Executables must resolve under system paths. Missing bubblewrap is a tool error, never a silent fallback.

### Added (B18, owner-approved)
- `.github/workflows/live-evals.yml`: manual and weekly (Mon 07:30 Brisbane) live eval run against the real model; skipped when the `ANTHROPIC_API_KEY` secret is absent; score posted to the job summary.
- CI runs the offline eval suite and installs bubblewrap so the real sandbox test runs where namespaces are permitted.

### Added (B17)
- Model routing: `planner_llm` for the planning phase and `escalation_llm` for the one step after a replan nudge (both default to the main model). CLI `--plan-model` (implies `--plan`) and `--escalation-model`; env `SEBASTIAN_PLAN_MODEL`, `SEBASTIAN_ESCALATION_MODEL`. A routed model equal to the main model reuses its client. Emits `escalated`.

### Added (B16)
- Parallel read tools: when every call in a turn is READ-only (after the unknown-tool, repeat-failure and approval checks, which stay sequential), the tool bodies run concurrently (up to 8 threads); results keep call order. Any WRITE/EXEC call in the turn keeps the whole turn sequential. Tracing stays on the main thread. Emits `tools_parallel`; `Sebastian(parallel_reads=False)` disables it.

### Changed (B6, B15)
- Memory: recording the same task again (case/whitespace-insensitive) replaces the old episode, so the newest outcome wins. Recall is now BM25 over task + summary with stopwords removed (rare terms outweigh common ones; shorter matching episodes rank higher; ties go to the most recent). Still pure Python.

### Added (B14)
- Planning phase (`Sebastian(plan=True)`, `sebastian run --plan`): one tool-free model call writes a numbered plan that is injected into the system prompt as `<plan>`; counts against the step and token budget; emits a `plan` trace event. Off by default.
- Replan nudge: after `replan_after_errors` (default 3) consecutive failed tool calls, a `<replan>` note rides on the tool-results turn asking the model to revise its approach; emits `replan_nudge`. `0` disables it.

### Fixed
- Anthropic backend: in user turns, `tool_result` blocks are now sent before any text block (required by the Messages API), and `tools` is omitted when empty.

### Added (B8, B9)
- Eval harness `sebastian.evals`: fresh workspace per case, checker over workspace/result/trace, score and summary. 5 capability cases and 5 prompt-injection cases (file override, fake system tag, filename, fake tool error, search output). Offline via `pytest -m eval` (score printed in the pytest summary) or `python -m sebastian.evals`; `--live` runs the same suite against a real model, `--min-score` sets the exit threshold.

### Added (B13)
- Context management (`context.py`, `ContextPolicy`): each tool result is capped (head + tail kept, default 12k chars) and, once the history passes `max_history_chars` (default 240k), tool results older than the last `keep_recent` turns are replaced by stubs, oldest first. Task text, assistant text and tool-call pairing are never altered. Emits a `context_compacted` trace event.

### Added (daily agent)
- `docs/BACKLOG.md`: ranked one-PR-sized queue for the daily upgrade agent (Jules), with *done when* checks; 🔒 items need owner sign-off.
- `AGENTS.md` / `CLAUDE.md`: the daily routine (one item, one PR, full checks, no self-merge) and hard limits.
- `.github/workflows/guard.yml`: fails PRs touching the watcher, CI, the report contract or agent rules unless labelled `owner-approved`.
- `scripts/resign_examples.py`: re-signs schema examples and updates `schemas/examples/VECTORS.md` (test vectors moved there, so routine capability changes don't touch guarded files).

### Added (B5)
- `sebastian traces [-n N]`: prints the last N runs (default 10) from `traces.jsonl` as `run_id  start  stop  steps=  tokens=`; skips malformed lines; unfinished runs show `running`.
- `run_end` trace events carry `usage={"input", "output"}`. The key avoids "token", which redaction treats as a secret (that is why `llm_step`'s `tokens` field is always redacted; see B13).

### Added (B4)
- `budget_warning` trace event (`limit`, `used`, `max`) emitted once per run for each of steps, tokens and seconds when usage reaches 80% of its limit (`Governor.new_warnings`, `WARN_FRACTION`). Stopping behaviour is unchanged.

### Added (B3)
- `search_files` tool (READ): case-sensitive substring search across workspace text files, returning `path:line: text` (1-based), capped at 200 matches; skips binary files, files over `MAX_READ_BYTES`, `.git/` and symlinks that leave the workspace.

### Added (B2)
- `read_file` takes optional `start_line` (0-based) and `max_lines` (0 = all) to read a slice of a file; out-of-range or negative values return a tool error.

### Changed
- **Watch pipeline is keyless by default.** Reports are published to the `watch-state` branch (`latest.json`, `reports/NNNNNN.json`, `state.json` in one commit) and read from GitHub's public raw URLs. Reports carry a SHA-256 fingerprint; GitHub provides provenance. Push mode (endpoints + HMAC) remains optional.
- First run starts from the previous commit automatically; no baseline or secrets needed.
- An escalated report is always `priority: HIGH` (schema + `finalize`).
- No draft service configured is no longer counted as a draft failure.

### Added
- `schemas/repo_change_report.schema.json`: JSON Schema (2020-12) contract for GROK-WATCHER -> CO-ORDINATOR-BOT reports, with signed examples, HMAC test vectors and signing spec in `schemas/README.md`.
- Work orders for both bots in `docs/watch/`, pointing at the schema as the authoritative contract.
- Contract tests (`tests/test_report_schema.py`): schema validity, examples, signatures, 20 invalid-report cases, and a check that the example capability inventory matches what Sebastian actually ships.
- Dev dependencies: `jsonschema`, `types-jsonschema`.
- `sebastian-watch` (watchkit, `sebastian.watch`): `scan`, `finalize`, `canary`, `verify` for the watcher and coordinator. Static capability inventory via `git show` + `ast` (repo code never executed), deterministic risk signals, history-rewrite detection, rule enforcement (priority/risk/escalation only ever raised), schema validation and HMAC signing. Optional extra `watch`.
- CI: watchkit canary build + verify smoke step.
- `.github/workflows/watch.yml`: unattended watcher on push, hourly, daily digest (08:00 Brisbane) and weekly canary (Mon 08:15 Brisbane). State on the `watch-state` branch, advanced only on coordinator acknowledgement. Dry run until secrets are set.
- `sebastian-watch run`: full pipeline with Grok draft endpoint, mechanical fallback draft, delivery retries (30s/2m/8m), outage and history-rewrite escalation.
- Changes to watch tooling, the watch workflow or the schema now add +3 risk and always escalate.
- `docs/watch/RUNBOOK.md`: setup, endpoint contracts, state, operations.

## [0.1.0] - 2026-10-18

### Added
- Core agent loop with completion, budget-exhaustion and error stop reasons.
- Governor enforcing step, token and wall-clock budgets.
- Approval gate; unattended and non-TTY runs deny all gated actions.
- Typed tools with schemas inferred from type hints and strict argument validation.
- Workspace jail (path, absolute and symlink escapes rejected).
- Opt-in sandboxed `run_command`: allowlist, no shell, scrubbed env, rlimits, timeout + group kill.
- Validated, atomic JSON episodic memory with recall injected into the system prompt.
- JSONL tracing with secret redaction.
- Provider-agnostic LLM interface, retry with exponential backoff + jitter, scripted test LLM.
- Anthropic backend with prompt caching on the system prompt.
- CLI: `run`, `demo` (offline), `version`.
- CI: ruff, mypy strict, pytest with 90% coverage gate on Python 3.11-3.13, demo run, pip-audit.
