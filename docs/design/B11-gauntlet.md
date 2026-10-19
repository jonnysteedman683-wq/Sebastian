# B11 Gauntlet: design for owner review

Status: **draft, awaiting owner sign-off**. Nothing here is built. B11 stays 🔒 until the
owner approves this doc (or an edited version of it) in a PR review.

Source: PROPOSALS #3 ("plan -> predict -> act -> test -> reflect stages, separate critic model,
`pytest` command gates before LLM judgement").

## 1. Goal

Today `Sebastian._drive` (src/sebastian/agent.py) is one loop: optional plan, then
think -> act -> observe until the model stops calling tools. "Done" means "the model said so".

The Gauntlet makes the run prove it is done. Each run passes through stages. A stage either passes
its gate or loops back, inside a hard budget. Deterministic checks (commands such as `pytest`)
run before any LLM judgement. When the run cannot pass, it stops and asks for help instead of
guessing.

Non-goals: no new autonomy. The Gauntlet does not approve, merge, push, schedule or widen
permissions. Every tool call still goes through the approval gate, workspace jail, command
sandbox, budget governor and trace redaction exactly as now.

## 2. Stages

```
PLAN -> PREDICT -> ACT -> VERIFY -> CRITIQUE -> DONE
          ^          ^       |          |
          |          +-------+----------+   (loop back with findings, bounded)
          +-- replan after N failed loops
any stage --(stuck / budget)--> HELP (stop, report)
```

| Stage | What runs | Gate to leave the stage | Exists today? |
|---|---|---|---|
| PLAN | One tool-free call (`_make_plan`) | Plan is non-empty | Yes (B14) |
| PREDICT | One tool-free call: "list what will change and how we will know it worked" | Produces a `<checks>` block: files expected, commands expected to pass | **New** |
| ACT | The current think -> act -> observe loop | The model stops calling tools | Yes |
| VERIFY | Deterministic: run the predicted check commands via `run_command` (same approval + sandbox) and diff the files changed against the prediction | Every check command exits 0 | **New** |
| CRITIQUE | One tool-free call on a *separate* critic model: task, plan, diff, check output. Returns `PASS` or a list of findings | `PASS` | **New** |
| HELP | Stop. Write a structured report: what was tried, which gate failed, last findings | n/a (terminal) | **New** |

VERIFY runs before CRITIQUE on purpose. A failing test is a fact, so it never costs a critic call.

## 3. Looping and the governor

- A failed VERIFY or CRITIQUE sends its findings back into ACT as a user turn, using the same
  mechanism as `REPLAN_NUDGE`.
- `GauntletPolicy(max_loops=3, replan_after=2, critic_llm=None, checks_required=True)`.
- After `replan_after` failed loops, re-enter PLAN with the findings.
- After `max_loops`, go to HELP.
- Every stage call counts against the existing `Governor` (steps, tokens, seconds). Budget
  exhaustion anywhere still ends the run as `budget_exhausted`. The Gauntlet cannot extend a
  budget.
- **Revert option (owner decision, Q3):** before looping, optionally roll the workspace back
  with `Checkpoints.undo` so each attempt starts clean, rather than patching on top of a failed
  attempt.

## 4. Safety analysis

| Risk | Mitigation |
|---|---|
| The model predicts a dangerous "check" command | Check commands go through `run_command` unchanged: approval gate, `CommandPolicy` allowlist, bubblewrap sandbox (B19 default). A denied check counts as a failed gate, never a bypass. |
| A prompt-injected file tells the critic "say PASS" | The critic gets no tools. Its output is parsed strictly (`PASS` or findings). VERIFY has already run, so the critic can block a run but cannot rescue a failing one. Add red-team cases aimed at the critic (B11e). |
| Loops burn budget | Hard `max_loops` plus the existing governor. Tests use `FakeClock`. |
| HELP becomes a nag or escalation path | HELP only stops and reports. It does not open issues, ping anyone or retry on a schedule. |
| Silent behaviour change | Off by default (`--gauntlet` flag). The default flips only after a later owner decision, backed by eval numbers. |

New stop reason: `StopReason.NEEDS_HELP = "needs_help"`. This touches the trace and report
vocabulary, so `schemas/examples/*.json`, `capability_inventory` and the resign script get updated
in the same PR. The guarded schema file `schemas/repo_change_report.schema.json` must not need
changes; if it does, that sub-item becomes 🔒.

## 5. Build plan (one PR each, each independently mergeable)

- **B11a Stage machine + PREDICT.** `gauntlet.py` with `GauntletPolicy`, stage enum and a
  predict call that parses a `<checks>` block (strict and bounded). Not wired into the CLI yet.
  *Done when:* ScriptedLLM tests cover parse/reject of predictions and the stage transitions.
- **B11b VERIFY.** Run predicted checks through `run_command`. Loop back on failure with output
  capped by `ContextPolicy`. *Done when:* a scripted run that breaks a test loops, fixes it and
  passes; a denied check fails the gate.
- **B11c CRITIQUE + separate critic model.** `--critic-model`, defaulting to the planner model.
  Strict verdict parser. *Done when:* a critic `FAIL` loops; malformed critic output counts as
  FAIL; the critic call has no tools.
- **B11d HELP + `needs_help` stop + CLI `--gauntlet`.** Structured help report in the trace and
  answer; dashboard tile for needs-help runs. *Done when:* `max_loops` exhaustion ends as
  `needs_help`; inventory examples re-signed.
- **B11e Evals.** Gauntlet on/off comparison in `python -m sebastian.evals`, plus red-team
  attacks aimed at the critic. *Done when:* the eval report shows both modes side by side.

## 6. Open questions for the owner

1. **Critic model:** same model as the actor, the planner model, or always a different provider
   tier?
2. **Checks when the model predicts none:** fail the gate (strict) or skip VERIFY (lenient)?
   The proposed default is strict for code tasks only, meaning any `.py` file changed.
3. **Revert between loops** (`Checkpoints.undo`) or patch forward?
4. **Default on?** Proposed: off until B11e shows a pass-rate gain with no resistance loss.
5. **Evolve:** should `sebastian evolve` run its inner agent under the Gauntlet once B11d lands?
   Proposed yes, as a separate item, because evolve already has outer gates.

Approving this doc unlocks B11a–B11e as normal (unlocked) backlog items, with any edits you make.
