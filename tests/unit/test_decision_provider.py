from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.core.decision import (
    JevRouter,
    ProviderJevClient,
    normalize_provider_answer,
)


def answer(**kwargs):
    return SimpleNamespace(**kwargs)


class FakeProvider:
    def __init__(self, answers=None, fail=False):
        self.answers = answers or {}
        self.fail = fail
        self.calls: list[dict] = []

    async def ask(self, questions, state=None, *, request_max_retries=0):
        self.calls.append(
            {"questions": questions, "state": state, "retries": request_max_retries}
        )
        if self.fail:
            raise RuntimeError("provider down")
        return self.answers


class FakeDirect:
    available = True
    protocol = "systemone"

    def __init__(self, probability=0.5, fail=False):
        self.probability = probability
        self.fail = fail
        self.calls = 0
        self.stats = {"calls": 0, "failures": 0}

    async def ask(self, state, questions):
        self.calls += 1
        if self.fail:
            return None
        return {"q": {"noul": self.probability}}


def make_client(provider, *, provider_id="p1", max_retries=0, resolver=None):
    async def default_resolver(_provider_id):
        return provider

    return ProviderJevClient(
        provider_id,
        resolver or default_resolver,
        Audit(),
        lambda kind, instructions, criteria: {
            "kind": kind,
            "instructions": instructions,
            "criteria": criteria,
        },
        max_retries=max_retries,
    )


# --------------------------------------------------------------------- 归一化
def test_normalize_boolean():
    assert normalize_provider_answer("noul", answer(probability=0.73)) == {"noul": 0.73}
    assert normalize_provider_answer("noul", answer(probability=1.4)) is None
    assert normalize_provider_answer("noul", answer()) is None


def test_normalize_choice():
    criteria = {"none": "", "c0": "", "c1": ""}
    normalized = normalize_provider_answer(
        "choice",
        answer(choice="c0", probabilities={"none": 0.1, "c0": 0.7, "c1": 0.2}),
        criteria,
    )
    assert normalized["choice"] == "c0"
    assert normalized["probabilities"]["c0"] == pytest.approx(0.7)
    assert sum(normalized["probabilities"].values()) == pytest.approx(1.0)

    only_choice = normalize_provider_answer(
        "choice", answer(choice="c0", probabilities=None), criteria
    )
    assert only_choice == {"choice": "c0", "probabilities": {"none": 0.0, "c0": 1.0, "c1": 0.0}}

    assert normalize_provider_answer("choice", answer(choice="x"), criteria) is None


def test_normalize_score():
    levels = ["1", "2", "3", "4", "5"]
    assert normalize_provider_answer("score", answer(score=2.4), levels) == {"score": 2.4}
    assert normalize_provider_answer("score", answer(score=9), levels) == {"score": 4.0}
    assert normalize_provider_answer("score", answer(score=-1), levels) == {"score": 0.0}
    assert normalize_provider_answer("score", answer(score=True), levels) is None
    assert normalize_provider_answer("unknown", answer(score=1), levels) is None


# --------------------------------------------------------------------- provider 客户端
async def test_ask_normalizes_all_question_kinds():
    provider = FakeProvider(
        {
            "n": answer(probability=0.6),
            "c": answer(choice="c0", probabilities={"none": 0.2, "c0": 0.8}),
            "s": answer(score=3.2),
        }
    )
    client = make_client(provider)
    result = await client.ask(
        {"me": "我"},
        {
            "n": {"type": "noul", "instructions": "是？"},
            "c": {"type": "choice", "instructions": "选？", "criteria": {"none": "", "c0": ""}},
            "s": {"type": "score", "instructions": "几分？", "criteria": ["a", "b", "c", "d", "e"]},
        },
    )
    assert result == {
        "n": {"noul": 0.6},
        "c": {"choice": "c0", "probabilities": {"none": 0.2, "c0": 0.8}},
        "s": {"score": 3.2},
    }
    assert provider.calls[0]["state"] == {"me": "我"}


async def test_builder_receives_kind_instructions_criteria():
    seen: list[tuple] = []
    provider = FakeProvider({"q": answer(probability=0.5)})

    def builder(kind, instructions, criteria):
        seen.append((kind, instructions, criteria))
        return {"kind": kind}

    async def resolver(_pid):
        return provider

    client = ProviderJevClient("p1", resolver, Audit(), builder)
    await client.noul("state", "是？")
    assert seen[0] == ("noul", "是？", None)


async def test_provider_missing_returns_none():
    async def resolver(_pid):
        return None

    client = make_client(None, resolver=resolver)
    assert await client.noul({}, "是？") is None
    assert client.stats["failures"] == 1


async def test_provider_without_ask_is_unusable():
    client = make_client(object())
    assert await client.noul({}, "是？") is None


async def test_provider_failure_backoff():
    provider = FakeProvider(fail=True)
    client = make_client(provider)
    for _ in range(3):
        assert await client.noul({}, "是？") is None
    calls = len(provider.calls)
    assert calls == 3
    assert await client.noul({}, "是？") is None
    assert len(provider.calls) == calls  # 退避期内不再调用


async def test_max_retries_forwarded():
    provider = FakeProvider({"q": answer(probability=0.5)})
    client = make_client(provider, max_retries=2)
    await client.noul({}, "是？")
    assert provider.calls[0]["retries"] == 2


async def test_wrappers():
    provider = FakeProvider(
        {
            "q": answer(
                probability=0.4,
                choice="c1",
                probabilities={"none": 0.1, "c0": 0.2, "c1": 0.7},
                score=1.5,
            )
        }
    )
    client = make_client(provider)
    assert await client.noul({}, "是？") == 0.4
    assert (await client.choice({}, "选？", {"none": "", "c0": "", "c1": ""}))["c1"] == 0.7
    assert await client.score({}, "几分？", ["a", "b", "c", "d", "e"]) == 1.5


async def test_invalid_answer_returns_none():
    provider = FakeProvider({"q": answer(probability=2.0)})
    client = make_client(provider)
    assert await client.noul({}, "是？") is None


def test_flags():
    async def resolver(_pid):
        return None

    assert make_client(None, resolver=resolver).available is True
    assert make_client(None, provider_id="", resolver=resolver).available is False
    assert make_client(None, resolver=resolver).protocol == "provider"


# --------------------------------------------------------------------- 路由
async def test_router_prefers_provider():
    provider = FakeProvider({"q": answer(probability=0.8)})
    primary = make_client(provider)
    fallback = FakeDirect()
    router = JevRouter(primary, fallback)
    assert await router.noul({}, "是？") == 0.8
    assert fallback.calls == 0
    assert router.stats["provider_calls"] == 1


async def test_router_falls_back_when_provider_fails():
    primary = make_client(FakeProvider(fail=True))
    fallback = FakeDirect(probability=0.3)
    router = JevRouter(primary, fallback)
    assert await router.noul({}, "是？") == 0.3
    assert fallback.calls == 1
    assert router.stats["fallback_calls"] == 1


async def test_router_without_any_backend():
    async def resolver(_pid):
        return None

    router = JevRouter(make_client(None, resolver=resolver), None)
    assert await router.noul({}, "是？") is None
    assert router.stats["failures"] == 1


async def test_router_available_and_protocol():
    primary = make_client(FakeProvider())
    direct = FakeDirect()
    assert JevRouter(primary, direct).protocol == "provider+direct"
    assert JevRouter(primary, None).protocol == "provider"
    assert JevRouter(None, direct).protocol == "direct"
    assert JevRouter(None, None).protocol == "none"
    assert JevRouter(None, None).available is False
