# Red-team baseline: 2026-11-10 (OFFLINE ONLY)

**This is not a live run.** The environment had no `ANTHROPIC_API_KEY` and no usable bubblewrap,
so neither `sebastian redteam` target could attack a real model. This report records the offline
baseline and what a live run would need. The procedure is in [docs/REDTEAM.md](../docs/REDTEAM.md).

- **Commit:** `486de4a` (main)
- **Python:** 3.13.18, `anthropic` 2.7.0, `DEFAULT_MODEL = claude-sonnet-5-5`
- **Sandbox:** `bwrap_ready() = False`, `checker_sandbox(live=True) = "unavailable"`

## Offline results

```
$ python -m sebastian.evals
...
score 100% (16/16)
injection: 0/5 resisted outright; 5/5 contained

$ python -m sebastian.evals --gauntlet
...
score 100% (18/18)
injection: 0/5 resisted outright; 5/5 contained

$ python -m pytest tests/test_redteam.py tests/test_redteam_critic.py
44 passed
```

"0/5 resisted outright; 5/5 contained" is by design. The scripted injection cases make the
model *attempt* the gated action so the test can show that the approval gate denies it. Offline
runs prove containment, not that a live model resists.

## Live attempt

```
$ SEBASTIAN_HOME=$(mktemp -d) sebastian redteam -n 1 --no-save    # no ANTHROPIC_API_KEY
Traceback (most recent call last):
  ...
  File "src/sebastian/llm/anthropic.py", line 88, in complete
TypeError: "Could not resolve authentication method. Expected one of api_key, auth_token,
or credentials to be set. ..."
(exit 1)
```

`sebastian run` fails the same way. The Anthropic backend lets the SDK's `TypeError` escape
instead of raising `LLMError`, so the CLI's clean error path (`redteam: ...`, exit 2) never
runs. `llm/` is a guarded path, so this is logged as **B77** under Proposed in
`docs/BACKLOG.md` rather than fixed here.

Even with a key, the **agent** target would have printed `not run` for every attack here,
because there is no bubblewrap. The **critic** target needs only a key.

## Next live run (owner)

On a disposable machine with a key and working bubblewrap:

```sh
export ANTHROPIC_API_KEY=... SEBASTIAN_HOME=$(mktemp -d)
sebastian redteam -n 5 2>&1 | tee reports/$(date -u +%F)-redteam-agent.txt
sebastian redteam --target critic -n 5 2>&1 | tee reports/$(date -u +%F)-redteam-critic.txt
```

Then review any new `evals/scars/redteam-*` files (docs/REDTEAM.md § Review scars) and add a row
to the Baselines table.
