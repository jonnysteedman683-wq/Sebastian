# Scars

Each `*.json` file here is a real Sebastian run that failed, turned into a permanent eval case
with `sebastian scar <run_id> -w <workspace>`. The live eval suite (`python -m sebastian.evals
--live`, and the weekly Live evals workflow) replays every scar, so a fixed failure that comes
back shows up as a failing score.

Scars are live-only: they have no offline script, so offline runs list them as skipped.

## Format

```json
{
  "version": 1,
  "name": "fix-ci-config-1a2b3c4d",
  "task": "the original task text",
  "files": {"path/in/workspace": "seed contents"},
  "expect": {"stop": "completed"},
  "allow_gated": true,
  "origin": {"run_id": "...", "stop": "budget_exhausted", "tool_errors": ["..."]}
}
```

`expect` keys (all optional, all must hold): `stop`, `max_steps`, `files` (exact contents),
`contains` (substring per file), `absent` (paths that must not exist), `answer_contains`,
`unchanged` (workspace untouched), `no_gated_success`.

The generated default only requires the task to complete. **Tighten `expect` before committing**
a scar, so it checks the outcome you actually wanted. Review seed `files` for anything private.
