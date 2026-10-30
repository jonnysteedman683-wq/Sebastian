"""P1-19: OPENAI_API_KEY only goes to OpenAI; other hosts get the key named for them, or none."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sebastian.config import Config
from sebastian.evals.leaderboard import LeaderboardError, ModelSpec, parse_spec
from sebastian.llm import RetryingLLM, make_llm
from sebastian.llm import openai_compat as oc
from sebastian.llm.base import LLMError
from sebastian.llm.openai_compat import OpenAICompatLLM, check_key_env, resolve_api_key
from sebastian.types import Message, Role

OPENAI = "https://api.openai.com/v1"
THIRD = "https://other-host.example/v1"
LOCAL = "http://localhost:11434/v1"
REPLY = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}


class Recorder:
    """Transport double: records headers, answers 200."""

    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []

    def __call__(
        self, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        self.headers.append(dict(headers))
        import json

        return 200, json.dumps(REPLY).encode()


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-FAKE")
    monkeypatch.setenv("MY_KEY", "sk-test-FAKE-mine")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-FAKE-anthropic")


def _auth(base_url: str, key_env: str = "") -> str | None:
    rec = Recorder()
    OpenAICompatLLM("m", base_url, key_env=key_env, transport=rec).complete(
        "S", [Message(Role.USER, text="t")], []
    )
    return rec.headers[0].get("Authorization")


def test_openai_gets_openai_key() -> None:
    assert _auth(OPENAI) == "Bearer sk-test-FAKE"
    assert _auth("https://API.OpenAI.com/v1") == "Bearer sk-test-FAKE"


@pytest.mark.parametrize("url", [THIRD, LOCAL, "http://127.0.0.1:8000/v1"])
def test_other_hosts_get_no_key_by_default(url: str) -> None:
    assert _auth(url) is None


@pytest.mark.parametrize("url", [THIRD, LOCAL])
def test_named_key_is_sent(url: str) -> None:
    assert _auth(url, "MY_KEY") == "Bearer sk-test-FAKE-mine"


def test_lookalike_host_gets_no_openai_key() -> None:
    assert _auth("https://api.openai.com.evil.example/v1") is None


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "anthropic_api_key", "GH_TOKEN", "GITHUB_TOKEN", "gh_pat"]
)
def test_forbidden_key_envs(name: str) -> None:
    with pytest.raises(LLMError, match="refusing"):
        check_key_env(name)
    with pytest.raises(LLMError, match="refusing"):
        OpenAICompatLLM("m", THIRD, key_env=name, transport=Recorder())


@pytest.mark.parametrize("name", ["", "1KEY", "MY-KEY", "A B", "KEÿ"])
def test_malformed_key_env(name: str) -> None:
    with pytest.raises(LLMError, match="environment variable name"):
        check_key_env(name)


def test_resolve_unset_named_key_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_KEY")
    assert resolve_api_key(THIRD, "MY_KEY") == ""


@pytest.mark.parametrize(
    ("text", "spec"),
    [
        ("openai:m@https://other-host.example/v1#MY_KEY",
         ModelSpec("m", "openai", THIRD, "MY_KEY")),
        ("openai:m#MY_KEY", ModelSpec("m", "openai", "", "MY_KEY")),
        ("openai:qwen3:8b@http://localhost:11434/v1", ModelSpec("qwen3:8b", "openai", LOCAL)),
    ],
)  # fmt: skip
def test_spec_key_suffix(text: str, spec: ModelSpec) -> None:
    assert parse_spec(text) == spec


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("openai:m@https://x.example/v1#ANTHROPIC_API_KEY", "refusing"),
        ("openai:m@https://x.example/v1#GITHUB_TOKEN", "refusing"),
        ("openai:m#", "empty #KEY_ENV"),
        ("openai:m#bad-name", "environment variable name"),
        ("anthropic:claude-x#MY_KEY", "needs the openai backend"),
    ],
)
def test_spec_key_suffix_errors(text: str, why: str) -> None:
    with pytest.raises(LeaderboardError, match=why):
        parse_spec(text)


def test_make_llm_and_config_carry_key_env() -> None:
    cfg = Config.from_env({"SEBASTIAN_API_KEY_ENV": " MY_KEY "})
    assert cfg.api_key_env == "MY_KEY"
    client = make_llm("m", "openai", THIRD, cfg.api_key_env)
    assert isinstance(client, RetryingLLM)
    assert client.inner._api_key == "sk-test-FAKE-mine"  # type: ignore[attr-defined]
    with pytest.raises(LLMError, match="refusing"):
        make_llm("m", "openai", THIRD, "ANTHROPIC_API_KEY")


def test_leaderboard_sends_each_key_only_to_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sebastian import cli

    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    built: dict[str, str] = {}

    def fake(model: str, backend: str, base_url: str, key_env: str) -> Any:
        client = OpenAICompatLLM(model, base_url or OPENAI, key_env=key_env, transport=Recorder())
        built[model] = client._api_key
        return client

    monkeypatch.setattr(cli, "make_llm", fake)
    specs = [
        "openai:gpt-x",
        f"openai:qwen@{THIRD}",
        f"openai:loc@{LOCAL}",
        f"openai:mine@{THIRD}#MY_KEY",
    ]
    cli.main(["models", *specs, "--no-record", "--yes", "--scars", str(tmp_path / "none")])
    capsys.readouterr()
    assert built == {"gpt-x": "sk-test-FAKE", "qwen": "", "loc": "", "mine": "sk-test-FAKE-mine"}


def test_redirects_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    class Resp:
        status = 302

        def getheader(self, name: str) -> str | None:
            if name == "Location":
                return "https://evil.example/steal"
            return None

        def read(self) -> bytes:
            return b""

    def noop_request(
        self: Any, method: str, url: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        pass

    def redirect_getresponse(self: Any) -> Resp:
        return Resp()

    monkeypatch.setattr(http.client.HTTPSConnection, "request", noop_request)
    monkeypatch.setattr(http.client.HTTPSConnection, "getresponse", redirect_getresponse)

    with pytest.raises(LLMError, match=r"redirect to \'evil\.example\' refused"):
        oc._http_transport("https://h/v1", {}, b"{}", 5)


def test_unauthorised_without_key_explains_why() -> None:
    def deny(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        return 401, b"missing key"

    with pytest.raises(LLMError, match=r"no API key was sent to other-host\.example"):
        OpenAICompatLLM("m", THIRD, transport=deny).complete(
            "S", [Message(Role.USER, text="t")], []
        )
    with pytest.raises(LLMError) as exc:
        OpenAICompatLLM("m", THIRD, key_env="MY_KEY", transport=deny).complete(
            "S", [Message(Role.USER, text="t")], []
        )
    assert "no API key" not in str(exc.value)
