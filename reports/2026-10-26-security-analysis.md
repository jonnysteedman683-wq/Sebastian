# Sebastian security analysis — 2026-10-26

Scope: static review of the safety TCB (`workspace`, `approval`, `tools/builtin`, `evolve`,
`trace`, `memory`, `config`, HTML renderers) plus one empirical check. Read-only; no repo code
changed. Findings ranked most to least severe.

## Summary

The jail, approval gate, command sandbox, redaction and HTML escaping are strong and well
tested. One real gap in a *stated* control (P1-05 git hardening) is confirmed below, plus two
defence-in-depth notes. Nothing here is a break of the approval gate or a path escape.

---

## F1 — Agent can plant a non-`.git` git repo that runs host code (Medium)

**Control it weakens:** P1-05. `docs/SECURITY.md` states "no file tool reads or writes git
config, hooks or the worktree pointer … a rewritten `.git` file, config or hook would run code
the next time the host (outside any sandbox) runs git here."

**Gap:** `Workspace.resolve` enforces that by rejecting any path with a component named `.git`
(`src/sebastian/tools/workspace.py:31`). But git does not require the name `.git`. A directory
holding `HEAD`, `config`, `objects/`, `refs/` is a valid git repository under any name. When a
human later runs a git command with that directory as the current/discovered git dir, git reads
its `config` and will execute a `core.fsmonitor` program or a `core.hooksPath` hook — on the
host, outside any sandbox, with the user's full filesystem and network.

The write tools let the agent create exactly such a tree anywhere in the workspace except under
a literal `.git`. Nothing in `resolve` inspects file *content* or recognises a repo laid out
under another name.

**Confirmed empirically** (PoC run in a throwaway scratchpad dir, then deleted): the file tools
accepted writes creating a repo layout under a non-`.git` directory; a `core.fsmonitor` value in
its `config` executed a marker command the next time `git status` ran with that dir as the git
dir. The marker file was created — host code ran.

**Preconditions / likelihood:** the agent must write the tree, and a human (or later tooling)
must invoke git from inside that agent-authored directory. That is narrower than root-level
`.git` tampering (the repo root's `.git` is already protected), so Medium, not High. But it is a
real way around a control the threat model claims is complete.

**Fix options (TCB change — needs an owner-approved backlog item):**
- In `Workspace.resolve`, reject writes to the reserved git entry names (`HEAD`, `config`,
  `objects`, `refs`, `hooks`, `packed-refs`, `FETCH_HEAD`) when they would land directly in a
  directory — i.e. refuse to let the agent assemble a repo skeleton; or
- more simply, when any tool writes a file named `config`/`HEAD`/etc., refuse. This is blunt but
  matches the existing "no git metadata, ever" stance; or
- document the residual risk explicitly: Sebastian's jail protects the *host from the agent*,
  but a human who runs git inside agent-written files inherits that content. Pair with the host
  hardening evolve already uses (`GIT_CONFIG_NOSYSTEM=1`, `core.fsmonitor=`,
  `core.hooksPath=/dev/null`) as guidance for anyone running git over a Sebastian workspace.

A new test under `tests/` (adding, not weakening) should assert the chosen rejection.

---

## F2 — `watch.gitio` runs git without the P1-05 hardening flags (Low, defence-in-depth)

`src/sebastian/watch/gitio.py:36-45,63` invoke `git -C <repo> …` with no `GIT_CONFIG_NOSYSTEM`,
no `-c core.fsmonitor=`, no `-c core.hooksPath=/dev/null` — the exact flags `evolve.py:233-239`
adds precisely because host git over an untrusted tree can run code. The watcher inventory never
executes repo code (it `ast`-parses `git show` output), and the plumbing used here (`rev-parse`,
`merge-base --is-ancestor`, `show`) does not fire hooks, so this is not currently exploitable.
But the watcher runs on `main` in CI against whatever is in the tree, and a future command added
to this class (e.g. anything touching the worktree or an fsmonitor-enabled config) would run
unhardened. Apply the same `GIT_HARDENING` constant and `GIT_CONFIG_NOSYSTEM=1` env here so the
property holds by construction, not by the current command list.

---

## F3 — Redaction is heuristic and the home dir is the real boundary (Low, already documented)

`trace.redact` and session redaction are good, but entropy/prefix heuristics will miss a secret
that looks like prose, and `memory` summaries (`agent.py:615`, `memory.py:146`) are stored
through the same redactor — a secret an agent echoes into its final answer is redacted on the
way to disk, but the home dir must still be treated as sensitive. `SECURITY.md` already says
this; no change needed beyond keeping `$SEBASTIAN_HOME` at `0700` (enforced by
`config.ensure_home`, `config.py:85`). Noted so it isn't mistaken for a guarantee.

---

## Checked and clean

- **Path escape:** absolute paths, `..`, and symlinks (incl. symlink-into-jail) are rejected;
  `resolve_strict` refuses any symlink component for checkpoint-rewind and the critic diff
  (P1-09). Search never follows directory symlinks and re-resolves each file.
- **Command exec:** no shell anywhere; `shlex` + allowlisted basename only; env scrubbed to
  `PATH`/`LANG`/`LC_ALL` + `HOME`=workspace; rlimits + process-group SIGKILL; bubblewrap fails
  closed (`resolve_command_sandbox`). `--sandbox none` still cuts the network via `unshare`
  unless `--allow-network`.
- **Approval:** `describe_call` shows argv/diff/content in full with control+bidi escaping,
  refuses >32 KiB rather than truncating; one `parse_command` for approve/key/execute (P1-06);
  no bypass flag; non-TTY ⇒ `DenyApprover`.
- **evolve `ScopedApprover`:** writes only outside the guard regex (read from `guard.yml`,
  matched after resolving `..`/symlinks), `.gitattributes`/`.gitmodules` refused, commands
  limited to pytest/ruff/mypy; host git hardened (F1 fix would complement this).
- **HTML:** `timeline.py` and `dashboard.py` route every model/tool/user value through
  `html.escape(…, quote=True)`; numeric fields are typed ints/floats. No injection seen.
- **Deserialisation:** JSON only; `grep` for `pickle`/`eval(`/`exec(`/`os.system`/`shell=True`/
  `yaml.load(` finds nothing in the TCB.
- **openai_compat (P1-19):** key attached only to `api.openai.com`; redirects refused; URL
  creds rejected; other hosts need an explicitly named env var.

## Could not test here

Live red-team (`sebastian redteam`) and the Gauntlet critic attacks need `ANTHROPIC_API_KEY`
and bubblewrap, neither present in this container. Running them would exercise the gate against
generated prompt injections — recommended before relying on the model-behaviour controls.
