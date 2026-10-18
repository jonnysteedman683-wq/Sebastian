# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com) and SemVer.

## [Unreleased]

### Added (daily agent)
- `docs/BACKLOG.md`: ranked one-PR-sized queue for the daily upgrade agent (Jules), with *done when* checks; 🔒 items need owner sign-off.
- `AGENTS.md` / `CLAUDE.md`: the daily routine (one item, one PR, full checks, no self-merge) and hard limits.
- `.github/workflows/guard.yml`: fails PRs touching the watcher, CI, the report contract or agent rules unless labelled `owner-approved`.
- `scripts/resign_examples.py`: re-signs schema examples and updates `schemas/examples/VECTORS.md` (test vectors moved there, so routine capability changes don't touch guarded files).

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
