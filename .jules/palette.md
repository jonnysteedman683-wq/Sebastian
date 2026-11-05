## 2025-02-12 - Dashboard Accessibility & Polish
**Learning:** Raw HTML tables generated in Python scripts lack basic accessibility attributes like `scope="col"` on headers by default, which is critical for screen reader users when not using component libraries.
**Action:** Added `scope="col"` to table headers and `color-scheme` metadata to improve native UI rendering in dark mode.

## 2026-11-01 - Scrollable Table Accessibility
**Learning:** Scrollable containers (like responsive tables) need `tabindex="0"` and `role="region"` with an `aria-labelledby` attribute so keyboard users can scroll them and screen readers announce them properly.
**Action:** Added region roles and focus-visible styling to dashboard scroll containers.

## 2023-11-14 - HTML Element Structural Semantics
**Learning:** Generating raw HTML tiles using unassociated `<div>` tags lacks semantic meaning. Wrapping key/value data natively with definition lists (`<dl>`, `<dt>`, `<dd>`) yields immediate accessibility improvements.
**Action:** Replaced key/value `<div>` tiles with semantic definition lists in the HTML template string and updated the inline CSS to reset `<dd>` margins so visual rendering is unaffected.

## 2026-11-04 - Chart Interactive Accessibility and Clarity
**Learning:** SVG `<title>` tooltips require `<circle tabindex="0">` to be accessible to keyboard navigation. Moreover, visual states like `:focus-visible` or `:hover` are necessary to provide visible feedback since tooltips often do not appear without mouse hover.
**Action:** Added `tabindex="0"` and explicit focus/hover states for points in inline SVG charts. Also added a third distinct visual style for series so data lines remain understandable in monochrome or without color reliance.
