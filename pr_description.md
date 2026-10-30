🧪 Testing Improvement for `Usage` type

🎯 **What:**
The `Usage` dataclass in `src/sebastian/types.py` lacked test coverage, specifically for its `total` property and `__add__` method.

📊 **Coverage:**
Added tests for:
*   `Usage.total` (happy path, zeros, input only, output only)
*   `Usage.__add__` (happy path, zeros, multiple additions)

✨ **Result:**
The `Usage` type is now fully tested, ensuring that token math works correctly and regression is prevented when we make changes to how tokens, costs, and unreported stats are calculated.
