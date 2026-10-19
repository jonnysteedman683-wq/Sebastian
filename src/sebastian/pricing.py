"""Model prices and dollar accounting (B40).

Sebastian ships no price list: provider prices change and could not be verified here. You keep
your own in ``$SEBASTIAN_HOME/prices.json``, copied from your provider's pricing page:

    {"claude-sonnet-5-5": {"input": 3.0, "output": 15.0},
     "qwen3": {"input": 0, "output": 0}}

Values are USD per million tokens. A run whose models are priced reports its cost in traces,
``sebastian traces`` and the dashboard, and ``--max-cost`` stops it at a dollar limit. A run
with ``--max-cost`` refuses to start if any model it would use has no price.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sebastian.llm.base import LLM
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message, Usage

PRICES_FILE = "prices.json"
MAX_PRICE = 1000.0  # USD per million tokens; anything above is almost certainly a typo


class PricingError(ValueError):
    """The price file is invalid, or a needed price is missing."""


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million input and output tokens."""

    input: float
    output: float

    def cost(self, usage: Usage) -> float:
        """Dollar cost of ``usage`` at this price."""
        return (usage.input_tokens * self.input + usage.output_tokens * self.output) / 1_000_000


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PricingError(f"{where} must be a number")
    if not math.isfinite(value) or not 0 <= value <= MAX_PRICE:
        raise PricingError(f"{where} must be between 0 and {MAX_PRICE:g} USD per million tokens")
    return float(value)


def parse_prices(raw: Any) -> dict[str, Price]:
    """Validate a ``{"model": {"input": n, "output": n}}`` mapping."""
    if not isinstance(raw, dict):
        raise PricingError("prices must be a JSON object mapping model names to prices")
    prices = {}
    for model, entry in raw.items():
        if not isinstance(model, str) or not model.strip():
            raise PricingError("model names must be non-empty strings")
        if not isinstance(entry, dict) or set(entry) != {"input", "output"}:
            raise PricingError(f"{model}: give exactly 'input' and 'output'")
        prices[model] = Price(
            _number(entry["input"], f"{model}.input"), _number(entry["output"], f"{model}.output")
        )
    return prices


def load_prices(home: str | Path) -> dict[str, Price]:
    """Prices from ``<home>/prices.json``; a missing file means no prices."""
    path = Path(home) / PRICES_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PricingError(f"{path}: invalid JSON ({exc.msg})") from exc
    try:
        return parse_prices(raw)
    except PricingError as exc:
        raise PricingError(f"{path}: {exc}") from exc


def require_prices(models: Sequence[str], prices: Mapping[str, Price]) -> None:
    """Fail if a dollar limit is set but some model in use has no price."""
    missing = sorted({m for m in models if m not in prices})
    if missing:
        raise PricingError(
            f"--max-cost needs a price for {', '.join(missing)}; add it to {PRICES_FILE}"
        )


class PricedLLM:
    """Wraps a model and fills in ``usage.cost`` from its price."""

    def __init__(self, inner: LLM, price: Price) -> None:
        self.inner = inner
        self.price = price

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        """Call the inner model and price the reply."""
        resp = self.inner.complete(system, messages, tools)
        return replace(resp, usage=replace(resp.usage, cost=self.price.cost(resp.usage)))


def priced(llm: LLM, model: str, prices: Mapping[str, Price]) -> LLM:
    """``llm`` with costs filled in when ``model`` has a price, else unchanged."""
    price = prices.get(model)
    return PricedLLM(llm, price) if price is not None else llm
