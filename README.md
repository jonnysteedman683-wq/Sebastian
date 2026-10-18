# Sebastian

An autonomous, tool-using agent built for safety first: every action is jailed to a workspace, risky actions need approval, and every run is bounded by a budget.

Pure Python, zero runtime dependencies. The Anthropic backend is an optional extra.

## Quick start

```bash
pip install -e ".[anthropic]"
sebastian demo -w /tmp/ws            # offline check, no API key needed
export ANTHROPIC_API_KEY=...
sebastian run "Create notes.md summarising todo.txt" -w ./project
```

Enable command execution per executable (no shell, sandboxed):

```bash
sebastian run "Run the tests and fix any failure" -w ./project --allow-cmd pytest
```

## How it works

```
task -> recall memory -> [ LLM -> gated tool calls -> results ]* -> answer
                              |            |
                         Governor     ApprovalGate
                   (steps/tokens/time)  (WRITE+ needs a yes)
```

| Module | Role |
|---|---|
| `agent.py` | Core loop. Stops on completion, budget exhaustion or model error. |
| `budget.py` | Governor. The only component that can end a run for budget. |
| `approval.py` | Approval gate. Non-TTY or `--unattended` runs deny every gated action. |
| `tools/` | Typed tools, schemas inferred from type hints, workspace jail, sandboxed `run_command`. |
| `memory.py` | Validated, atomically written episodic memory with recall. |
| `trace.py` | JSONL run traces with secret redaction. |
| `llm/` | Provider-agnostic interface, retry with backoff + jitter, scripted test double, Anthropic backend. |

Full map: [`docs/SEBASTIAN_MAP.md`](docs/SEBASTIAN_MAP.md). Threat model: [`docs/SECURITY.md`](docs/SECURITY.md).

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Read by the SDK only; never logged. |
| `SEBASTIAN_MODEL` | `claude-sonnet-5-5` | Also `--model`. |
| `SEBASTIAN_MAX_STEPS` | `25` | |
| `SEBASTIAN_MAX_TOKENS` | `200000` | |
| `SEBASTIAN_MAX_SECONDS` | `600` | |
| `SEBASTIAN_HOME` | `~/.sebastian` | Memory and traces. |

## Development

```bash
pip install -e ".[dev,anthropic]"
ruff check . && ruff format --check . && mypy && pytest --cov
```

Maintenance and upgrades follow [`docs/WORK_ORDER.md`](docs/WORK_ORDER.md).
