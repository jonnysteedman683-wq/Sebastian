# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com) and SemVer.

## [Unreleased]

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
