# Proposals (out of scope for v0.1.0)

Ordered by value. Each needs owner sign-off before implementation (WORK_ORDER section 5).

1. **Container/namespace sandbox for `run_command`** (bubblewrap/nsjail, no network). Closes SECURITY residual risks 1-2.
2. **Eval harness (U12)** with a fixed task suite plus hostile prompt-injection cases, scored in CI.
3. **Gauntlet integration (U1/U2/U4)**: plan -> predict -> act -> test -> reflect stages, separate critic model, `pytest` command gates before LLM judgement.
4. **Embedding memory (U5)** with decay and dedup, replacing keyword recall.
5. **Trace viewer / replay (U9)** over `traces.jsonl`.
6. **Live API smoke test** in CI behind a repository secret.
7. **Licence**: done, MIT (B12).

## Bug in `src/sebastian/trace.py` \`_high_entropy\` function

The `_high_entropy` function in `src/sebastian/trace.py` is intended to selectively redact high entropy strings within segments of paths or URLs (e.g. `github.com/owner/Repo` vs `github.com/owner/[REDACTED]`).
Currently, the function is:
```python
def _high_entropy(m: re.Match[str]) -> str:
    tok = m.group(0)
    # A token with "/" may be a URL or path: judge each segment on its own, so
    # github.com/owner/Repo stays readable while a long random segment is still caught.
    parts = tok.split("/") if "/" in tok else [tok]
    return REDACTED if any(_looks_random(p) for p in parts) else tok
```

Because it returns `REDACTED` if *any* part looks random, it replaces the entire match with `[REDACTED]`. For instance, `https://example.com/api/aZ3kQ9xW2mN7pL5vR8tY1uB4cD6eF0gH/data` becomes `https://example.[REDACTED]` rather than `https://example.com/api/[REDACTED]/data`.

**Proposed Fix:**
```python
def _high_entropy(m: re.Match[str]) -> str:
    tok = m.group(0)
    if "/" in tok:
        parts = tok.split("/")
        if any(_looks_random(p) for p in parts):
            return "/".join(REDACTED if _looks_random(p) else p for p in parts)
        return tok
    return REDACTED if _looks_random(tok) else tok
```
