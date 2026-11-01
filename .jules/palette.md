## 2025-02-12 - Dashboard Accessibility & Polish
**Learning:** Raw HTML tables generated in Python scripts lack basic accessibility attributes like `scope="col"` on headers by default, which is critical for screen reader users when not using component libraries.
**Action:** Added `scope="col"` to table headers and `color-scheme` metadata to improve native UI rendering in dark mode.
