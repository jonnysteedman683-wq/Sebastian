# watch-state

Written only by `.github/workflows/watch.yml`. Do not edit.

- `latest.json`: newest report (schema: `schemas/repo_change_report.schema.json` on `main`)
- `reports/NNNNNN.json`: every report, by sequence number
- `state.json`: watcher position (`last_reported_sha`, `seq`, ...)

Readers: fetch `latest.json` from the raw URL and run `sebastian-watch verify`.
