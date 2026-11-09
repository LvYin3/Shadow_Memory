"""测试替身：不 import astrbot，只复刻被插件依赖的属性契约。"""

from __future__ import annotations

from typing import Any

from astrbot_plugin_shadow_memory.core.http import HTTPResult


class FakeChain:
    def __init__(self, text: str = "", kind: str = "plain") -> None:
        self.text = text
        self.kind = kind

    def __repr__(self) -> str:
        return f"FakeChain({self.kind}:{self.text!r})"


class FakeResult:
    def __init__(self, text: str = "", is_llm: bool = True) -> None:
        self.chain: list[Any] = [FakeChain(text)]
        self._is_llm = is_llm

    def is_llm_result(self) -> bool:
        return self._is_llm


class FakeGroup:
    def __init__(self, name: str = "测试群", group_id: str = "g1") -> None:
        self.group_name = name
        self.group_id = group_id


class FakeMessageObj:
    def __init__(self, group: FakeGroup | None = None, message_id: str = "m1") -> None:
        self.group = group
        self.group_id = group.group_id if group else ""
        self.message_id = message_id
        self.message: list[Any] = []


class FakeImage:
    type = "image"

    def __init__(self, data: str = "IMAGE") -> None:
        self._data = data

    async def convert_to_base64(self) -> str:
        return self._data


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = False
        self.response: Any = {"tid": "tid-1", "time": 1}

    async def call_action(self, action: str, **payload: Any) -> Any:
        self.calls.append((action, dict(payload)))
        if self.fail:
            raise RuntimeError("backend rejected")
        return self.response


class FakeEvent:
    def __init__(
        self,
        text: str = "",
        *,
        platform: str = "aiocqhttp",
        platform_name: str = "aiocqhttp",
        message_type: str = "GroupMessage",
        sender: str = "小明",
        sender_id: str = "u1",
        session_id: str = "g1",
        wake: bool = False,
        group: bool = True,
    ) -> None:
        self.message_str = text
        self.platform = platform
        self.platform_name = platform_name
        self._message_type = message_type
        self._sender = sender
        self._sender_id = sender_id
        self._session_id = session_id
        self.is_at_or_wake_command = wake
        self._extras: dict[str, Any] = {}
        self.sent: list[Any] = []
        self._result: FakeResult | None = None
        self.bot = FakeBot()
        self.message_obj = FakeMessageObj(FakeGroup() if group else None)

    # ---- 身份
    def get_platform_id(self) -> str:
        return self.platform

    def get_platform_name(self) -> str:
        return self.platform_name

    @property
    def unified_msg_origin(self) -> str:
        return f"{self.platform}:{self._message_type}:{self._session_id}"

    def get_message_type(self) -> str:
        return self._message_type

    def is_private_chat(self) -> bool:
        return self._message_type == "FriendMessage"

    def get_sender_name(self) -> str:
        return self._sender

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return self._session_id if self._message_type == "GroupMessage" else ""

    # ---- extras
    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        if key is None:
            return dict(self._extras)
        return self._extras.get(key, default)

    # ---- 发送
    def plain_result(self, text: str) -> FakeResult:
        return FakeResult(text)

    def chain_result(self, chain: list[Any]) -> FakeResult:
        result = FakeResult("")
        result.chain = list(chain)
        return result

    async def send(self, chain: Any) -> None:
        self.sent.append(chain)

    def get_result(self) -> FakeResult | None:
        return self._result

    def set_result(self, result: FakeResult) -> None:
        self._result = result


class FakeJev:
    """可编程的判断模型替身。"""

    def __init__(
        self,
        *,
        available: bool = True,
        choice: dict[str, float] | None = None,
        noul: float | None = 0.9,
        score: float | None = 2.0,
        ask: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.available = available
        self.choice_value = choice
        self.noul_value = noul
        self.score_value = score
        self.ask_value = ask
        self.calls: list[tuple[str, Any]] = []
        self.stats = {"calls": 0}

    async def ask(self, state, questions):
        self.calls.append(("ask", questions))
        if self.ask_value is not None:
            return self.ask_value
        answers = {}
        for name, question in questions.items():
            kind = question.get("type")
            if kind == "score":
                answers[name] = {"score": self.score_value}
            elif kind == "choice":
                answers[name] = {"choice": "none", "probabilities": {"none": 1.0}}
            else:
                answers[name] = {"noul": self.noul_value}
        return answers

    async def choice(self, state, instructions, options):
        self.calls.append(("choice", dict(options)))
        if self.choice_value is not None:
            return dict(self.choice_value)
        keys = list(options)
        return {key: (0.9 if index == 1 else 0.1 / max(1, len(keys) - 1)) for index, key in enumerate(keys)}

    async def noul(self, state, instructions, criteria=None):
        self.calls.append(("noul", instructions))
        return self.noul_value

    async def score(self, state, instructions, levels):
        self.calls.append(("score", levels))
        return self.score_value


class FakeText:
    def __init__(self, queue: list[Any] | None = None, text: str = "") -> None:
        self.queue = list(queue or [])
        self.text = text
        self.calls: list[tuple[str, str]] = []
        self.tasks: list[str] = []

    def push(self, value: Any) -> None:
        self.queue.append(value)

    async def generate_json(
        self, system, user, *, bot_id="", umo="", schema_hint="", task=""
    ):
        self.tasks.append(task)
        self.calls.append((system[:20], user[:40]))
        if not self.queue:
            return None
        return self.queue.pop(0)

    async def generate(self, system, user, *, bot_id="", umo="", task=""):
        self.tasks.append(task)
        self.calls.append((system[:20], user[:40]))
        if self.queue:
            value = self.queue.pop(0)
            return value if isinstance(value, str) else None
        return self.text or None


class FakeHTTP:
    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.get_results: dict[str, Any] = {}
        self.get_calls: list[str] = []
        self.default = HTTPResult(status=500, error="no route")

    def set(self, url: str, result: Any) -> None:
        self.results[url] = result

    def set_get(self, url: str, result: Any) -> None:
        self.get_results[url] = result

    async def post_json(self, url, payload, headers=None, timeout_s=5.0):
        self.calls.append((url, payload))
        value = self.results.get(url)
        if value is None:
            return self.default
        if callable(value):
            return value(payload)
        return value

    async def get_bytes(
        self, url, *, max_bytes, timeout_s=10.0, max_redirects=3, validate=None
    ):
        self.get_calls.append(url)
        if validate is not None:
            try:
                validate(url)
            except Exception as exc:  # noqa: BLE001 - 模拟真实客户端的校验拒绝
                return HTTPResult(error=f"validate:{exc}")
        value = self.get_results.get(url)
        if value is None:
            return self.default
        if callable(value):
            value = value()
        if isinstance(value, HTTPResult):
            if isinstance(value.body, bytes) and len(value.body) > max_bytes:
                return HTTPResult(status=value.status, error="too large")
            return value
        if isinstance(value, bytes):
            if len(value) > max_bytes:
                return HTTPResult(status=200, error="too large")
            return HTTPResult(status=200, body=value)
        return HTTPResult(status=500, error="bad fake")


class FakeTTS:
    def __init__(self, path: str = "/tmp/voice.silk", fail: bool = False) -> None:
        self.path = path
        self.fail = fail
        self.calls: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("tts down")
        return self.path
