# Security & Threat Model

## Assets
Host filesystem outside the workspace, host secrets/env, the API key, stored memory, and anything reachable over the network.

## Threats and controls

| Threat | Control | Evidence |
|---|---|---|
| Path escape (`..`, absolute, symlink) | `Workspace.resolve` resolves symlinks, rejects anything outside root | `tests/test_tools.py::test_workspace_rejects_*` |
| Shell injection | No shell; `shlex.split` + exec of allowlisted basename only | `test_command_no_shell_injection`, `test_command_rejections` |
| Env/secret leakage to subprocess | Env scrubbed to `PATH`, `LANG`, `LC_ALL`; `HOME` = workspace | `test_command_runs_in_workspace_with_scrubbed_env` |
| Runaway processes | Timeout + process-group SIGKILL; RLIMIT_CPU, RLIMIT_AS | `test_command_timeout_kills` |
| Unapproved destructive actions | `ApprovalGate`: WRITE and above need approval | `test_approval_denied_is_reported_not_executed` |
| Unattended bypass | Non-TTY / `--unattended` uses `DenyApprover`. No bypass flag exists. | `test_run_non_tty_denies_writes_and_traces` |
| Prompt injection via tool output / memory | System prompt marks them as data; memory wrapped in `<recalled_memory>` | Prompt review (behavioural evals: proposal U12) |
| Runaway cost / infinite loop | Governor: steps, tokens, wall-clock | `test_step_budget_stops_run`, `test_token_and_time_budgets` |
| Repeat-failure loops | Identical failed call is blocked within a run | `test_repeated_failed_call_is_blocked` |
| Secrets in logs | `trace.redact` on keys and token-shaped values | `test_redaction`, `test_jsonl_tracer` |
| Corrupt / tampered memory | Schema-validated load, raises instead of wiping; atomic writes | `test_memory_corruption_raises` |
| Untrusted deserialisation | JSON only. No pickle/eval anywhere. | `grep -rn "pickle\|eval(" src` is empty |

## Residual risks (escalated, not fixed)

1. **`run_command` arguments are not jailed.** An allowlisted `cat /etc/passwd` reads outside the workspace, and allowlisting an interpreter (`python`) is equivalent to arbitrary code. Mitigation today: approval gate (EXEC needs a yes) and an explicit allowlist. Upgrade: run commands in a container or Linux namespace (bubblewrap/nsjail) with no network.
2. **No network egress control** for subprocesses. Same upgrade as above.
3. **Prompt injection is mitigated, not prevented.** Needs a hostile-input eval suite (U12).
4. **RLIMIT_AS** can break some runtimes (e.g. JVM). Tunable via `CommandPolicy.max_memory_bytes`.

## Reporting
Raise a private issue to the owner (Jonny). Never include keys or memory contents.
