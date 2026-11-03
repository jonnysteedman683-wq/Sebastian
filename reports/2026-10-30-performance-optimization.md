# Performance Optimization Report

**Target:** `src/sebastian/gauntlet.py:137`
**Issue:** Suboptimal Data Structure for Membership Check

## Actions Taken
1. **Measured Baseline:** Created benchmark scripts using the `timeit` module to test different membership evaluation methods (`in tuple`, `in set`, `in frozenset`).
2. **Analysis:** We discovered that while using a set mathematically offers O(1) lookups vs O(N) for tuples, using an *inline* set like `{Stage.VERIFY, Stage.CRITIQUE}` is actually **significantly slower** (~7.1s vs ~2.1s for 10M iterations) than a tuple `(Stage.VERIFY, Stage.CRITIQUE)`.
    * **Why:** CPython optimizes literal sets (like `{1, 2, 3}`) to frozensets at compile-time. But because `Stage` is an Enum (non-literal object), CPython cannot optimize it. It results in a `BUILD_SET` opcode that dynamically builds the set at runtime every time the line is evaluated.
3. **Implementation:** Rather than using an inline set or tuple, we extracted the data structure to a module-level pre-allocated frozenset `_RETRYABLE_STAGES = frozenset({Stage.VERIFY, Stage.CRITIQUE})`.
4. **Verified:**
    * Performance improved to ~1.85s for 10M iterations (10% faster than the tuple, 74% faster than the inline set).
    * `python -m pytest --cov` and `python -m pytest tests/test_gauntlet.py` were run. The gauntlet tests passed 100%. Two failures in `test_publish.py` were determined to be environment-related and unrelated to the changes (they failed on the pristine main branch as well).
5. **Quality Checks:** Passed `ruff check .`, `ruff format .`, and `python -m mypy`.
6. **Committed:** Committed under `jules-4030398936455940651-4e09eae5` with a detailed commit message outlining the "What, Why, and Measured Improvement".
