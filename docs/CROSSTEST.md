# Cross-testing your agents (`sebastian crosstest`)

Run every agent's test suite against every other agent, and get one table that shows where each
agent is weak. Nothing is written to any repo. Each case runs in a temporary folder that is
deleted afterwards. Fixing what it finds is a separate step that a person starts: an
`evolve` run or a PR, merged by you.

## 1. Write a manifest

`agents.json` (keep it next to your repos; relative `suite` paths are relative to this file):

```json
{
  "version": 1,
  "agents": [
    {
      "name": "sebastian",
      "suite": "Sebastian/evals/crosstest",
      "command": ["sebastian", "run", "--unattended", "--workspace", "{workspace}", "{task}"],
      "timeout": 600
    },
    {
      "name": "samanthar",
      "suite": "Samanthar/evals/crosstest",
      "command": ["samanthar", "run", "{task_file}"],
      "timeout": 600
    }
  ]
}
```

- `command` is an argv list. It never goes through a shell. Placeholders:
  - `{task}`: the task text.
  - `{task_file}`: a file holding the task, outside the workspace.
  - `{workspace}`: the case's temp folder, which is also the working directory.
- Prefer each agent's non-interactive mode (Sebastian: `--unattended`, which denies every
  gated action). Each agent's own approval gate and sandbox still apply.
- `timeout` is per case, in seconds; on timeout the whole process group is killed.

## 2. Write cases

One JSON file per case in the agent's `suite` folder. The format is compatible with Sebastian's
scar files:

```json
{
  "version": 1,
  "name": "add-greeting",
  "task": "Create hello.txt containing exactly: hello",
  "files": {"README.md": "seed files the task starts from"},
  "expect": {
    "files": {"hello.txt": "hello"},
    "contains": {"README.md": "seed"},
    "absent": ["secret.txt"],
    "exit_code": 0
  }
}
```

Only `files`, `contains`, `absent` and `exit_code` can be checked from outside an agent.
Sebastian-only keys (`stop`, `no_gated_*`, ...) are ignored. A case with no checkable key
shows as `unchecked` and is never counted as a pass.

## 3. Run it

```bash
sebastian crosstest --manifest agents.json --dry-run      # list what would run; costs nothing
sebastian crosstest --manifest agents.json --json results.json
sebastian crosstest --manifest agents.json --target samanthar --suite sebastian
```

Output is a target × suite matrix of `passed/checked`, followed by every failure. The exit code
is 0 when every checked case passed, 1 when any failed, errored or timed out, and 2 for a bad
manifest.

## Trust model

- The manifest is your config, so its commands run as written. Don't run someone else's
  manifest.
- A suite's task text becomes the target agent's task. Only list suites you'd be happy to
  type into that agent yourself.
- Cross-testing never proposes, commits, pushes or merges. Agents don't change each other here.
