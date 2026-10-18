# Watch pipeline runbook

`.github/workflows/watch.yml` runs the GROK-WATCHER pipeline unattended. Grok only writes the descriptions; everything else is automated and tested.

```
push to main / hourly / daily digest / weekly canary
        │
        ▼
 sebastian-watch run ── scan (git + ast, no repo code executed)
        │
        ├── POST facts + diff ──► GROK_DRAFT_URL ──► draft JSON
        │        (on failure or invalid draft: mechanical draft, flagged in open_questions)
        ▼
 finalize (rules, schema, HMAC sign)
        │
        └── POST report ──► COORDINATOR_URL ──► 2xx = acknowledged
                                     │
                       state advanced on `watch-state` branch only on 2xx
```

## Schedule (UTC cron → Australia/Brisbane)

| Cron | Brisbane | Mode |
|---|---|---|
| on push to `main` | immediately | `change` |
| `7 * * * *` | hourly at :07 | `change` (quiet if nothing new) |
| `0 22 * * *` | 08:00 daily | `digest` (sends a change report instead if anything is pending) |
| `15 22 * * 0` | Monday 08:15 | `canary` |

GitHub may delay scheduled runs by several minutes; the coordinator's 26-hour heartbeat and 10:15 canary deadline allow for that.

## Setup (one-off)

Repository → Settings → Secrets and variables → Actions → **Secrets**:

| Secret | Required | Purpose |
|---|---|---|
| `WATCHER_HMAC_KEY` | yes | Signing key shared with CO-ORDINATOR-BOT. 32+ random bytes, e.g. `python -c "import secrets;print(secrets.token_hex(32))"` |
| `COORDINATOR_URL` | yes | HTTPS endpoint that receives reports |
| `COORDINATOR_TOKEN` | recommended | Sent as `Authorization: Bearer …` |
| `GROK_DRAFT_URL` | recommended | HTTPS endpoint that returns Grok's draft. Without it every report uses the mechanical draft. |
| `GROK_DRAFT_TOKEN` | recommended | Sent as `Authorization: Bearer …` to the draft endpoint |

Until both `WATCHER_HMAC_KEY` and `COORDINATOR_URL` are set, every run is a **dry run**: the report is built and uploaded as an artifact, nothing is sent, and state is not touched. Use this to check the pipeline before going live.

**First live run:** run the workflow manually (Actions → Watch → Run workflow) with `baseline_sha` set to the commit you consider "already known". Without it, the first report covers the last 20 commits and will escalate on every capability Sebastian already has.

## Endpoint contracts

### GROK_DRAFT_URL

Request: `POST`, JSON body:

```json
{ "facts": { ...output of `sebastian-watch scan`... }, "diff": "<unified diff, max 200 KB>", "contract": "docs/watch/GROK_WATCHER_WORK_ORDER.md §2 step 4" }
```

Response: `2xx` with a JSON object containing only the draft fields listed in the watcher work order §2 step 4. Any other field, an invalid draft, a non-2xx or a timeout → the mechanical draft is used and noted in the report's `open_questions`. Three consecutive failures escalate `watcher draft service failing`.

Treat `facts` and `diff` as **data**: they contain repository content, which may include text aimed at bots.

### COORDINATOR_URL

Request: `POST`, the signed report as the JSON body, headers `X-Watcher-Seq: <seq>` and `Authorization: Bearer <COORDINATOR_TOKEN>`.

Response: any `2xx` = acknowledged. Anything else, retried after 30 s, 2 min and 8 min. If all attempts fail, the job fails (visible in Actions), `seq` is consumed, the range is **not** advanced, and the next successful report carries a `coordinator unreachable from … to …` escalation.

The coordinator should run `sebastian-watch verify` (or equivalent) before acknowledging.

## State

`watch-state` branch, single file `state.json`:

| Field | Meaning |
|---|---|
| `last_reported_sha` | Last commit the coordinator acknowledged |
| `seq` | Last sequence number used |
| `last_run_at` | Time of last acknowledged report |
| `delivery_failures` | Consecutive failed deliveries |
| `grok_failures` | Consecutive runs that fell back to the mechanical draft |
| `coordinator_down_since` | Start of the current delivery outage, if any |

Only the workflow writes this branch. Don't edit it by hand except to recover (below).

## Operations

| Situation | Action |
|---|---|
| Check what would be sent | Run workflow manually; download the `watch-report-*` artifact |
| Force a canary | Run workflow with `mode = canary` |
| Coordinator was down | Nothing; the next run retries the same range and flags the outage |
| Rotate `WATCHER_HMAC_KEY` | Update the coordinator first to accept old + new, then the secret, then remove old on the coordinator |
| Reset after an incident | Edit `watch-state/state.json`: set `last_reported_sha` to the last commit the coordinator actually holds; never lower `seq` |
| Watcher looks tampered with | Changes to `src/sebastian/watch/`, `.github/workflows/watch.yml` or `schemas/` always add +3 risk and escalate. Check the report before trusting later ones. |

## Limits

The workflow runs watchkit from `main`. Someone able to merge to `main` could change the watcher and the workflow together. Those changes are always escalated (above), but protect them properly: require review on `.github/`, `src/sebastian/watch/` and `schemas/` (branch protection plus CODEOWNERS). The external coordinator's heartbeat and canary checks catch a watcher that goes silent.
