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

## 2026-11-06 - Data Table Row Headers
**Learning:** For HTML data tables, screen readers need row context just as much as column context. When the first column contains identifying data (like an ID, a rank, or a label), using `<td>` means the row has no semantic identifier. Using `<th scope="row">` provides this context.
**Action:** Changed the first cell of each data row in dashboard tables from `<td>` to `<th scope="row">`. Added CSS to reset visual styling for row headers so they look identical to regular table data cells while remaining accessible.
## 2026-11-05 - SVG Decorative Elements Accessibility
**Learning:** Screen readers will redundantly announce axis labels and legends in SVG charts if they are left accessible, cluttering the experience when data points are already accessible via `<title>` elements on inner points. Additionally, Internet Explorer can accidentally focus SVG elements unless `focusable="false"` is added.
**Action:** Added `aria-hidden="true"` to chart axis text and legend elements. Added `focusable="false"` to the main `<svg>` container.
