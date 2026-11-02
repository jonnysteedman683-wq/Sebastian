## 2025-02-12 - Dashboard Accessibility & Polish
**Learning:** Raw HTML tables generated in Python scripts lack basic accessibility attributes like `scope="col"` on headers by default, which is critical for screen reader users when not using component libraries.
**Action:** Added `scope="col"` to table headers and `color-scheme` metadata to improve native UI rendering in dark mode.

## 2026-11-01 - Scrollable Table Accessibility
**Learning:** Scrollable containers (like responsive tables) need `tabindex="0"` and `role="region"` with an `aria-labelledby` attribute so keyboard users can scroll them and screen readers announce them properly.
**Action:** Added region roles and focus-visible styling to dashboard scroll containers.
