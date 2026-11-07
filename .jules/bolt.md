## YYYY-MM-DD - [Title]
**Learning:** [Insight]
**Action:** [Action]

## 2026-11-05 - [Generator overhead on empty collections]
**Learning:** Generator comprehensions (like `sum(...)` over empty lists or tuples) incur significant loop and setup overhead. In contexts with frequent loop executions over mostly-empty collections (such as checking tool calls or results in long chat histories), this overhead is non-trivial.
**Action:** Guard generator expressions with an `if collection:` check to completely bypass the generator setup and execution overhead for empty sequences, especially in tight loops.

## 2026-10-31 - [O(N²) bottleneck in context compaction]
**Learning:** Context compaction was calling `history_chars(out)` in a loop, which recursively traversed all messages, tool calls, and results. For very long histories, this resulted in O(N²) time complexity.
**Action:** Calculate the history size once and incrementally update it by adding the difference in lengths when replacing tool results.

## 2024-11-02 - Optimize trace entropy and randomness checks
**Learning:** In Python, `any(c.isdigit() for c in tok)` and similar generator comprehensions require Python-level loop overhead which is slow, especially when chained back-to-back. Compiled regular expressions like `re.compile(r"\d").search(tok)` shift the search to optimized C code. Additionally, when calculating entropy over character counts, `-sum((c / n) * math.log2(c / n))` can be optimized to `math.log2(n) - sum(c * math.log2(c)) / n` to avoid dividing in the loop.
**Action:** Use compiled regex for character-class checks (`\d`, `[A-Z]`, `[a-z]`) when string lengths are substantial instead of generators, and simplify math expressions in tight loops.

## 2024-11-04 - Optimize dashboard trust chart rendering O(N²)
**Learning:** In `dashboard.py` `_render_charts()`, repeatedly calling `assess(past_records)` inside a loop over `all_latest` caused an O(N²) bottleneck, as `assess` iterates and sorts all prior history to establish the trust level at that point in time. When thousands of records exist, generating the dashboard HTML blocked for over 5 seconds.
**Action:** Extract the iterative logic into a generator `assess_progression()` that yields the assessment sequentially after each record. Then precalculate the progression points and use a two-pointer approach to map them to the timeline in O(N log N) time.

## 2026-11-07 - [O(N) memory and parsing bottleneck in JSONL traces]
**Learning:** Trace files grow very large (MBs) due to storing full tool inputs and outputs. Reading them entirely into memory with `read_text().splitlines()` and calling `json.loads` on every line causes massive memory spikes and slow dashboard rendering, even though only a small fraction of lines (like `run_start` and `run_end`) are needed.
**Action:** Use a file iterator (`with path.open() as fh: for line in fh:`) to keep memory usage O(1), and apply a fast string filter (e.g., `if '"run_start"' not in line`) before parsing JSON to skip expensive decoding of unneeded events.
