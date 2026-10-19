"""Model leaderboard (B39): run the eval suite against several models and rank them.

``sebastian models SPEC [SPEC ...]`` where each SPEC is ``[backend:]model[@base_url]``:

    claude-sonnet-5-5                         default backend (SEBASTIAN_BACKEND)
    anthropic:claude-opus-5-5
    openai:qwen3@http://localhost:11434/v1    a local Ollama model

Every model gets the same cases, the same budget and the same guardrails. Rows are ranked by
score, then injection resistance, then critic containment (with ``--gauntlet``), then fewest
tokens. Nothing here changes what a model is allowed to do; it only measures it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sebastian.evals.harness import EvalCase, SuiteResult, run_suite
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm.base import LLM

BACKENDS = ("anthropic", "openai")
MAX_MODELS = 10


class LeaderboardError(ValueError):
    """A model spec is malformed or the request is too large."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Which model to test, and through which backend."""

    model: str
    backend: str = ""  # "" = the configured default
    base_url: str = ""

    @property
    def label(self) -> str:
        """Short display name."""
        prefix = f"{self.backend}:" if self.backend else ""
        return prefix + self.model


def parse_spec(text: str) -> ModelSpec:
    """Parse ``[backend:]model[@base_url]``.

    The backend prefix is recognised only for known backends, so model names that contain a
    colon (``qwen3:8b``) still work.
    """
    text = text.strip()
    rest, _, base_url = text.partition("@")
    backend = ""
    head, sep, tail = rest.partition(":")
    if sep and head in BACKENDS:
        backend, rest = head, tail
    if not rest.strip():
        raise LeaderboardError(f"no model name in {text!r}")
    if base_url and backend != "openai":
        raise LeaderboardError(f"@base_url needs the openai backend: {text!r}")
    return ModelSpec(rest.strip(), backend, base_url.strip())


@dataclass(slots=True)
class Row:
    """One model's results."""

    spec: ModelSpec
    plain: SuiteResult
    gauntlet: SuiteResult | None = None

    @property
    def score(self) -> float:
        """Plain-mode pass rate."""
        return self.plain.score

    @property
    def resistance(self) -> float | None:
        """Injection cases the model resisted outright (never even tried a gated tool)."""
        hostile = [r for r in self.plain.results if r.hostile]
        return sum(r.resisted for r in hostile) / len(hostile) if hostile else None

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
        runs = self.plain.results + (self.gauntlet.results if self.gauntlet else [])
        return sum(r.tokens for r in runs)


def _key(row: Row) -> tuple[float, float, float, int]:
    def v(x: float | None) -> float:
        return -1.0 if x is None else x

    return (-row.score, -v(row.resistance), -v(row.critic), row.tokens)


def rank(rows: Sequence[Row]) -> list[Row]:
    """Best first: score, then resistance, then critic containment, then fewest tokens."""
    return sorted(rows, key=_key)


def run_leaderboard(
    specs: Sequence[ModelSpec],
    factory: Callable[[ModelSpec], LLM],
    cases: Sequence[EvalCase],
    gauntlet: bool = False,
) -> list[Row]:
    """Run ``cases`` for every spec (a fresh client per case) and return the ranked rows."""
    if not specs:
        raise LeaderboardError("give at least one model")
    if len(specs) > MAX_MODELS:
        raise LeaderboardError(f"at most {MAX_MODELS} models per run")
    labels = [s.label for s in specs]
    if len(set(labels)) != len(labels):
        raise LeaderboardError("each model may appear only once")
    rows = []
    for spec in specs:

        def make(spec: ModelSpec = spec) -> LLM:
            return factory(spec)

        row = Row(spec, run_suite(cases, make))
        if gauntlet:
            row.gauntlet = run_suite(cases, make, GauntletPolicy())
        rows.append(row)
    return rank(rows)


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def render(rows: Sequence[Row]) -> str:
    """The leaderboard as a fixed-width table."""
    width = max([5, *(len(r.spec.label) for r in rows)])
    header = (
        f"{'#':>2}  {'model':<{width}}  {'score':>5}  {'inject':>6}  "
        f"{'gauntl':>6}  {'critic':>6}  {'tokens':>10}"
    )
    lines = [header, "-" * len(header)]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i:>2}  {r.spec.label:<{width}}  {_pct(r.score):>5}  {_pct(r.resistance):>6}  "
            f"{_pct(r.gauntlet_score):>6}  {_pct(r.critic):>6}  {r.tokens:>10,}"
        )
    lines.append(
        "score = cases passed; inject = injection attempts resisted outright; "
        "gauntl/critic = with --gauntlet"
    )
    return "\n".join(lines)
