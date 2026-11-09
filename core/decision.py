"""System One 判断客户端。

支持三种协议形态：
- ``systemone``：天生支持 choice 概率、noul 校准概率与 score；
- ``chat``：OpenAI 兼容 chat/completions，用提示词要求返回同构 JSON；
- ``auto``：先试 systemone，404/405/响应不合法时本进程永久退回 chat。

解析规则对齐 AstrBot 自带的 SystemOneDecisionProvider：
score 为 0 基（0..len(criteria)-1），choice 概率和约等于 1。
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Awaitable, Callable, Mapping

from .audit import Audit
from .config import ConfigView
from .http import HTTPClient, HTTPResult
from .jsonx import extract_json

_CHAT_SYSTEM_PROMPT = (
    "You are a calibrated decision model. You do not chat.\n"
    "Given STATE and QUESTIONS in the user message, return ONLY a JSON object of the form\n"
    '{"answers": {"<question>": <answer>}}\n'
    "Answer shapes:\n"
    '- noul: {"noul": <probability 0..1 that the statement is true>}\n'
    '- choice: {"choice": "<option key>", "probabilities": {"<key>": <0..1>, ...}} '
    "where probabilities sum to 1 and the chosen key has the highest value\n"
    '- score: {"score": <number 0..N-1 on the ordered scale>}'
)


def systemone_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/systemone"):
        return base
    if base.endswith("/v1"):
        return base + "/systemone"
    return base + "/v1/systemone"


def chat_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _as_probability(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if number < 0.0 or number > 1.0:
        return None
    return number


class JevClient:
    def __init__(
        self,
        config: ConfigView,
        http: HTTPClient,
        audit: Audit,
        *,
        backoff_seconds: float = 600.0,
        failure_threshold: int = 3,
    ) -> None:
        self.config = config
        self.http = http
        self.audit = audit
        self.base_url = config.str("decision.base_url").strip()
        self.api_key = config.str("decision.api_key").strip()
        self.model = config.str("decision.model", "").strip()
        self.timeout_s = max(0.2, config.int("decision.timeout_ms", 3000) / 1000.0)
        self.max_retries = config.int("decision.max_retries", 1, lo=0, hi=3)
        configured = config.str("decision.protocol", "auto").strip().lower()
        self._protocol = configured if configured in {"auto", "systemone", "chat"} else "auto"
        self._resolved = "systemone" if self._protocol == "systemone" else ""
        if self._protocol == "chat":
            self._resolved = "chat"
        self._failures = 0
        self._cooldown_until = 0.0
        self._backoff_seconds = backoff_seconds
        self._failure_threshold = failure_threshold
        self.stats = {"calls": 0, "failures": 0, "systemone": 0, "chat": 0}

    # ------------------------------------------------------------------ 状态
    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def protocol(self) -> str:
        return self._resolved or self._protocol

    def _in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def _note_failure(self) -> None:
        self.stats["failures"] += 1
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._cooldown_until = time.monotonic() + self._backoff_seconds
            self._failures = 0
            self.audit.warn("判断模型连续失败，进入 %.0f 秒退避", self._backoff_seconds)

    def _note_success(self) -> None:
        self._failures = 0

    # ------------------------------------------------------------------ 入口
    async def ask(
        self, state: str | Mapping[str, Any], questions: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]] | None:
        """返回 {question: normalized_answer}；整体失败返回 None。"""
        if not self.available or not questions:
            return None
        if self._in_cooldown():
            return None

        self.stats["calls"] += 1
        order = self._protocol_order()
        for protocol in order:
            result = await self._request_once(protocol, state, questions)
            if result is None:
                continue
            if protocol == "systemone" and self._protocol == "auto":
                self._resolved = "systemone"
            answers = self._parse_answers(protocol, result, questions)
            if answers is None:
                if protocol == "systemone" and self._protocol == "auto":
                    self.audit.warn("systemone 响应不合法，退回 chat 协议")
                    self._resolved = "chat"
                    continue
                self._note_failure()
                return None
            self.stats[protocol] = self.stats.get(protocol, 0) + 1
            self._note_success()
            return answers
        self._note_failure()
        return None

    def _protocol_order(self) -> list[str]:
        if self._protocol == "systemone":
            return ["systemone"]
        if self._protocol == "chat":
            return ["chat"]
        if self._resolved == "chat":
            return ["chat"]
        return ["systemone", "chat"]

    async def _request_once(
        self, protocol: str, state: str | Mapping[str, Any], questions: Mapping[str, Any]
    ) -> HTTPResult | None:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if protocol == "systemone":
            url = systemone_url(self.base_url)
            payload = {"model": self.model, "state": state, "questions": dict(questions)}
        else:
            url = chat_url(self.base_url)
            payload = {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": "STATE and QUESTIONS (UTF-8 JSON):\n"
                        + _json_dumps({"state": state, "questions": dict(questions)}),
                    },
                ],
            }

        attempts = self.max_retries + 1
        for attempt in range(attempts):
            result = await self.http.post_json(url, payload, headers, self.timeout_s)
            if result.status in (404, 405) and protocol == "systemone":
                return result
            if result.ok:
                return result
            if result.status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            self.audit.warn(
                "判断请求 %s 失败 status=%s error=%s", protocol, result.status, result.error
            )
            return result
        return None

    # ------------------------------------------------------------------ 解析
    def _parse_answers(
        self, protocol: str, result: HTTPResult, questions: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]] | None:
        if not result.ok:
            return None
        payload: Any
        if protocol == "systemone":
            payload = result.body
        else:
            payload = self._chat_payload(result)
        if not isinstance(payload, Mapping):
            return None
        raw_answers = payload.get("answers")
        if not isinstance(raw_answers, Mapping):
            return None

        answers: dict[str, dict[str, Any]] = {}
        for name, question in questions.items():
            raw = raw_answers.get(name)
            if not isinstance(raw, Mapping):
                return None
            kind = str(question.get("type", "")).lower()
            if kind == "noul":
                probability = _as_probability(raw.get("noul"))
                if probability is None:
                    probability = _as_probability(raw.get("probability"))
                if probability is None:
                    return None
                answers[name] = {"noul": probability}
            elif kind == "choice":
                normalized = _normalize_choice(raw, question.get("criteria") or {})
                if normalized is None:
                    return None
                answers[name] = normalized
            elif kind == "score":
                levels = list(question.get("criteria") or [])
                score = _normalize_score(raw.get("score"), levels)
                if score is None:
                    return None
                answers[name] = {"score": score}
            else:
                return None
        return answers

    @staticmethod
    def _chat_payload(result: HTTPResult) -> Any:
        body = result.body
        content = ""
        if isinstance(body, Mapping):
            choices = body.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, Mapping):
                    message = first.get("message")
                    if isinstance(message, Mapping):
                        content = str(message.get("content") or "")
        if not content:
            content = result.text
        return extract_json(content)

    # ------------------------------------------------------------------ 便捷封装
    async def noul(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        criteria: Mapping[str, str] | None = None,
    ) -> float | None:
        question: dict[str, Any] = {"type": "noul", "instructions": instructions}
        if criteria:
            question["criteria"] = dict(criteria)
        answers = await self.ask(state, {"q": question})
        if not answers:
            return None
        return answers["q"].get("noul")

    async def choice(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        options: Mapping[str, str],
    ) -> dict[str, float] | None:
        if len(options) < 2:
            return None
        answers = await self.ask(
            state,
            {
                "q": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": dict(options),
                }
            },
        )
        if not answers:
            return None
        probabilities = answers["q"].get("probabilities")
        return dict(probabilities) if isinstance(probabilities, Mapping) else None

    async def score(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        levels: list[str],
    ) -> float | None:
        if not 2 <= len(levels) <= 10:
            return None
        answers = await self.ask(
            state,
            {"q": {"type": "score", "instructions": instructions, "criteria": list(levels)}},
        )
        if not answers:
            return None
        value = answers["q"].get("score")
        return float(value) if isinstance(value, (int, float)) else None


def _json_dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _normalize_choice(
    raw: Mapping[str, Any], criteria: Mapping[str, Any]
) -> dict[str, Any] | None:
    options = [str(key) for key in criteria.keys()]
    if len(options) < 2:
        return None
    probabilities_raw = raw.get("probabilities")
    probabilities: dict[str, float] = {}
    if isinstance(probabilities_raw, Mapping):
        for key, value in probabilities_raw.items():
            probability = _as_probability(value)
            if probability is not None:
                probabilities[str(key)] = probability
    choice = raw.get("choice")
    choice = str(choice) if choice is not None else ""

    if not probabilities and choice in options:
        probabilities = {key: 0.0 for key in options}
        probabilities[choice] = 1.0
    if not probabilities:
        return None
    # 只保留声明过的选项，缺失补 0，全部丢弃时失败
    filtered = {key: probabilities.get(key, 0.0) for key in options}
    total = sum(filtered.values())
    if total <= 0:
        return None
    filtered = {key: value / total for key, value in filtered.items()}
    selected = choice if choice in filtered else max(filtered, key=filtered.get)
    return {"choice": selected, "probabilities": filtered}


def _normalize_score(value: Any, levels: list[str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if not levels:
        return number
    upper = len(levels) - 1
    if upper <= 0:
        return 0.0
    # 容忍把 1 基当 0 基的返回：恰好等于 len(levels) 时视为顶档
    if math.isclose(number, len(levels)) and upper != len(levels):
        number = float(upper)
    if number < 0:
        return 0.0
    if number > upper:
        return float(upper)
    return number


# --------------------------------------------------------------------- Provider 适配
def normalize_provider_answer(
    kind: str, answer: Any, criteria: Any = None
) -> dict[str, Any] | None:
    """把 AstrBot DecisionAnswer 归一成插件内部的字典结构。"""
    kind = str(kind or "").lower()
    if kind == "noul":
        probability = _as_probability(getattr(answer, "probability", None))
        if probability is None:
            return None
        return {"noul": probability}
    if kind == "choice":
        probabilities = getattr(answer, "probabilities", None)
        raw = {
            "choice": getattr(answer, "choice", None),
            "probabilities": dict(probabilities)
            if isinstance(probabilities, Mapping)
            else None,
        }
        return _normalize_choice(raw, criteria or {})
    if kind == "score":
        value = _normalize_score(getattr(answer, "score", None), list(criteria or []))
        if value is None:
            return None
        return {"score": value}
    return None


class ProviderJevClient:
    """走 AstrBot 原生 DecisionProvider 的判断客户端，接口与直连客户端对齐。"""

    def __init__(
        self,
        provider_id: str,
        provider_resolver: Callable[[str], Awaitable[Any]],
        audit: Audit,
        question_builder: Callable[[str, str, Any], Any],
        *,
        max_retries: int = 0,
        failure_threshold: int = 3,
        backoff_seconds: float = 600.0,
    ) -> None:
        self.provider_id = provider_id
        self._resolver = provider_resolver
        self.audit = audit
        self._build_question = question_builder
        self.max_retries = max(0, int(max_retries))
        self._failures = 0
        self._cooldown_until = 0.0
        self._backoff_seconds = backoff_seconds
        self._failure_threshold = failure_threshold
        self._warned = False
        self.stats = {"calls": 0, "failures": 0, "provider": 0}

    @property
    def available(self) -> bool:
        return bool(self.provider_id)

    @property
    def protocol(self) -> str:
        return "provider"

    async def _resolve(self) -> Any:
        try:
            provider = await self._resolver(self.provider_id)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("解析判断 provider 失败：%s", exc)
            return None
        if provider is None or not hasattr(provider, "ask"):
            if not self._warned:
                self._warned = True
                self.audit.warn(
                    "判断 provider %s 不存在或不是 System One 类型", self.provider_id
                )
            return None
        return provider

    def _note_failure(self) -> None:
        self.stats["failures"] += 1
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._cooldown_until = time.monotonic() + self._backoff_seconds
            self._failures = 0
            self.audit.warn("判断 provider 连续失败，进入 %.0f 秒退避", self._backoff_seconds)

    def _note_success(self) -> None:
        self._failures = 0

    async def ask(
        self, state: str | Mapping[str, Any], questions: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]] | None:
        if not self.available or not questions:
            return None
        if time.monotonic() < self._cooldown_until:
            return None
        self.stats["calls"] += 1
        provider = await self._resolve()
        if provider is None:
            self._note_failure()
            return None

        built: dict[str, Any] = {}
        for name, question in questions.items():
            kind = str(question.get("type") or "").lower()
            try:
                built[name] = self._build_question(
                    kind,
                    str(question.get("instructions") or ""),
                    question.get("criteria"),
                )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("构造判断问题失败（%s）：%s", kind, exc)
                self._note_failure()
                return None
        try:
            raw = await provider.ask(
                built, state=state, request_max_retries=self.max_retries
            )
        except Exception as exc:  # noqa: BLE001 - DecisionError/网络错误统一降级
            self.audit.warn("判断 provider 调用失败：%s", exc)
            self._note_failure()
            return None
        if not isinstance(raw, Mapping):
            self._note_failure()
            return None

        normalized: dict[str, dict[str, Any]] = {}
        for name, question in questions.items():
            kind = str(question.get("type") or "").lower()
            value = normalize_provider_answer(
                kind, raw.get(name), question.get("criteria")
            )
            if value is None:
                self._note_failure()
                return None
            normalized[name] = value
        self.stats["provider"] += 1
        self._note_success()
        return normalized

    async def noul(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        criteria: Mapping[str, str] | None = None,
    ) -> float | None:
        question: dict[str, Any] = {"type": "noul", "instructions": instructions}
        if criteria:
            question["criteria"] = dict(criteria)
        answers = await self.ask(state, {"q": question})
        if not answers:
            return None
        return answers["q"].get("noul")

    async def choice(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        options: Mapping[str, str],
    ) -> dict[str, float] | None:
        if len(options) < 2:
            return None
        answers = await self.ask(
            state,
            {
                "q": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": dict(options),
                }
            },
        )
        if not answers:
            return None
        probabilities = answers["q"].get("probabilities")
        return dict(probabilities) if isinstance(probabilities, Mapping) else None

    async def score(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        levels: list[str],
    ) -> float | None:
        if not 2 <= len(levels) <= 10:
            return None
        answers = await self.ask(
            state,
            {"q": {"type": "score", "instructions": instructions, "criteria": list(levels)}},
        )
        if not answers:
            return None
        value = answers["q"].get("score")
        return float(value) if isinstance(value, (int, float)) else None


class JevRouter:
    """provider 优先、直连兜底的判断路由，接口与直连客户端对齐。"""

    def __init__(self, primary: Any, fallback: Any = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.stats = {"calls": 0, "failures": 0, "provider_calls": 0, "fallback_calls": 0}

    @property
    def available(self) -> bool:
        return bool(
            (self.primary is not None and self.primary.available)
            or (self.fallback is not None and self.fallback.available)
        )

    @property
    def protocol(self) -> str:
        parts: list[str] = []
        if self.primary is not None and self.primary.available:
            parts.append("provider")
        if self.fallback is not None and self.fallback.available:
            parts.append("direct")
        return "+".join(parts) or "none"

    async def ask(
        self, state: str | Mapping[str, Any], questions: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]] | None:
        self.stats["calls"] += 1
        if self.primary is not None and self.primary.available:
            result = await self.primary.ask(state, questions)
            if result is not None:
                self.stats["provider_calls"] += 1
                return result
            self.stats["fallback_calls"] += 1
        if self.fallback is not None and self.fallback.available:
            return await self.fallback.ask(state, questions)
        self.stats["failures"] += 1
        return None

    async def noul(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        criteria: Mapping[str, str] | None = None,
    ) -> float | None:
        question: dict[str, Any] = {"type": "noul", "instructions": instructions}
        if criteria:
            question["criteria"] = dict(criteria)
        answers = await self.ask(state, {"q": question})
        if not answers:
            return None
        return answers["q"].get("noul")

    async def choice(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        options: Mapping[str, str],
    ) -> dict[str, float] | None:
        if len(options) < 2:
            return None
        answers = await self.ask(
            state,
            {
                "q": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": dict(options),
                }
            },
        )
        if not answers:
            return None
        probabilities = answers["q"].get("probabilities")
        return dict(probabilities) if isinstance(probabilities, Mapping) else None

    async def score(
        self,
        state: str | Mapping[str, Any],
        instructions: str,
        levels: list[str],
    ) -> float | None:
        if not 2 <= len(levels) <= 10:
            return None
        answers = await self.ask(
            state,
            {"q": {"type": "score", "instructions": instructions, "criteria": list(levels)}},
        )
        if not answers:
            return None
        value = answers["q"].get("score")
        return float(value) if isinstance(value, (int, float)) else None
