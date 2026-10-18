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

## Repo watch tooling (`sebastian.watch`)

| Threat | Control | Evidence |
|---|---|---|
| Malicious commit runs code in the watcher | Inventory parses source with `ast` from `git show`; nothing imported or executed | `tests/test_watch.py::test_inventory_never_executes_repo_code` |
| Watcher draft hides risk | `finalize` only raises priority/risk/escalation; computed fields override the draft; unknown draft fields rejected | `test_finalize_never_lowers_watcher_judgement`, `test_finalize_rejects_overreach_and_bad_drafts` |
| Force-push hides history | `history_rewritten` detection adds +3 risk and escalates | `test_history_rewrite_detected_and_escalated` |
| Forged / replayed reports | Keyless (default): reports read only from the `watch-state` branch on GitHub (provenance) + SHA-256 fingerprint (integrity). Push mode: HMAC-SHA256. Both: constant-time compare, `seq` and `from_sha` continuity in `verify` | `test_verify_catches_tampering_seq_and_gaps`, `test_keyless_fingerprint_detects_tampering` |
| Key leakage | Key read from env only, never printed or logged | `test_cli_full_flow` asserts key absent from output |
| Watcher tampered via a merged commit | Changes to `src/sebastian/watch/`, `.github/workflows/watch.yml`, `schemas/` add +3 risk and always escalate | `test_watch_tooling_change_is_flagged_and_escalated` |
| Plaintext endpoints | `Endpoints` rejects non-https URLs (push mode) | `test_endpoints_require_https` |
| Report and state out of step | Report and `state.json` committed together; a failed push writes neither | `.github/workflows/watch.yml` (Publish step) |
| Escalation hidden behind low priority | Schema and `finalize`: `escalate: true` forces `priority: HIGH` | `test_invalid_reports_rejected`, `test_finalize_never_lowers_watcher_judgement` |
| Secrets exposed to forks | Watch workflow triggers only on `main` push, schedule and manual dispatch (never `pull_request`) | `.github/workflows/watch.yml` |

## Residual risks (escalated, not fixed)

1. **`run_command` arguments are not jailed.** An allowlisted `cat /etc/passwd` reads outside the workspace, and allowlisting an interpreter (`python`) is equivalent to arbitrary code. Mitigation today: approval gate (EXEC needs a yes) and an explicit allowlist. Upgrade: run commands in a container or Linux namespace (bubblewrap/nsjail) with no network.
2. **No network egress control** for subprocesses. Same upgrade as above.
3. **Prompt injection is mitigated, not prevented.** Needs a hostile-input eval suite (U12).
4. **RLIMIT_AS** can break some runtimes (e.g. JVM). Tunable via `CommandPolicy.max_memory_bytes`.
5. **The watcher runs from `main`.** Whoever can merge to `main` can change the watcher and its workflow together. Mitigated by always-escalated tooling changes and the coordinator's heartbeat/canary; fully fixed only by branch protection + CODEOWNERS on `.github/`, `src/sebastian/watch/`, `schemas/` (owner action).
6. **Keyless trust depends on GitHub.** Anyone with push rights to `watch-state` could publish a report with a valid fingerprint. Mitigation: readers fetch only from GitHub and treat non-bot commits there as incidents; fully fixed by protecting `watch-state` so only Actions can push (owner action).

## Reporting
Raise a private issue to the owner (Jonny). Never include keys or memory contents.
