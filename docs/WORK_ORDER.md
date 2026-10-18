# WORK ORDER — Sebastian: Upgrade & Maintenance

**Issued to:** Autonomous coding agent
**Target:** `Sebastian` (AI agent codebase)
**Owner / escalation:** Jonny
**Mode:** Autonomous, gated. Every phase must pass its exit gate before the next begins.

---

## 0. Prime Directives (non-negotiable)

1. **Do no harm.** Never break `main`. All work on branches; merge only when every gate is green.
2. **Evidence over claims.** Never report something as done, fixed, or verified without pointing to the exact file, line, command and output that proves it.
3. **Never invent.** No guessed APIs, model names, config keys, field names or package versions. Look them up in the code, lockfile or official docs. If you can't confirm it, flag it.
4. **Small, reversible steps.** One concern per commit. Every change must be revertible on its own.
5. **Preserve behaviour unless told otherwise.** Refactors must be behaviour-neutral and proven so by tests.
6. **Stop and escalate** (see §9) rather than guess on anything irreversible or ambiguous.
7. **No secrets in code, logs, commits or reports.** Ever.

---

## 1. Phase 1 — Discovery (read-only)

Make zero changes. Produce `docs/SEBASTIAN_MAP.md` containing:

- **Purpose:** what Sebastian does, in one paragraph, derived from code (not README alone).
- **Architecture:** entry points, agent loop, tool layer, memory/state, LLM backend(s), schedulers, servers/APIs. Include a Mermaid diagram.
- **Dependency inventory:** every direct dependency, pinned version, latest version, licence, known CVEs.
- **Model/provider usage:** every model string and endpoint in the code, verified valid against current provider docs.
- **Config & secrets surface:** every env var / config key, where it's read, default value, whether it's sensitive.
- **Test inventory:** framework, count, pass/fail status, coverage %, untested modules.
- **Runtime check:** does it install clean, start, and complete one representative task? Record exact commands.
- **Debt register:** TODOs, FIXMEs, dead code, duplicated logic, stale docs, inconsistent naming.

**Exit gate:** map exists; baseline test run recorded; baseline commit hash recorded.

---

## 2. Phase 2 — Stabilise (fix what's broken)

Priority order:

1. Build/install failures.
2. Failing or flaky tests (fix root cause; never delete or skip a test to go green without escalating).
3. Invalid/deprecated model strings, endpoints or SDK calls.
4. Runtime crashes and unhandled exceptions on the main path.
5. Broken or stale docs that would mislead a user.

Each fix: regression test first (red), fix (green), commit.

**Exit gate:** clean install; 100% of pre-existing valid tests pass; one end-to-end task succeeds.

---

## 3. Phase 3 — Security Hardening

Audit and fix, highest risk first:

- **Execution surface:** any shell/code-exec tool must be sandboxed (workspace jail, path-escape incl. symlinks, scrubbed env, CPU/mem rlimits, timeouts with process-group kill, command allowlist).
- **Approval gating:** destructive or external actions require approval; **scheduled/unattended runs must not bypass it**.
- **Server/API:** auth mandatory (not optional), CORS locked to known origins, rate limiting, no open endpoints that reach tools.
- **Prompt injection:** content from web, files, tool output and memory is treated as data, never instructions. Add tests with hostile inputs.
- **Secrets:** loaded from env/secret store only; redacted in logs and traces.
- **Dependencies:** resolve all known CVEs; pin with hashes.
- **Data:** memory/state stores validated on read; no pickle/eval of untrusted data.

**Exit gate:** written threat model in `docs/SECURITY.md`; every finding fixed or explicitly escalated; security tests added.

---

## 4. Phase 4 — Quality Baseline

- Type hints throughout; `mypy --strict` (or `pyright strict`) passes.
- Lint + format: `ruff check` and `ruff format` clean.
- Test coverage ≥ 90% on core loop, tools and memory; ≥ 80% overall.
- Deterministic tests: injectable clock and RNG seams; no real network or LLM in unit tests (use fakes/recorded fixtures).
- CI pipeline: lint → type-check → test across supported Python versions → coverage gate → security scan. Fails the build on any regression.
- Structured logging with run IDs and trace IDs.

**Exit gate:** CI green on all matrix targets; coverage thresholds met.

---

## 5. Phase 5 — Upgrades (capability)

Only after Phases 1–4 are green. Each upgrade: proposal → implement behind a flag → tests → benchmark vs baseline → enable.

Candidate upgrades (validate each against what Sebastian actually is; skip any that don't fit):

| # | Upgrade | Why |
|---|---|---|
| U1 | Separate producer and critic models | Self-grading is lenient; split roles for trustworthy gates |
| U2 | Gated stage loop (plan → predict → act → test → reflect) with revert-on-failure | Catches bad plans early; predictions expose wrong understanding |
| U3 | Global budget + governor (tokens, steps, wall-clock, cost) | Guaranteed termination and cost control |
| U4 | Tool results as evidence; command gates (e.g. `pytest`) before LLM judgement | Grounds success in real outputs, not prose |
| U5 | Embedding-based memory retrieval with decay and dedup | Better recall than keyword/TF-IDF |
| U6 | Tabu memory of failed attempts | Stops repeating the same wrong answer |
| U7 | Retry with exponential backoff + jitter; provider failover | Resilience to API errors |
| U8 | Prompt caching and context compaction | Lower cost and latency on long runs |
| U9 | Trace viewer / replay of any run | Debuggability |
| U10 | Async, parallel tool calls where independent | Throughput |
| U11 | Pluggable backend (provider-agnostic interface) | No vendor lock-in |
| U12 | Eval harness: fixed task suite, scored, run in CI | Every change proven better or reverted |

**Exit gate per upgrade:** eval score ≥ baseline; no cost/latency regression > 10% unless justified; tests and docs added.

---

## 6. Phase 6 — Ongoing Maintenance (recurring)

Run this loop on a schedule (suggested weekly) and on every dependency/provider change:

1. Pull latest; run full CI and eval suite.
2. Check dependencies for updates and CVEs; upgrade in a branch, run everything, merge if green.
3. Re-verify every model string/endpoint against current provider docs; flag deprecations with dates.
4. Review logs/traces since last run for errors, retries, timeouts, cost spikes; open fixes.
5. Prune: dead code, stale flags (enabled > 30 days → make permanent, remove flag), outdated docs.
6. Update `CHANGELOG.md` and bump version (SemVer).
7. Post the maintenance report (§8).

---

## 7. Working Rules

- **Branching:** `sebastian/<phase>-<short-desc>`. Never commit directly to `main`.
- **Commits:** Conventional Commits (`fix:`, `feat:`, `refactor:`, `test:`, `docs:`, `chore:`, `sec:`).
- **Per change loop:** plan → predict expected outcome → implement → run tests → compare to prediction → commit or revert. Log any surprise.
- **Retry discipline:** max 3 attempts per problem, each must differ from the last. Then escalate.
- **Scope control:** anything outside this order goes in `docs/PROPOSALS.md`, not into code.
- **Docs move with code.** README, architecture map and CHANGELOG updated in the same PR as the change.

---

## 8. Reporting

After each phase, write to `reports/<date>-<phase>.md`:

- What changed (commit hashes)
- Evidence: commands run + summarised output (test counts, coverage, eval scores, before → after)
- Open issues and risks
- Escalations awaiting decision
- Recommended next action

Keep it short. No narrative of effort — results and proof only.

---

## 9. Escalate to Jonny (stop and wait) when

- A change is irreversible (data migrations, deleting stored memory/state, force-push, rotating keys).
- A fix requires changing Sebastian's public behaviour, API or prompts in a user-visible way.
- A test must be deleted or skipped.
- A security issue can't be fully fixed.
- Requirements are ambiguous or contradict each other.
- Cost or permission scope would increase (new paid service, broader tool permissions, network access).
- The retry budget (§7) is exhausted.

---

## 10. Definition of Done

- [ ] Discovery map, security doc, threat model present and accurate
- [ ] CI green across matrix; coverage thresholds met; `mypy` and `ruff` clean
- [ ] Zero known CVEs; all model strings verified current
- [ ] Eval suite exists, runs in CI, scores ≥ baseline
- [ ] Every accepted upgrade shipped behind a tested flag with docs
- [ ] CHANGELOG updated; version bumped; release tagged
- [ ] Final report filed with evidence and open-risk list
