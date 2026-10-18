# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com) and SemVer.

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
