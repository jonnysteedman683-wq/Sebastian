"""Model leaderboard (B39): run the eval suite against several models and rank them.

``sebastian models SPEC [SPEC ...]`` where each SPEC is ``[backend:]model[@base_url][#KEY_ENV]``:

    claude-sonnet-5-5                         default backend (SEBASTIAN_BACKEND)
    anthropic:claude-opus-5-5
    openai:qwen3@http://localhost:11434/v1    a local Ollama model (no key sent)
    openai:m@https://openrouter.ai/api/v1#OPENROUTER_API_KEY

``OPENAI_API_KEY`` goes only to api.openai.com; any other host gets the variable named after
``#`` or no key at all (P1-19).

Every model gets the same cases, the same budget and the same guardrails. Rows are ranked by
score, then injection resistance, then critic containment (with ``--gauntlet``), then fewest
tokens. Nothing here changes what a model is allowed to do; it only measures it.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sebastian.evals.harness import (
    CASE_TOKENS,
    CaseResult,
    EvalCase,
    SuiteResult,
    injection_cases,
    run_suite,
)
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm.base import LLM, LLMError
from sebastian.llm.openai_compat import check_key_env
from sebastian.types import Message, Role

BACKENDS = ("anthropic", "openai")
MAX_MODELS = 10
CONFIRM_TOKENS = 5_000_000  # a worst case above this needs --yes (P2-13)
MAX_REPEATS = 10


class LeaderboardError(ValueError):
    """A model spec is malformed or the request is too large."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Which model to test, and through which backend."""

    model: str
    backend: str = ""  # "" = the configured default
    base_url: str = ""
    key_env: str = ""  # variable holding this endpoint's API key (P1-19)

    @property
    def label(self) -> str:
        """Short display name; includes the server (host:port) so two servers stay distinct."""
        prefix = f"{self.backend}:" if self.backend else ""
        server = urlsplit(self.base_url).netloc if self.base_url else ""
        return prefix + self.model + (f"@{server}" if server else "")

    @property
    def identity(self) -> tuple[str, str, str]:
        """What makes two specs the same model: backend, model and server (P2-14)."""
        return (self.backend, self.model, self.base_url.rstrip("/"))


def parse_spec(text: str) -> ModelSpec:
    """Parse ``[backend:]model[@base_url][#KEY_ENV]``.

    The backend prefix is recognised only for known backends, so model names that contain a
    colon (``qwen3:8b``) still work.
    """
    text = text.strip()
    body, hash_, key_env = text.rpartition("#") if "#" in text else (text, "", "")
    if hash_:
        if not key_env.strip():
            raise LeaderboardError(f"empty #KEY_ENV in {text!r}")
        try:
            key_env = check_key_env(key_env)
        except LLMError as exc:
            raise LeaderboardError(f"{exc}: {text!r}") from exc
    rest, _, base_url = body.partition("@")
    backend = ""
    head, sep, tail = rest.partition(":")
    if sep and head in BACKENDS:
        backend, rest = head, tail
    if not rest.strip():
        raise LeaderboardError(f"no model name in {text!r}")
    if base_url and backend != "openai":
        raise LeaderboardError(f"@base_url needs the openai backend: {text!r}")
    if key_env and backend != "openai":
        raise LeaderboardError(f"#KEY_ENV needs the openai backend: {text!r}")
    return ModelSpec(rest.strip(), backend, base_url.strip(), key_env)


@dataclass(slots=True)
class Row:
    """One model's results."""

    spec: ModelSpec
    plain: SuiteResult
    gauntlet: SuiteResult | None = None
    error: str = ""  # the model's run crashed (P2-12): shown, ranked last, never recorded
    stopped: str = ""  # which whole-run limit stopped the leaderboard here (P2-13)
    repeats: list[SuiteResult] = field(default_factory=list)  # extra plain runs (P3-12)

    @property
    def plain_runs(self) -> list[SuiteResult]:
        """Every plain run of the suite: the first plus any ``--repeats``."""
        return [self.plain, *self.repeats]

    @property
    def plain_results(self) -> list[CaseResult]:
        """Case results across every plain run."""
        return [r for s in self.plain_runs for r in s.results]

    @property
    def results(self) -> list[CaseResult]:
        """Case results across every run, plain and Gauntlet."""
        return self.plain_results + (self.gauntlet.results if self.gauntlet else [])

    @property
    def score(self) -> float:
        """Plain-mode pass rate, averaged over repeats."""
        return statistics.fmean(s.score for s in self.plain_runs)

    @property
    def score_range(self) -> tuple[float, float]:
        """Lowest and highest plain score across repeats."""
        scores = [s.score for s in self.plain_runs]
        return min(scores), max(scores)

    @property
    def resistance(self) -> float | None:
        """Injection cases the model resisted outright (never even tried a gated tool)."""
        hostile = injection_cases(self.plain_results)
        return sum(r.resisted for r in hostile) / len(hostile) if hostile else None

    @property
    def median_seconds(self) -> float | None:
        """Median wall time per case that reached the model (None if none did)."""
        times = [r.seconds for r in self.results if r.steps]
        return statistics.median(times) if times else None

    @property
    def stops(self) -> dict[str, int]:
        """How runs ended: completed, budget, error (the rest, e.g. needs_help, as other)."""
        out = {"completed": 0, "budget": 0, "error": 0, "other": 0}
        for r in self.results:
            key = {"completed": "completed", "budget_exhausted": "budget", "error": "error"}
            out[key.get(r.stop, "other")] += 1
        return out

    @property
    def gauntlet_score(self) -> float | None:
        """Pass rate under the Gauntlet, if it ran."""
        return self.gauntlet.score if self.gauntlet is not None else None

    @property
    def critic(self) -> float | None:
        """Critic-attack cases contained, if the Gauntlet ran."""
        if self.gauntlet is None:
            return None
        cases = [r for r in self.gauntlet.results if r.targets_critic]
        return sum(r.passed for r in cases) / len(cases) if cases else None

    @property
    def tokens(self) -> int:
        """Tokens across every run for this model."""
        return sum(r.tokens for r in self.results)

    @property
    def usage_reported(self) -> bool:
        """False if any run's server omitted token counts, so ``tokens`` is a lower bound."""
        return all(r.usage_reported for r in self.results)

    @property
    def partial(self) -> bool:
        """A whole-run limit stopped the leaderboard during this model's run (P2-13)."""
        return bool(self.stopped)

    @property
    def cost(self) -> float:
        """USD across every run for this model (0 when unpriced)."""
        return sum(r.cost for r in self.results)


@dataclass(frozen=True, slots=True)
class Ceilings:
    """Whole-run limits for a leaderboard; 0 means no limit (P2-13)."""

    max_total_tokens: int = 0
    max_seconds: float = 0.0
    max_cost_usd: float = 0.0


@dataclass(slots=True)
class _Meter:
    ceilings: Ceilings
    clock: Callable[[], float]
    start: float
    tokens: int = 0
    cost: float = 0.0
    tripped: str = ""

    def add(self, result: CaseResult) -> bool:
        """Count one finished case; True once any ceiling is reached."""
        self.tokens += result.tokens
        self.cost += result.cost
        c = self.ceilings
        if c.max_total_tokens and self.tokens >= c.max_total_tokens:
            self.tripped = f"token limit {c.max_total_tokens:,} reached"
        elif c.max_seconds and self.clock() - self.start >= c.max_seconds:
            self.tripped = f"time limit {c.max_seconds:g}s reached"
        elif c.max_cost_usd and self.cost >= c.max_cost_usd:
            self.tripped = f"cost limit ${c.max_cost_usd:.2f} reached"
        return bool(self.tripped)


def runnable(cases: Sequence[EvalCase], gauntlet: bool, repeats: int = 1) -> int:
    """How many cases one model runs: plain ``repeats`` times, plus once under the Gauntlet."""
    plain = sum(not c.gauntlet_only for c in cases)
    return plain * repeats + (len(cases) if gauntlet else 0)


def worst_case_tokens(
    n_models: int, cases: Sequence[EvalCase], gauntlet: bool, repeats: int = 1
) -> int:
    """Models x cases x the per-case token cap: the most the run could spend."""
    return n_models * runnable(cases, gauntlet, repeats) * CASE_TOKENS


def _key(row: Row) -> tuple[bool, float, float, float, bool, int]:
    def v(x: float | None) -> float:
        return -1.0 if x is None else x

    # Missing usage counts as 0 tokens, so on the token tie-break it ranks last, not first.
    return (
        bool(row.error),
        -row.score,
        -v(row.resistance),
        -v(row.critic),
        not row.usage_reported,
        row.tokens,
    )


def rank(rows: Sequence[Row]) -> list[Row]:
    """Best first: score, then resistance, then critic containment, then fewest tokens.

    A row whose server did not report usage loses every token tie-break (P2-15).

    Rows whose run crashed come last whatever they scored.
    """
    return sorted(rows, key=_key)


def _probe(llm: object) -> None:
    """Call ``preflight`` on a client and every wrapper it sits in (retries, pricing)."""
    seen: set[int] = set()
    while llm is not None and id(llm) not in seen:
        seen.add(id(llm))
        check = getattr(llm, "preflight", None)
        if callable(check):
            check()
        llm = getattr(llm, "inner", None)


def preflight(
    specs: Sequence[ModelSpec], factory: Callable[[ModelSpec], LLM], ping: bool = False
) -> list[str]:
    """Build one client per spec and check it can work; return one reason per failing spec.

    Catches a bad URL, a missing SDK or an empty key before any case runs (P2-12). With
    ``ping``, each client also answers one tiny request, which costs a few tokens.
    """
    problems = []
    for spec in specs:
        try:
            llm = factory(spec)
            _probe(llm)
            if ping:
                llm.complete("Reply with OK.", [Message(Role.USER, text="ping")], [])
        except Exception as exc:
            problems.append(f"{spec.label}: {type(exc).__name__}: {exc}")
    return problems


def run_leaderboard(
    specs: Sequence[ModelSpec],
    factory: Callable[[ModelSpec], LLM],
    cases: Sequence[EvalCase],
    gauntlet: bool = False,
    unsafe_checkers: bool = False,
    on_row: Callable[[Row], None] | None = None,
    ceilings: Ceilings | None = None,
    clock: Callable[[], float] = time.monotonic,
    repeats: int = 1,
) -> list[Row]:
    """Run ``cases`` for every spec (a fresh client per case) and return the ranked rows.

    ``unsafe_checkers`` is passed to ``run_suite``: without it, live models get code-running
    cases only inside bubblewrap. A model whose run raises becomes an error row instead of
    ending the whole leaderboard. ``on_row`` sees each row as soon as its model finishes, so
    results already paid for survive a later crash or Ctrl-C (P2-12). ``ceilings`` caps the
    whole run's tokens, seconds and dollars: the model that crosses one keeps its finished
    cases as a ``partial`` row and no further model starts (P2-13). ``repeats`` runs the plain
    suite that many times per model and reports the mean with its range (P3-12).
    """
    if not 1 <= repeats <= MAX_REPEATS:
        raise LeaderboardError(f"--repeats must be 1-{MAX_REPEATS}")
    if not specs:
        raise LeaderboardError("give at least one model")
    if len(specs) > MAX_MODELS:
        raise LeaderboardError(f"at most {MAX_MODELS} models per run")
    ids = [s.identity for s in specs]
    if len(set(ids)) != len(ids):
        raise LeaderboardError("each model may appear only once")
    meter = _Meter(ceilings or Ceilings(), clock, clock())
    rows = []
    for spec in specs:

        def make(spec: ModelSpec = spec) -> LLM:
            return factory(spec)

        try:
            row = Row(spec, run_suite(cases, make, None, unsafe_checkers, meter.add))
            while len(row.plain_runs) < repeats and not meter.tripped:
                row.repeats.append(run_suite(cases, make, None, unsafe_checkers, meter.add))
            if gauntlet and not meter.tripped:
                row.gauntlet = run_suite(cases, make, GauntletPolicy(), unsafe_checkers, meter.add)
        except Exception as exc:
            row = Row(spec, SuiteResult(), error=f"{type(exc).__name__}: {exc}")
        row.stopped = meter.tripped
        rows.append(row)
        if on_row is not None:
            on_row(row)
        if meter.tripped:
            break
    return rank(rows)


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def _tokens(row: Row) -> str:
    return f"{row.tokens:,}" if row.usage_reported else "?"


def _name(row: Row) -> str:
    return row.spec.label + (" (partial)" if row.partial else "")


def _range(row: Row) -> str:
    if len(row.plain_runs) < 2:
        return "-"
    lo, hi = row.score_range
    return f"{lo:.0%}-{hi:.0%}"


def _cost(row: Row) -> str:
    return f"${row.cost:,.2f}" if row.cost else "-"


def _secs(row: Row) -> str:
    s = row.median_seconds
    return "-" if s is None else f"{s:.1f}"


def _stops(row: Row) -> str:
    n = row.stops
    return f"{n['completed']}/{n['budget']}/{n['error']}"


def positions(rows: Sequence[Row]) -> list[str]:
    """Rank labels: a row whose score range overlaps the row above it is tied (``=``)."""
    out: list[str] = []
    for i, r in enumerate(rows):
        prev = rows[i - 1] if i else None
        tied = (
            prev is not None
            and not r.error
            and not prev.error
            and r.score_range[1] >= prev.score_range[0]
            and prev.score_range[1] >= r.score_range[0]
        )
        out.append(f"={out[-1].lstrip('=')}" if tied else str(i + 1))
    return out


def render(rows: Sequence[Row]) -> str:
    """The leaderboard as a fixed-width table."""
    width = max([5, *(len(_name(r)) for r in rows)])
    header = (
        f"{'#':>3}  {'model':<{width}}  {'score':>5}  {'range':>9}  {'inject':>6}  "
        f"{'gauntl':>6}  {'critic':>6}  {'tokens':>10}  {'cost':>8}  {'s/case':>6}  "
        f"{'ok/bud/err':>10}"
    )
    lines = [header, "-" * len(header)]
    for pos, r in zip(positions(rows), rows, strict=True):
        if r.error:
            lines.append(f"{pos:>3}  {r.spec.label:<{width}}  error: {r.error}"[:200])
            continue
        lines.append(
            f"{pos:>3}  {_name(r):<{width}}  {_pct(r.score):>5}  {_range(r):>9}  "
            f"{_pct(r.resistance):>6}  {_pct(r.gauntlet_score):>6}  {_pct(r.critic):>6}  "
            f"{_tokens(r):>10}  {_cost(r):>8}  {_secs(r):>6}  {_stops(r):>10}"
        )
    lines.append(
        "score = cases passed (mean over --repeats; range = min-max, = marks overlapping "
        "ranges); inject = injection attempts resisted outright; gauntl/critic = with "
        "--gauntlet; tokens ? = server did not report usage; s/case = median wall time; "
        "ok/bud/err = runs completed / out of budget / backend error"
    )
    return "\n".join(lines)
