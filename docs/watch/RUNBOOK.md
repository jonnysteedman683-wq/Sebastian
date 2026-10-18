# Watch pipeline runbook

`.github/workflows/watch.yml` watches this repo and publishes signed-by-fingerprint reports. **No keys, tokens or endpoints are needed.** Bots read the reports straight from GitHub.

```
push to main / hourly / daily digest / weekly canary
        │
        ▼
 sebastian-watch run ── scan (git + ast, repo code never executed)
        │               mechanical draft from computed facts
        │               finalize: rules, schema, SHA-256 fingerprint
        ▼
 one commit to the `watch-state` branch:  latest.json  reports/NNNNNN.json  state.json
        │
        ▼
 GROK-WATCHER and CO-ORDINATOR-BOT read latest.json (public raw URL, no auth)
```

## Where the reports are

| What | URL |
|---|---|
| Newest report | `https://raw.githubusercontent.com/jonnysteedman683-wq/Sebastian/watch-state/latest.json` |
| Report by number | `https://raw.githubusercontent.com/jonnysteedman683-wq/Sebastian/watch-state/reports/000042.json` |
| Watcher position | `https://raw.githubusercontent.com/jonnysteedman683-wq/Sebastian/watch-state/state.json` |
| History (who wrote what, when) | `https://github.com/jonnysteedman683-wq/Sebastian/commits/watch-state` |

Raw URLs can lag a few minutes behind a new commit (CDN cache). Readers poll; they don't need to be instant.

## Schedule (UTC cron → Australia/Brisbane)

| Trigger | Brisbane | Mode |
|---|---|---|
| push to `main` | immediately | `change` |
| `7 * * * *` | hourly at :07 | `change` (quiet if nothing new: no commit) |
| `0 22 * * *` | 08:00 daily | `digest` (a change report instead if anything is pending) |
| `15 22 * * 0` | Monday 08:15 | `canary` |

## Setup

None. The first run starts from the previous commit, so its report covers only the latest change. To start from a specific commit, run the workflow manually (Actions → Watch → Run workflow) with `baseline_sha` before any state exists.

Recommended (owner, one-off): protect the `watch-state` branch so only GitHub Actions can push to it, and require review on `.github/`, `src/sebastian/watch/` and `schemas/` (branch protection + CODEOWNERS).

## How readers trust a report

1. **Fetch only from GitHub** over HTTPS, from the URLs above. Never accept a report forwarded by anyone else as authoritative.
2. **Check the fingerprint:** `sebastian-watch verify latest.json --expect-seq <last_seq+1> --expect-from <last_to_sha>` (no key set → keyless check). It also checks the schema, `seq` and commit continuity.
3. **Provenance** comes from GitHub: only the workflow writes `watch-state`, and every commit is visible in its history. A commit there by anyone other than `sebastian-watch[bot]` is itself an incident.

The fingerprint proves the report wasn't altered after it was built; GitHub proves who built it. Together they replace the shared key.

## State

`state.json` on `watch-state`:

| Field | Meaning |
|---|---|
| `last_reported_sha` | Last commit covered by a published report |
| `seq` | Last sequence number published |
| `last_run_at` | Time of last publish |
| `grok_failures`, `delivery_failures`, `coordinator_down_since` | Used only in optional push mode |

Report and state are written in the **same commit**, so they can't disagree. If the push fails, neither lands and the next run retries the same range.

## Operations

| Situation | Action |
|---|---|
| Check what would be published | Run workflow with `dry_run` ticked; download the `watch-report-*` artifact |
| Force a canary | Run workflow with `mode = canary` |
| Reset after an incident | Edit `watch-state/state.json`: set `last_reported_sha` to the last commit you trust; never lower `seq` |
| Watcher looks tampered with | Changes to `src/sebastian/watch/`, `.github/workflows/watch.yml` or `schemas/` always add +3 risk, escalate and are HIGH. Read that report before trusting later ones. |

## Optional: push mode (keys)

Only if a bot can't poll. Set repo secrets and the workflow pushes each report instead of publishing it to the branch:

| Secret | Purpose |
|---|---|
| `COORDINATOR_URL` | HTTPS endpoint that receives reports (`POST`, JSON body, header `X-Watcher-Seq`) |
| `COORDINATOR_TOKEN` | Sent as `Authorization: Bearer …` |
| `WATCHER_HMAC_KEY` | Signs reports with HMAC-SHA256 instead of the fingerprint |
| `GROK_DRAFT_URL`, `GROK_DRAFT_TOKEN` | Endpoint that returns Grok's draft (`POST {facts, diff}` → draft JSON); mechanical draft on failure, escalated after 3 failures in a row |

Push mode retries at 30 s, 2 min, 8 min, advances state only on a `2xx`, and escalates outages in the next delivered report.

## Limits

The workflow runs watchkit from `main`, so whoever can merge to `main` can change the watcher. Those changes are always escalated, and the coordinator's heartbeat and canary checks catch a watcher that goes silent; branch protection closes the gap.
