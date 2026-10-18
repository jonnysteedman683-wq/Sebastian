# Sebastian - agent instructions

Any coding agent working in this repo follows `docs/WORK_ORDER.md`. Short version:

- Branch `sebastian/<phase>-<desc>`; never commit to `main`. Conventional Commits.
- Before pushing: `ruff check . && ruff format --check . && mypy && pytest --cov` must all pass.
- Evidence over claims: cite file, line, command and output.
- Never invent model IDs, SDK fields or package versions; verify against installed SDK or official docs.
- Tests use `ScriptedLLM`, `FakeClock`, `SeededIdSource`. No network or real LLM in tests.
- Security-relevant changes update `docs/SECURITY.md`. Structural changes update `docs/SEBASTIAN_MAP.md`.
- Out-of-scope ideas go in `docs/PROPOSALS.md`. Stop and escalate per WORK_ORDER section 9.
