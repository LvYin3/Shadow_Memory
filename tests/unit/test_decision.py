from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.core.config import ConfigView
from astrbot_plugin_shadow_memory.core.decision import JevClient, chat_url, systemone_url
from astrbot_plugin_shadow_memory.core.http import HTTPResult

from ..fakes import FakeHTTP


def _client(overrides=None, http=None) -> tuple[JevClient, FakeHTTP]:
    cfg = ConfigView(
        overrides
        or {
            "decision": {
                "base_url": "https://relay.example/v1",
                "api_key": "sk-test",
                "model": "decision-model",
                "protocol": "systemone",
            }
        },
        "",
    )
    fake_http = http or FakeHTTP()
    return JevClient(cfg, fake_http, Audit()), fake_http


def test_url_derivation():
    assert systemone_url("https://a/v1") == "https://a/v1/systemone"
    assert systemone_url("https://a") == "https://a/v1/systemone"
    assert systemone_url("https://a/v1/systemone") == "https://a/v1/systemone"
    assert chat_url("https://a/v1") == "https://a/v1/chat/completions"
    assert chat_url("https://a") == "https://a/v1/chat/completions"


async def test_systemone_choice_parsed():
    http = FakeHTTP(
        {
            "https://relay.example/v1/systemone": HTTPResult(
                status=200,
                body={
                    "answers": {
                        "q": {
                            "type": "choice",
                            "choice": "c1",
                            "probabilities": {"none": 0.1, "c0": 0.2, "c1": 0.7},
                            "confidence": 0.7,
                        }
                    }
                },
            )
        }
    )
    client, _ = _client(http=http)
    result = await client.choice({}, "pick", {"none": "no", "c0": "a", "c1": "b"})
    assert result["c1"] == pytest.approx(0.7)
    assert sum(result.values()) == pytest.approx(1.0)


async def test_choice_without_probabilities_uses_choice():
    http = FakeHTTP(
        {
            "https://relay.example/v1/systemone": HTTPResult(
                status=200, body={"answers": {"q": {"choice": "c0"}}}
            )
        }
    )
    client, _ = _client(http=http)
    result = await client.choice({}, "pick", {"none": "no", "c0": "a"})
    assert result == {"none": 0.0, "c0": 1.0}


async def test_noul_probability_parsed_and_validated():
    http = FakeHTTP(
        {
            "https://relay.example/v1/systemone": HTTPResult(
                status=200, body={"answers": {"q": {"noul": 0.63}}}
            )
        }
    )
    client, _ = _client(http=http)
    assert await client.noul({}, "ask") == pytest.approx(0.63)

    bad = FakeHTTP(
        {
            "https://relay.example/v1/systemone": HTTPResult(
                status=200, body={"answers": {"q": {"noul": 1.5}}}
            )
        }
    )
    client_bad, _ = _client(http=bad)
    assert await client_bad.noul({}, "ask") is None


async def test_score_is_zero_based_and_clamped():
    def make(score):
        return FakeHTTP(
            {
                "https://relay.example/v1/systemone": HTTPResult(
                    status=200, body={"answers": {"q": {"score": score}}}
                )
            }
        )

    client, _ = _client(http=make(2.4))
    assert await client.score({}, "how", ["a", "b", "c", "d", "e"]) == pytest.approx(2.4)
    client_hi, _ = _client(http=make(9))
    assert await client_hi.score({}, "how", ["a", "b", "c"]) == pytest.approx(2.0)
    client_lo, _ = _client(http=make(-3))
    assert await client_lo.score({}, "how", ["a", "b", "c"]) == pytest.approx(0.0)


async def test_auto_falls_back_to_chat_on_404():
    chat_body = {
        "choices": [
            {
                "message": {
                    "content": '```json\n{"answers": {"q": {"noul": 0.8}}}\n```'
                }
            }
        ]
    }
    http = FakeHTTP(
        {
            "https://relay.example/v1/systemone": HTTPResult(status=404),
            "https://relay.example/v1/chat/completions": HTTPResult(status=200, body=chat_body),
        }
    )
    client, fake = _client(
        {
            "decision": {
                "base_url": "https://relay.example/v1",
                "api_key": "sk",
                "model": "decision-model",
                "protocol": "auto",
            }
        },
        http=http,
    )
    assert await client.noul({}, "ask") == pytest.approx(0.8)
    assert client.protocol == "chat"
    urls = [call[0] for call in fake.calls]
    assert urls[0].endswith("/systemone")
    assert urls[1].endswith("/chat/completions")


async def test_request_uses_bearer_and_payload():
    http = FakeHTTP(
        {
            "https://relay.example/v1/systemone": HTTPResult(
                status=200, body={"answers": {"q": {"noul": 0.5}}}
            )
        }
    )
    client, fake = _client(http=http)
    await client.noul({"me": "我"}, "ask")
    url, payload = fake.calls[0]
    assert payload["model"] == "decision-model"
    assert payload["state"] == {"me": "我"}
    assert payload["questions"]["q"]["type"] == "noul"


async def test_retries_then_backoff_after_three_failures():
    http = FakeHTTP()  # 默认 500
    client, fake = _client(http=http)
    for _ in range(3):
        assert await client.noul({}, "ask") is None
    calls_before = len(fake.calls)
    assert await client.noul({}, "ask") is None
    assert len(fake.calls) == calls_before  # 退避期内不再发请求
    assert fake.calls


async def test_unavailable_without_base_url():
    client = JevClient(ConfigView({"decision": {"base_url": ""}}, ""), FakeHTTP(), Audit())
    assert client.available is False
    assert await client.noul({}, "ask") is None
