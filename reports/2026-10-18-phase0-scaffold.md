# Report: Phase 0 - Initial scaffold (2026-10-18)

**Baseline:** repo was empty. Bootstrap commit `89e2912` on `main`; scaffold on `sebastian/phase0-scaffold`.

## Evidence
| Check | Command | Result |
|---|---|---|
| Lint | `ruff check . && ruff format --check .` | clean |
| Types | `mypy` (strict) | 0 issues, 26 files |
| Tests | `pytest --cov` | 85 passed, 99.6% branch coverage |
| End-to-end | `sebastian demo -w /tmp/ws` | `[completed] steps=3`, file written and verified |
| Python matrix | 3.11, 3.12, 3.13 | 85 passed on each; mypy clean on each |
| SDK | anthropic 1.15.0 signatures inspected | `messages.create`, `tool_use`, `tool_result` fields match |

## Open risks
See `docs/SECURITY.md` residual risks 1-4.

## Escalations awaiting decision
- Licence choice.
- Whether to add a live API smoke test (needs `ANTHROPIC_API_KEY` repo secret).

## Recommended next action
Proposal 1 (container sandbox) then Proposal 2 (eval harness), before any capability upgrade.
