# Sebastian

An autonomous, tool-using agent built for safety first: every action is jailed to a workspace, risky actions need approval, and every run is bounded by a budget.

Pure Python, zero runtime dependencies. The Anthropic backend is an optional extra.

## Quick start

```bash
pip install -e ".[anthropic]"
# or run on a local / open-weight model, no extra install:
#   SEBASTIAN_BACKEND=openai SEBASTIAN_BASE_URL=http://localhost:11434/v1 sebastian run "..." --model qwen3
sebastian demo -w /tmp/ws            # offline check, no API key needed
export ANTHROPIC_API_KEY=...
sebastian run "Create notes.md summarising todo.txt" -w ./project
```

Enable command execution per executable (no shell, sandboxed):

```bash
sebastian run "Run the tests and fix any failure" -w ./project --allow-cmd pytest
# Commands are sandboxed by default when bubblewrap works (no network, workspace only).
# --sandbox bwrap makes it mandatory; --sandbox none turns it off.
```

Plan before acting, and run the eval suite:

```bash
sebastian run "Refactor utils.py and keep the tests green" -w ./project --plan
sebastian run "Fix the failing parser test" -w ./project --allow-cmd pytest --gauntlet
#   --gauntlet: plan, predict the checks, act, run the checks, then a critic review;
#   failures loop back (max 3), then the run stops as needs_help with a report;
#   --gauntlet-revert undoes a failed attempt's file changes before each retry
sebastian resume <run_id> -w ./project # continue a run that hit its budget or an API error
sebastian undo <run_id> -w ./project # roll back a run's file changes (run_id from `sebastian traces`)
sebastian evolve --repo . --pr --sandbox bwrap  # improve itself: next backlog item, sandboxed gates, opens a PR (never merges)
sebastian evolve --repo . --gauntlet-revert     # same, but the inner run must prove itself; needs_help = rejected
sebastian scar <run_id> -w ./project # turn a failed run into a permanent eval case (evals/scars/)
sebastian redteam -n 10               # attacker model vs Sebastian; breaches become eval cases
sebastian redteam --target critic     # attacks on the Gauntlet reviewer; any PASS is a breach
sebastian dashboard                  # HTML page: eval trend, injection resistance, scars, runs
python -m sebastian.evals            # offline, no key
python -m sebastian.evals --live     # real model: capability + prompt-injection score
python -m sebastian.evals --live --compare  # plain vs Gauntlet side by side, plus critic attacks
```

## How it works

```
task -> recall memory -> [plan] -> [ compact -> LLM -> gated tool calls -> results ]* -> answer
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
| `context.py` | Caps tool results and elides old ones so long runs stay inside the context budget. |
| `evals/` | Capability and prompt-injection eval suite, offline or live. |
| `memory.py` | Validated, atomically written episodic memory with recall. |
| `trace.py` | JSONL run traces with secret redaction. |
| `llm/` | Provider-agnostic interface, retry with backoff + jitter, scripted test double, Anthropic backend. |

Full map: [`docs/SEBASTIAN_MAP.md`](docs/SEBASTIAN_MAP.md). Threat model: [`docs/SECURITY.md`](docs/SECURITY.md).

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Read by the SDK only; never logged. |
| `SEBASTIAN_MODEL` | `claude-sonnet-5-5` | Also `--model`. |
| `SEBASTIAN_PLAN_MODEL` | (main model) | Planning model; also `--plan-model` (implies `--plan`). |
| `SEBASTIAN_ESCALATION_MODEL` | (main model) | Used for the step after repeated tool failures; also `--escalation-model`. |
| `SEBASTIAN_CRITIC_MODEL` | (planning model) | Gauntlet critic; also `--critic-model` (implies `--gauntlet`). |
| `SEBASTIAN_BACKEND` | `anthropic` | `openai` = any OpenAI-compatible server (OpenAI, Ollama, vLLM, LM Studio, OpenRouter...). No extra install. |
| `SEBASTIAN_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint. Plain `http://` only for localhost. |
| `OPENAI_API_KEY` | (none) | Sent as a bearer token by the `openai` backend; never logged. Local servers usually need none. |
| `SEBASTIAN_MAX_STEPS` | `25` | |
| `SEBASTIAN_MAX_TOKENS` | `200000` | |
| `SEBASTIAN_MAX_SECONDS` | `600` | |
| `SEBASTIAN_HOME` | `~/.sebastian` | Memory and traces. |

## Repo watch (watchkit)

`sebastian-watch` supports the GROK-WATCHER and CO-ORDINATOR-BOT ([`docs/watch/`](docs/watch/)). It reads the repo through git and `ast` only, never running repo code.

```bash
pip install -e ".[watch]"
# Keyless by default. Optional: export WATCHER_HMAC_KEY=... for HMAC signing (never printed)
sebastian-watch scan --from <last_sha> --to origin/main > facts.json   # watcher: facts
sebastian-watch finalize --draft draft.json --facts facts.json --seq N # watcher: validate + sign
sebastian-watch canary --seq N                                          # watcher: weekly test
sebastian-watch verify report.json --expect-seq N --expect-from <sha>   # coordinator: accept/reject
```

Report contract: [`schemas/`](schemas/). Automated by `.github/workflows/watch.yml` with no keys: reports land on the `watch-state` branch ([`latest.json`](https://raw.githubusercontent.com/jonnysteedman683-wq/Sebastian/watch-state/latest.json)). Details: [`docs/watch/RUNBOOK.md`](docs/watch/RUNBOOK.md).

## Development

```bash
pip install -e ".[dev,anthropic,watch]"
ruff check . && ruff format --check . && mypy && pytest --cov
```

Maintenance and upgrades follow [`docs/WORK_ORDER.md`](docs/WORK_ORDER.md).

## Licence

MIT. See [`LICENSE`](LICENSE).
