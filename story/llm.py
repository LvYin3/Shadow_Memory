"""故事模块的模型调用：直接走 provider，输出为结构化 JSON。

场景不属于任何 QQ 会话，所以不经 AstrBot 主 Agent（那条路会把内容写进某个会话的
对话历史）。背景模型和场景中的她各有一个 provider，由调用方注入解析器。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..core.audit import Audit
from ..core.jsonx import extract_json

ROLE_GM = "gm"
ROLE_ACTOR = "actor"
ROLE_LABELS = {ROLE_GM: "背景模型", ROLE_ACTOR: "场景中的她"}

# (role, bot_id) -> provider；provider 需要有 ``text_chat(prompt=, system_prompt=, contexts=)``
ProviderResolver = Callable[[str, str], Awaitable[Any]]
# 返回 True 表示合格；返回 False 或一句原因表示不合格（原因会在重试时告诉模型）。
Validator = Callable[[dict[str, Any]], "bool | str"]

RETRY_FEEDBACK = "\n\n# 上一次输出不合格\n{reason}\n请按要求重新输出。"


class StoryLLMError(Exception):
    """模型不可用，或重试后仍没有给出合法输出。"""

    def __init__(self, message: str, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


class StoryLLM:
    def __init__(
        self, resolver: ProviderResolver, audit: Audit, *, retries: int = 1
    ) -> None:
        self._resolver = resolver
        self.audit = audit
        self.retries = max(0, int(retries))
        self.stats = {"calls": 0, "failures": 0}

    async def available(self, role: str, bot_id: str) -> bool:
        try:
            return await self._resolver(role, bot_id) is not None
        except Exception:  # noqa: BLE001 - 解析失败等同于不可用
            return False

    async def ask_json(
        self,
        role: str,
        bot_id: str,
        system: str,
        user: str,
        *,
        validate: Validator | None = None,
    ) -> tuple[dict[str, Any], int]:
        """返回（JSON 对象，实际调用次数）。输出不合法时重试，仍失败则抛错。"""
        label = ROLE_LABELS.get(role, role)
        try:
            provider = await self._resolver(role, bot_id)
        except Exception as exc:  # noqa: BLE001
            raise StoryLLMError(f"解析{label}的 provider 失败：{exc}") from exc
        if provider is None:
            raise StoryLLMError(f"没有可用的{label} provider")

        attempts = 0
        reason = ""
        feedback = ""
        for _ in range(1 + self.retries):
            attempts += 1
            self.stats["calls"] += 1
            # 重试时把上一次哪里不合格告诉模型，否则它多半会原样再错一遍。
            prompt = user + RETRY_FEEDBACK.format(reason=feedback) if feedback else user
            try:
                response = await provider.text_chat(
                    prompt=prompt, system_prompt=system, contexts=[]
                )
            except Exception as exc:  # noqa: BLE001 - 网络/限流等统一按失败重试
                reason = f"调用失败：{type(exc).__name__}: {exc}"
                self.audit.warn("故事模型调用失败（%s）：%s", label, exc)
                continue
            text = getattr(response, "completion_text", None)
            value = extract_json(text)
            if not isinstance(value, dict):
                reason = "输出不是 JSON 对象"
                feedback = "上一次没有只输出一个 JSON 对象。"
                self.audit.warn(
                    "故事模型输出不是 JSON 对象（%s）：%s", label, str(text or "")[:120]
                )
                continue
            verdict = validate(value) if validate is not None else True
            if verdict is not True:
                reason = verdict if isinstance(verdict, str) and verdict else "输出缺少必填内容"
                feedback = reason
                self.audit.warn(
                    "故事模型输出不合格（%s）：%s｜%s", label, reason, str(text or "")[:120]
                )
                continue
            self.audit.debug(
                "story_llm",
                role=role,
                bot_id=bot_id,
                attempts=attempts,
                system_chars=len(system),
                user_chars=len(user),
                out_chars=len(str(text or "")),
            )
            return value, attempts
        self.stats["failures"] += 1
        raise StoryLLMError(f"{label}{reason or '没有给出合法输出'}", attempts)
