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
# Commands are sandboxed with bubblewrap (no network, workspace only). Without a usable
# bubblewrap the run refuses to start; --sandbox none runs on the host (network cut off with
# unshare, or --allow-network to accept a networked host process).
```

Plan before acting, and run the eval suite:

```bash
sebastian run "Refactor utils.py and keep the tests green" -w ./project --plan
sebastian run "Fix the failing parser test" -w ./project --allow-cmd pytest --gauntlet
#   --gauntlet: plan, predict the checks, act, run the checks, then a critic review;
#   failures loop back (max 3), then the run stops as needs_help with a report;
#   --gauntlet-revert undoes a failed attempt's file changes before each retry
sebastian run "Fix the flaky test" -w ./repo --allow-cmd pytest --pr         # completed run -> branch + PR
sebastian run "Bump the version" -w ./repo --allow-cmd pytest --push-main  # Gauntlet-proven -> push to main
#   publishing is done by the harness after the run, never the agent; clean repo required,
#   fast-forward only, .github/ and git config files are never published
sebastian resume <run_id> -w ./project # continue a run that hit its budget or an API error
sebastian show <run_id>              # one run as an HTML timeline (--text for the terminal)
sebastian undo <run_id> -w ./project # roll back a run's file changes (run_id from `sebastian traces`)
sebastian evolve --repo . --pr  # improve itself: next backlog item, bubblewrap-sandboxed gates (required), opens a PR (never merges)
sebastian evolve --repo . --gauntlet-revert     # same, but the inner run must prove itself; needs_help = rejected
sebastian scar <run_id> -w ./project # turn a failed run into a permanent eval case (evals/scars/)
sebastian redteam -n 10               # attacker model vs Sebastian; breaches become eval cases
sebastian redteam --target critic     # attacks on the Gauntlet reviewer; any PASS is a breach
sebastian dashboard                  # HTML page: eval trend, injection resistance, scars, runs
sebastian crosstest --manifest agents.json  # every agent's suite vs every agent; read-only (docs/CROSSTEST.md)
python -m sebastian.evals            # offline, no key
python -m sebastian.evals --live     # real model: capability + prompt-injection score
python -m sebastian.evals --live --compare  # plain vs Gauntlet side by side, plus critic attacks
sebastian models claude-sonnet-5-5 openai:qwen3@http://localhost:11434/v1 --gauntlet  # rank models
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

Costs (B40): put your provider's prices in `$SEBASTIAN_HOME/prices.json`, in USD per million
tokens, e.g. `{"claude-sonnet-5-5": {"input": 3, "output": 15}, "qwen3": {"input": 0, "output": 0}}`
(copy the numbers from the provider's pricing page; Sebastian ships none). Priced runs show their
cost in `sebastian traces` and the dashboard.

| Env var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Read by the SDK only; never logged. |
| `SEBASTIAN_MODEL` | `claude-sonnet-5-5` | Also `--model`. |
| `SEBASTIAN_PLAN_MODEL` | (main model) | Planning model; also `--plan-model` (implies `--plan`). |
| `SEBASTIAN_ESCALATION_MODEL` | (main model) | Used for the step after repeated tool failures; also `--escalation-model`. |
| `SEBASTIAN_CRITIC_MODEL` | (planning model) | Gauntlet critic; also `--critic-model` (implies `--gauntlet`). |
| `SEBASTIAN_BACKEND` | `anthropic` | `openai` = any OpenAI-compatible server (OpenAI, Ollama, vLLM, LM Studio, OpenRouter...). No extra install. |
| `SEBASTIAN_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint. Plain `http://` only for localhost. |
| `OPENAI_API_KEY` | (none) | Sent as a bearer token **only to `api.openai.com`**; never logged. |
| `SEBASTIAN_API_KEY_ENV` | (none) | Name of the variable holding the key for any other `openai`-backend host (e.g. `OPENROUTER_API_KEY`). Unset = no key is sent there. `ANTHROPIC_API_KEY`, `GH_*` and `GITHUB_*` are refused. In `sebastian models`, use a `#ENV_NAME` suffix per spec instead. |
| `SEBASTIAN_MAX_STEPS` | `25` | |
| `SEBASTIAN_MAX_COST` | `0` (off) | USD per run; also `--max-cost`. Needs a price for every model in use. |
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
python -m pip install -e ".[dev,anthropic,watch]"
python -m ruff check . && python -m ruff format --check . && python -m mypy && python -m pytest --cov
```

Use `python -m` so the tools run in the interpreter where `sebastian` is installed; a standalone `pytest`/`mypy` on PATH may not see it.

Maintenance and upgrades follow [`docs/WORK_ORDER.md`](docs/WORK_ORDER.md).

## Licence

MIT. See [`LICENSE`](LICENSE).
