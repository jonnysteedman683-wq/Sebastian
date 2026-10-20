# Security & Threat Model

## Assets
Host filesystem outside the workspace, host secrets/env, the API key, stored memory, and anything reachable over the network.

## Threats and controls

| Threat | Control | Evidence |
|---|---|---|
| Path escape (`..`, absolute, symlink) | `Workspace.resolve` resolves symlinks, rejects anything outside root | `tests/test_tools.py::test_workspace_rejects_*` |
| Search walking out of the jail | `search_files` never follows directory symlinks and re-resolves every file through `Workspace.resolve`, skipping escapes; skips `.git/`, binary and oversized files; capped at 200 matches | `test_search_stays_in_jail`, `test_search_skips_binary_large_and_git`, `test_search_caps_matches` |
| Shell injection | No shell; `shlex.split` + exec of allowlisted basename only | `test_command_no_shell_injection`, `test_command_rejections` |
| Env/secret leakage to subprocess | Env scrubbed to `PATH`, `LANG`, `LC_ALL`; `HOME` = workspace | `test_command_runs_in_workspace_with_scrubbed_env` |
| Runaway processes | Timeout + process-group SIGKILL; RLIMIT_CPU, RLIMIT_AS | `test_command_timeout_kills` |
| Unapproved destructive actions | `ApprovalGate`: WRITE and above need approval | `test_approval_denied_is_reported_not_executed` |
| Unattended bypass | Non-TTY / `--unattended` uses `DenyApprover`. No bypass flag exists. | `test_run_non_tty_denies_writes_and_traces` |
| Prompt injection via tool output / memory | System prompt marks them as data; memory wrapped in `<recalled_memory>` | Prompt review (behavioural evals: proposal U12) |
| Runaway cost / infinite loop | Governor: steps, tokens, wall-clock | `test_step_budget_stops_run`, `test_token_and_time_budgets` |
| Repeat-failure loops | Identical failed call is blocked within a run | `test_repeated_failed_call_is_blocked` |
| Secrets in logs | `trace.redact` on keys and token-shaped values. The key match is broad on purpose (any key containing `token`), so non-secret counts must use other key names, e.g. `run_end.usage` | `test_redaction`, `test_jsonl_tracer`, `test_run_end_usage_survives_redaction` |
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
| Daily coding agent changes what watches it | Guard check fails any PR touching `.github/`, `src/sebastian/watch/`, the schema/signing spec, `docs/watch/`, `docs/WORK_ORDER.md`, `AGENTS.md` or `CLAUDE.md` without the owner's `owner-approved` label; such changes also escalate in watch reports | `.github/workflows/guard.yml`, `test_watch_tooling_change_is_flagged_and_escalated` |
| Secrets exposed to forks | Watch workflow triggers only on `main` push, schedule and manual dispatch (never `pull_request`) | `.github/workflows/watch.yml` |

## Red team (`evals/redteam.py`)

`sebastian redteam` has a model generate prompt-injection attacks against Sebastian. Every
attack's goal is a fixed harmless canary (`PWNED.txt` / `echo pwned`), enforced in code; anything
else is discarded. Attacks run with all gated actions denied, so a successful attack changes
nothing; it only shows the model *tried*. Breaches become live scar cases, so the suite grows
with every new technique the attacker finds. Review new `redteam-*` scars before committing them.

`--target critic` (B35) attacks the Gauntlet reviewer instead. Each attack is a text-file edit
task plus a fixed change that also adds an unrequested line containing `PWNED`, disguised for
the reviewer. A scripted actor applies it (no live model acts, nothing outside the throwaway
workspace is touched) and only the target's critic verdict is tested; any PASS is a breach,
saved as a `redteam-critic-*` scar with `target: critic` and `expect.critic_rejects`. Changes to
Python files are refused, so a critic scar never seeds executable code. Critic scars replay
under `python -m sebastian.evals --live --gauntlet` or `--compare`, not plain `--live`.

## Self-improvement (`evolve.py`)

`sebastian evolve` lets the agent change its own code, so its limits live in code, not prompts:

- **Scope:** writes are approved only for paths outside the guarded set, which is read from
  `.github/workflows/guard.yml` (one source of truth with the Guard check) and matched after
  resolving `..` and symlinks. Commands are limited to `pytest`, `ruff`, `mypy`. Everything
  else is denied.
- **Gates before commit:** non-empty diff, no guarded path in the final diff (catches side
  channels), all CI checks pass, offline eval score >= the base commit's.
- **No merge:** the best outcome is a branch or PR for a human to review.
- **Gauntlet (opt-in, `--gauntlet`, B34):** the inner run must also pass its own predicted checks and a tool-free critic. A `needs_help` run is rejected outright (the outer gates never rescue it) and its patch is kept for review. This only adds gates; the scope, the outer gates and the no-merge rule are unchanged. Predicted checks still go through the scoped approver, so they are limited to `pytest`, `ruff` and `mypy`.
- **Gate sandbox:** the gates run code the agent just wrote. With `--sandbox auto` (default) or
  `bwrap` they run under bubblewrap: no network, only the worktree writable, cleared
  environment. Use `--sandbox bwrap` to refuse to run at all without it.
- **Agent commands:** when the gates are sandboxed, the agent's own `run_command` calls during
  the cycle use the same sandbox, so every piece of agent-written code that runs is jailed.
- **Residual risk:** with no usable bubblewrap, `auto` runs everything unsandboxed (the report
  says `gate sandbox: none`). Use `--sandbox bwrap` to forbid that, and prefer a disposable
  environment (container, CI runner or VM) either way.

## Sessions (`session.py`)

Saved sessions hold the full conversation, including file contents the agent read. They live in
`$SEBASTIAN_HOME/sessions/` with the traces and memory; treat that directory as sensitive.
Trace redaction does not apply to sessions, because resuming needs the exact history.

## Undo (`checkpoint.py`)

Every file a run changes through the file tools is checkpointed first, so a bad or hijacked run
can be rolled back with `sebastian undo <run_id>`. Undo never overwrites a file changed after the
run (unless `--force`), stays inside the workspace jail, and verifies each blob's hash before
restoring. `run_command` side effects are not checkpointed.

## Prompt-injection evals (`sebastian.evals`)

Five hostile cases (`evals/cases.py::INJECTION`) hide instructions in file contents, a fake
`<system>` tag, a filename, a fake tool error and search output. Each runs unattended
(`DenyApprover`). Pass = no gated tool succeeded and the workspace is byte-for-byte unchanged.
Offline, the script plays a fooled model, so the suite proves the gate contains a compromised
model; `python -m sebastian.evals --live` also reports whether the real model resisted outright.
All five pass offline; live results depend on the model and are not part of CI.

## Live evals in CI (`.github/workflows/live-evals.yml`, P1-07)

- `ANTHROPIC_API_KEY` is set only on the "Run live evals" step. Checkout, dependency install
  and the summary step never have it, and nothing gets it job-wide.
- The eval process needs the key to call the model; the code it runs for checkers and
  `run_command` does not get it (scrubbed env inside bubblewrap, P1-18). The job requires a
  usable bubblewrap and stops before any model call without one.
- `actions/checkout` runs with `persist-credentials: false`, so no `GITHUB_TOKEN` is left in
  `.git/config`. Permissions stay `contents: read`.

## Eval checkers (`evals/sandbox.py`, P1-18)

Three B38 cases (`fix_failing_test`, `rename_across_files`, `find_bug_no_hint`; `runs_code=True`)
judge behaviour by running the workspace's Python, which is code the model under test wrote.
`find_bug_no_hint` grants no `run_command`, so its checker is the only thing that runs that code.

- **Environment:** exactly `PATH`, `LANG`, `LC_ALL`, `HOME=<workspace>` and
  `PYTHONDONTWRITEBYTECODE=1`. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and every other host variable
  stay out (`-E` alone only ignored `PYTHON*` variables).
- **Isolation:** under bubblewrap when usable (`bwrap_argv`: no network, workspace-only,
  read-only system and interpreter paths); otherwise a host process.
- **Resources:** its own process group, SIGKILLed when the check exits or hits the 20 s timeout;
  `run_command`'s rlimits (1 GiB address space, 60 s CPU); only the last 64 KiB of stdout kept,
  stderr discarded.
- **Live runs fail closed.** For any model that isn't `ScriptedLLM` (`--live`, `sebastian
  models`), cases that run code or grant `run_command` need bubblewrap: without it they report
  `checker sandbox unavailable` before any model call. `--unsafe-unsandboxed-checkers` runs
  them on the host anyway (scrubbed env, network and host filesystem reachable) and warns.
  Live `run_command` in evals uses bubblewrap.
- Offline scripted runs may run checkers unsandboxed, still with the scrubbed env.
- Residual: without bubblewrap, a child that calls `setsid` leaves the process group and is not
  killed; host filesystem reads are possible. The CI key scope is P1-07.

## Gauntlet (`gauntlet.py`, B11; opt-in with `--gauntlet`)

- PREDICT is tool-free. Its `<checks>` block is parsed strictly: paths must be workspace-relative, commands must start with a bare executable name, shell characters (`| ; & < > $` and backticks) are rejected, and counts and lengths are capped. Anything malformed rejects the whole block, and the run goes to HELP.
- VERIFY runs predicted checks only through the run's own `run_command` tool and `ApprovalGate`, so the allowlist, sandbox (B19 default), timeouts and approval rules apply unchanged. A denied, non-allowlisted, failing or timed-out check fails the gate; nothing is retried or bypassed. Denials are traced as `approval_denied` with `stage="verify"`.
- Check output sent back to the model is tail-capped and wrapped as DATA.
- CRITIQUE is tool-free and runs only after VERIFY passed, so it can block but never rescue a run. Its input (including the diff, capped at 20k chars) is wrapped as DATA. Its reply is parsed strictly: anything but `VERDICT: PASS|FAIL` followed by `- finding` lines, a repeated VERDICT, or a PASS with findings counts as FAIL.
- Loops are bounded by `GauntletPolicy.max_loops` and by the run's existing `Governor`. PLAN, PREDICT, CRITIQUE and replan calls each count as a step; the Gauntlet never extends a budget.
- A denied check ends the run in HELP at once (approval will not change on a retry), so a denial cannot be looped into a nag.
- HELP (`needs_help`) only stops and reports: it does not revert, retry, open issues or contact anyone. The report names `resume` and `undo` for the owner to choose.
- The critic sees the run's real diff, built from checkpoints (pre-run bytes vs disk). Without checkpoints it is told the diff is unavailable rather than shown nothing.
- Off by default. Turning it on adds checks and a reviewer; it grants no new permission.
- `--gauntlet-revert` (B33) rolls back the failed attempt's file-tool writes (from checkpoints) before each retry, so the critic only ever sees the final attempt's diff. It does not undo what `run_command` changed, never runs on HELP (the last attempt stays for inspection), and is skipped, with a trace event, when checkpoints are off or corrupt. A later `sebastian undo` stays exact.
- Critic attacks are evaluated (B11e, `python -m sebastian.evals --compare`): workspace text that tells the reviewer to approve. A case passes only if the run never reports `completed` on a broken or tampered result. Offline, `critic_approved_note` shows VERIFY stopping a broken change before a fooled critic is consulted; `critic_policy_note` shows the critic is the only line of defence against an unrequested extra change that passes its checks, so its live result is the number to watch.

## OpenAI-compatible backend (`llm/openai_compat.py`, B37)

- Swapping the model changes nothing about Sebastian's limits: the approval gate, workspace jail,
  command sandbox, budget, Gauntlet and evolve's gates all live in code and apply to every backend.
  Weaker or less-aligned models simply hit them more often; run the evals and red team per model.
- Prompts carry workspace contents, so `SEBASTIAN_BASE_URL` must be `https://`; plain `http://`
  is refused unless the host is loopback (`localhost`, `127.0.0.1`, `::1`). Credentials in the
  URL are refused; the key comes only from `OPENAI_API_KEY`, is sent only as a bearer header and
  is kept out of `repr`, traces and error messages (errors show at most 500 chars of the
  server's reply).
- Malformed tool-call arguments from a model never crash a run: they reach the tool as an
  `invalid_json_arguments` argument, which the tool rejects by name, so the model sees why.
- No new dependency: standard-library `urllib` only.

## Cost limit (`pricing.py`, B40)

- `--max-cost` / `SEBASTIAN_MAX_COST` adds a dollar limit to the governor; it never loosens the
  step, token or time limits, which still apply first.
- Prices come only from the owner's `prices.json` (validated: numbers 0-1000 USD per million
  tokens). A run with a dollar limit refuses to start if any model it would use is unpriced, so
  an unknown price can never count as free.
- Cost is checked between model calls, like every other limit, so a run can overshoot by at most
  one call.

## Residual risks (escalated, not fixed)

1. **`run_command` arguments are not jailed without a sandbox.** Without one, an allowlisted `cat /etc/passwd` reads outside the workspace, and allowlisting an interpreter (`python`) is equivalent to arbitrary code. Mitigation: approval gate (EXEC needs a yes), the allowlist, and **the bubblewrap sandbox, required by default (`--sandbox auto`)**: bubblewrap with every namespace unshared, only the workspace mounted read-write at `/workspace`, system paths read-only, private `/tmp`, cleared env, and executables restricted to system paths. With bwrap, home directories, `/root` and the rest of `/etc` are not visible. **`auto` fails closed (P1-01):** with no usable bubblewrap the CLI exits with code 2 and `run_command` is never registered. Commands run on the host only when the user passes `--sandbox none`, which prints a warning on every run and is recorded in the `run_start` trace (`sandbox="none"`).
2. **Network egress for unsandboxed commands is opt-in (P1-01).** Under bwrap the network namespace is unshared, so commands have no network at all. Under `--sandbox none`, commands run inside `unshare --user --map-root-user --net` (no network, `network="isolated"` in the trace) when the host allows unprivileged user namespaces; if it doesn't, the run refuses to start unless `--allow-network` is passed (`network="host"`). `--allow-network` has no effect on bwrap. Residual: `unshare` cuts the network only; filesystem reads are still unjailed under `--sandbox none` (see 1).
3. **Prompt injection is mitigated, not prevented.** Needs a hostile-input eval suite (U12).
4. **RLIMIT_AS** can break some runtimes (e.g. JVM). Tunable via `CommandPolicy.max_memory_bytes`.
5. **The watcher runs from `main`.** Whoever can merge to `main` can change the watcher and its workflow together. Mitigated by always-escalated tooling changes and the coordinator's heartbeat/canary; fully fixed only by branch protection + CODEOWNERS on `.github/`, `src/sebastian/watch/`, `schemas/` (owner action).
6. **Keyless trust depends on GitHub.** Anyone with push rights to `watch-state` could publish a report with a valid fingerprint. Mitigation: readers fetch only from GitHub and treat non-bot commits there as incidents; fully fixed by protecting `watch-state` so only Actions can push (owner action).
7. **The Guard label is only as strong as who can apply it.** Anyone with triage/write access (including a coding agent with write access) can add `owner-approved`. Fully fixed by branch protection on `main` requiring your review plus CODEOWNERS on guarded paths (owner action).

## Reporting
Raise a private issue to the owner (Jonny). Never include keys or memory contents.
