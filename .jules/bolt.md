## YYYY-MM-DD - [Title]
**Learning:** [Insight]
**Action:** [Action]

## 2026-10-31 - [O(N²) bottleneck in context compaction]
**Learning:** Context compaction was calling `history_chars(out)` in a loop, which recursively traversed all messages, tool calls, and results. For very long histories, this resulted in O(N²) time complexity.
**Action:** Calculate the history size once and incrementally update it by adding the difference in lengths when replacing tool results.

## 2024-11-02 - Optimize trace entropy and randomness checks
**Learning:** In Python, `any(c.isdigit() for c in tok)` and similar generator comprehensions require Python-level loop overhead which is slow, especially when chained back-to-back. Compiled regular expressions like `re.compile(r"\d").search(tok)` shift the search to optimized C code. Additionally, when calculating entropy over character counts, `-sum((c / n) * math.log2(c / n))` can be optimized to `math.log2(n) - sum(c * math.log2(c)) / n` to avoid dividing in the loop.
**Action:** Use compiled regex for character-class checks (`\d`, `[A-Z]`, `[a-z]`) when string lengths are substantial instead of generators, and simplify math expressions in tight loops.
