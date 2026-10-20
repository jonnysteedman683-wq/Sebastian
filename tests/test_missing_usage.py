"""P2-15: a model whose server omits usage must not win the token tie-break."""

from __future__ import annotations

import json
from collections.abc import Sequence

from sebastian.evals.cases import CAPABILITY
from sebastian.evals.leaderboard import ModelSpec, render, run_leaderboard
from sebastian.llm.openai_compat import parse_response
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message, Usage


class Model:
    def __init__(self, usage: Usage) -> None:
        self.usage = usage

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        return LLMResponse("Done.", usage=self.usage)


def test_honest_model_ranks_first_and_silent_one_shows_question_mark() -> None:
    models = {"silent": Usage(unreported=1), "honest": Usage(10, 5)}
    rows = run_leaderboard(
        [ModelSpec("silent"), ModelSpec("honest")], lambda s: Model(models[s.model]), CAPABILITY
    )
    assert rows[0].score == rows[1].score  # a real tie on score
    assert [r.spec.model for r in rows] == ["honest", "silent"]
    assert not rows[1].usage_reported
    table = render(rows).splitlines()
    assert table[2].split()[7] != "?"  # the tokens column
    assert table[3].split()[7] == "?"


def test_usage_flag_from_the_wire() -> None:
    def body(usage: object) -> bytes:
        msg = {"choices": [{"message": {"content": "x"}}], "usage": usage}
        return json.dumps(msg).encode()

    assert parse_response(body({"prompt_tokens": 3, "completion_tokens": 4})).usage.unreported == 0
    for bad in (None, {}, {"prompt_tokens": 3}, {"prompt_tokens": "3", "completion_tokens": 1}):
        assert parse_response(body(bad)).usage.unreported == 1


def test_unreported_adds_up() -> None:
    assert (Usage(1, 1) + Usage(unreported=1) + Usage(unreported=1)).unreported == 2
