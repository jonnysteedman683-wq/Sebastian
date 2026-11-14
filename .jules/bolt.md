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

## 2026-11-07 - [Generator overhead in context compaction and dashboard]
**Learning:** Even when guarded by an `if collection:` check, generator comprehensions like `sum(...)` over small collections (e.g., tool calls/results in a single message) still incur generator setup overhead in Python. When called frequently (like during history compaction or chart generation over thousands of runs), this overhead accumulates.
**Action:** Replace `sum(len(x) for x in collection)` with a simple `for` loop and an accumulator variable in tight loops. Additionally, precompute `any()` or `sum()` over large collections (like trace runs) once if their results are used multiple times in the same function.
## 2026-11-06 - [Generator comprehension overhead in hot loops]
**Learning:** Generator comprehensions passed to functions like `sum()` inside tight loops incur significant overhead due to object creation and function calls for yielding items. Replacing these with simple `for` loops avoids this overhead. Additionally, combining multiple separate iterations over a collection into a single, unified loop significantly improves performance on hot paths.
**Action:** Replace `sum(generator_expression)` in hot loops with explicit iteration and running totals, combining iterations when traversing the same lists multiple times.

## 2026-11-10 - [Memory Overhead of read_text().splitlines() on large files]
**Learning:** `Path.read_text().splitlines()` loads the entire file into memory as a single string, and then allocates a massive list of strings. For large `.jsonl` trace and history files, this leads to significant O(N) memory allocation and CPU overhead, sometimes over 1.5 seconds for large files compared to 0.15s for iterators.
**Action:** Use file iterators (`with path.open("r", encoding="utf-8") as fh: for line in fh:`) instead of `.read_text().splitlines()` when parsing log or trace files, and apply fast string filtering before calling `json.loads()`.

## 2026-11-12 - [json.loads overhead on large trace files]
**Learning:** In `summarize_runs()`, calling `json.loads` on every line of a large trace file to extract usage information from a small subset of events (`run_start`, `run_end`) incurs significant overhead. Additionally, using generator expressions in this hot loop (e.g. `sum(v for k, v in dict.items())`) adds unnecessary setup costs.
**Action:** Add fast string substring matching filters (`in line`) before calling `json.loads` to skip irrelevant lines entirely, and replace generator comprehensions with manual loops or direct lookups when parsing traces or hot paths.
