# Sebastian Map

Baseline: v0.1.0 (initial scaffold). Update this file in the same PR as any structural change.

## Purpose

Sebastian takes a natural-language task, works through it by calling tools inside a sandboxed workspace, and returns an answer. It guarantees termination (budget governor), refuses risky actions without approval, remembers past episodes, and traces every run.

## Architecture

```mermaid
flowchart TD
    CLI[cli.py] --> A[agent.Sebastian.run]
    A --> M[memory.MemoryStore.recall]
    M --> P[prompts.build_system_prompt]
    P --> L{loop}
    L --> G[budget.Governor.exhausted?]
    G -- spent --> E[RunResult BUDGET_EXHAUSTED]
    G -- ok --> LLM[llm: RetryingLLM -> AnthropicLLM]
    LLM -- no tool calls --> D[RunResult COMPLETED]
    LLM -- error --> X[RunResult ERROR]
    LLM -- tool calls --> AG[approval.ApprovalGate]
    AG -- denied --> R[error ToolResult]
    AG -- allowed --> T[tools: jailed file tools / sandboxed run_command]
    T --> R2[ToolResult]
    R --> L
    R2 --> L
    D & E & X --> MR[memory.record]
    A -. every event .-> TR[trace.JsonlTracer]
```

| Concern | Location |
|---|---|
| Entry points | `sebastian` console script, `python -m sebastian` -> `cli.main`; `sebastian-watch` -> `watch.cli.main` |
| Agent loop | `agent.py` |
| Context management | `context.py`: per-result cap + oldest-first elision of tool results (`ContextPolicy`) |
| Tool layer | `tools/base.py`, `tools/builtin.py`, `tools/workspace.py` |
| Memory/state | `memory.py` (JSON, schema v1) |
| LLM backends | `llm/anthropic.py`, `llm/scripted.py`, `llm/base.py` |
| Schedulers / servers | None (deliberately). See PROPOSALS. |
| Repo watch tooling | `watch/` (`sebastian-watch`): `gitio.py` read-only git, `inventory.py` static AST inventory + risk signals, `report.py` finalise/sign/verify, `runner.py` unattended run + delivery, `cli.py`; scheduled by `.github/workflows/watch.yml`; keyless publish to the `watch-state` branch (`latest.json`, `reports/`, `state.json`) |
| Daily agent | `docs/BACKLOG.md` (queue), `AGENTS.md` (routine), `.github/workflows/guard.yml` (guarded paths), `scripts/resign_examples.py` |
| External contracts | `schemas/repo_change_report.schema.json`: report format for the repo watcher and coordinator bots (`docs/watch/`) |

## Dependencies

Runtime: none. Optional `anthropic>=1.15` (verified against 1.15.0; uses `httpx2`). Dev: ruff, mypy, pytest, pytest-cov, jsonschema, types-jsonschema.

## Model / provider usage

| Where | Value | Verified |
|---|---|---|
| `config.DEFAULT_MODEL` | `claude-sonnet-5-5` | Oct 2026, current Anthropic model ID |
| Messages API | `messages.create` with `tools`, `tool_use`, `tool_result` | SDK 1.15.0 signatures inspected |

## Config & secrets

See README table. Only secret is `ANTHROPIC_API_KEY`, read by the SDK; never stored on `Config`, redacted in traces.

## Tests

pytest, 159 tests (incl. 28 schema contract and 46 watchkit tests), 98.5% branch coverage (gate 90%). No network or real LLM in tests.

## Runtime check

```bash
pip install -e ".[dev,anthropic]"
sebastian demo -w /tmp/ws   # -> "[completed] steps=3"
```

## Debt register

- Memory recall is keyword overlap (planned U5: embeddings).
- `run_command` arguments are not path-jailed (see SECURITY residual risks).
- No live-API smoke test in CI (needs a secret; Phase 2 decision).
