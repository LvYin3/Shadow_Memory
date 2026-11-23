"""故事模块的模型调用：直接走 provider，输出为结构化 JSON。

场景不属于任何 QQ 会话，所以不经 AstrBot 主 Agent（那条路会把内容写进某个会话的
对话历史）。背景模型和场景中的她各有一个 provider，由调用方注入解析器。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from ..core.audit import Audit
from ..core.jsonx import extract_json

ROLE_GM = "gm"
ROLE_ACTOR = "actor"
ROLE_LABELS = {ROLE_GM: "背景模型", ROLE_ACTOR: "场景中的她"}

# (role, bot_id) -> provider；provider 需要有 ``text_chat(prompt=, system_prompt=, contexts=)``
ProviderResolver = Callable[[str, str], Awaitable[Any]]
# 需要思考时走直连写作模型；(system, user) -> 正文。没有直连地址就不要传。
DirectComplete = Callable[[str, str], Awaitable[str | None]]
# 返回 True 表示合格；返回 False 或一句原因表示不合格（原因会在重试时告诉模型）。
Validator = Callable[[dict[str, Any]], "bool | str"]

RETRY_FEEDBACK = "\n\n# 上一次输出不合格\n{reason}\n请按要求重新输出。"
TOOL_ROUNDS = 2
TOOL_RESULT_CHARS = 1500
MISSED = "没搜到"


class StoryLLMError(Exception):
    """模型不可用，或重试后仍没有给出合法输出。"""

    def __init__(self, message: str, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


class StoryLLM:
    def __init__(
        self,
        resolver: ProviderResolver,
        audit: Audit,
        *,
        retries: int = 1,
        direct: DirectComplete | None = None,
    ) -> None:
        self._resolver = resolver
        self.audit = audit
        self.retries = max(0, int(retries))
        self._direct = direct
        self._warned_thinking = False
        self.stats = {"calls": 0, "failures": 0}

    async def available(self, role: str, bot_id: str) -> bool:
        try:
            return await self._resolver(role, bot_id) is not None
        except Exception:  # noqa: BLE001 - 解析失败等同于不可用
            return False

    async def _chat(
        self,
        provider: Any,
        system: str,
        prompt: str,
        label: str,
        *,
        func_tool: Any = None,
        tool_calls_result: Any = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"prompt": prompt, "system_prompt": system, "contexts": []}
        if func_tool is not None:
            kwargs["func_tool"] = func_tool
        if tool_calls_result:
            kwargs["tool_calls_result"] = tool_calls_result
        try:
            return await provider.text_chat(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 网络/限流等统一按失败重试
            self.audit.warn("故事模型调用失败（%s）：%s", label, exc)
            return None

    async def _direct_text(self, system: str, prompt: str, label: str) -> str | None:
        if self._direct is None:
            return None
        try:
            return await self._direct(system, prompt)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("故事思考直连失败（%s）：%s", label, exc)
            return None

    def _skip_thinking(self, label: str) -> None:
        if self._warned_thinking:
            return
        self._warned_thinking = True
        self.audit.event("thinking_skipped", role=label, path="story")

    async def _complete(
        self,
        provider: Any,
        system: str,
        prompt: str,
        thinking: bool,
        label: str,
        *,
        search: Any = None,
        tools_then_think: bool = False,
    ) -> tuple[str | None, int]:
        """返回（正文，实际调用次数）。

        没挂搜索时：需要思考就先走直连，直连没有或失败再退回 provider。
        挂了搜索时：在 provider 上最多两轮工具。收场要思考，就先搜完再把资料交给直连写 JSON。
        """
        if search is None:
            if thinking:
                text = await self._direct_text(system, prompt, label)
                if text:
                    return text, 1
                self._skip_thinking(label)
            response = await self._chat(provider, system, prompt, label)
            text = getattr(response, "completion_text", None) if response is not None else None
            return text, 1
        return await self._complete_with_search(
            provider, system, prompt, label, search, tools_then_think=tools_then_think
        )

    async def _complete_with_search(
        self,
        provider: Any,
        system: str,
        prompt: str,
        label: str,
        search: Any,
        *,
        tools_then_think: bool,
    ) -> tuple[str | None, int]:
        materials: list[str] = []
        packed: list[Any] = []
        executed = 0
        calls = 0
        final_text: str | None = None
        stopped = False
        while executed < TOOL_ROUNDS:
            response = await self._chat(
                provider,
                system,
                prompt,
                label,
                func_tool=getattr(search, "func_tool", None),
                tool_calls_result=packed or None,
            )
            calls += 1
            if response is None:
                materials.append(MISSED)
                stopped = True
                break
            calls_now = _tool_calls(response)
            if not calls_now:
                final_text = getattr(response, "completion_text", None)
                stopped = True
                break
            contents: list[str] = []
            for name, arguments in calls_now:
                try:
                    raw = await search.run(name, arguments)
                except Exception as exc:  # noqa: BLE001 - 工具失败不当成这场失败
                    self.audit.warn("故事搜索失败（%s）：%s", name, exc)
                    raw = MISSED
                contents.append(_clip_tool(raw))
            materials.extend(contents)
            try:
                packed.append(search.pack(response, contents))
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("故事搜索结果没能交回模型：%s", exc)
                materials.append(MISSED)
                stopped = True
                break
            executed += 1
        if not stopped:
            # 两轮用完还在要工具：不再执行，按没搜到交给它写 JSON。
            materials.append(MISSED)
        if tools_then_think and self._direct is None:
            self._skip_thinking(label)
        if tools_then_think and self._direct is not None:
            directed = await self._direct_text(system, _with_materials(prompt, materials), label)
            calls += 1
            if directed:
                return directed, calls
        if final_text:
            return final_text, calls
        note = _with_materials(prompt, materials or [MISSED])
        response = await self._chat(provider, system, note, label)
        calls += 1
        text = getattr(response, "completion_text", None) if response is not None else None
        return text, calls

    async def ask_json(
        self,
        role: str,
        bot_id: str,
        system: str,
        user: str,
        *,
        validate: Validator | None = None,
        thinking: bool = False,
        search: Any = None,
        tools_then_think: bool = False,
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
            # 重试时把上一次哪里不合格告诉模型，否则它多半会原样再错一遍。
            prompt = user + RETRY_FEEDBACK.format(reason=feedback) if feedback else user
            text, used = await self._complete(
                provider,
                system,
                prompt,
                thinking,
                label,
                search=search,
                tools_then_think=tools_then_think,
            )
            attempts += used
            self.stats["calls"] += used
            if text is None:
                reason = "调用失败"
                continue
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


def _tool_calls(response: Any) -> list[tuple[str, dict[str, Any]]]:
    names = list(getattr(response, "tools_call_name", None) or [])
    if not names:
        return []
    args = list(getattr(response, "tools_call_args", None) or [])
    found: list[tuple[str, dict[str, Any]]] = []
    for index, name in enumerate(names):
        if not name:
            continue
        raw = args[index] if index < len(args) else {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        found.append((str(name), raw if isinstance(raw, dict) else {}))
    return found


def _clip_tool(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return MISSED
    if len(text) <= TOOL_RESULT_CHARS:
        return text
    return text[:TOOL_RESULT_CHARS] + "…"


def _with_materials(prompt: str, materials: list[str]) -> str:
    if not materials:
        return prompt
    return (
        prompt
        + "\n\n# 搜到的资料\n"
        + "\n".join(materials)
        + "\n工具没返回的部分不要编造。写成她能感知到的事，不要写你搜索过，不要贴原始链接。只输出 JSON。"
    )
