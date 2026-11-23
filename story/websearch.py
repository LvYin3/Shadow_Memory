"""给背景模型挂上 AstrBot 已经启用的网页搜索。

不走聊天主 Agent，避免写进某个会话的历史。只挂当前启用的那一家搜索，
以及它自带的打开网页；选取和主 Agent 的网页搜索一致。没开搜索就什么都不挂。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def story_search(star_context: Any, umo: str = "") -> Any | None:
    """返回带 func_tool / run / pack 的一套工具；没开或没有可用工具时返回 None。"""
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.tools.web_search_tools import normalize_legacy_web_search_config

    cfg = star_context.get_config(umo=umo) or {}
    normalize_legacy_web_search_config(cfg)
    settings = cfg.get("provider_settings") or {}
    if not settings.get("web_search"):
        return None

    toolset = ToolSet()
    tool_mgr = star_context.get_llm_tool_manager()
    for cls in _tool_classes(str(settings.get("websearch_provider") or "tavily")):
        tool = tool_mgr.get_builtin_tool(cls)
        if tool is not None:
            toolset.add_tool(tool)
    if toolset.empty():
        return None
    return _SearchBundle(star_context, toolset, umo)


def _tool_classes(provider: str) -> list[type]:
    from astrbot.core.tools import web_search_tools as tools

    mapping: dict[str, list[type]] = {
        "tavily": [tools.TavilyWebSearchTool, tools.TavilyExtractWebPageTool],
        "bocha": [tools.BochaWebSearchTool],
        "brave": [tools.BraveWebSearchTool],
        "firecrawl": [tools.FirecrawlWebSearchTool, tools.FirecrawlExtractWebPageTool],
        "baidu_ai_search": [tools.BaiduWebSearchTool],
        "exa": [tools.ExaWebSearchTool, tools.ExaGetContentsTool],
        "anysearch": [tools.AnySearchWebSearchTool],
        "deepseek": [tools.DeepSeekWebSearchTool, tools.DeepSeekFetchWebPageTool],
        "serpapi": [
            tools.SerpApiWebSearchTool,
            tools.SerpApiNewsSearchTool,
            tools.SerpApiImageSearchTool,
            tools.SerpApiFinanceSearchTool,
            tools.SerpApiScholarSearchTool,
        ],
    }
    return mapping.get(provider, [])


class _SearchBundle:
    def __init__(self, star_context: Any, toolset: Any, umo: str = "") -> None:
        self.func_tool = toolset
        self._star = star_context
        self._toolset = toolset
        self._umo = umo

    def _context(self) -> SimpleNamespace:
        # 传机器人的探测 unified_msg_origin，让 get_config 走该机器人的配置文件路由，不碰真实会话。
        return SimpleNamespace(
            context=SimpleNamespace(
                context=self._star,
                event=SimpleNamespace(unified_msg_origin=self._umo),
            )
        )

    async def run(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._toolset.get_tool(name)
        if tool is None:
            return ""
        result = await tool.call(self._context(), **(arguments or {}))
        return _as_text(result)

    def pack(self, response: Any, contents: list[str]) -> Any:
        from astrbot.core.agent.message import AssistantMessageSegment, ToolCallMessageSegment
        from astrbot.core.provider.entities import ToolCallsResult

        ids = list(getattr(response, "tools_call_ids", None) or [])
        blocks = [
            ToolCallMessageSegment(
                tool_call_id=ids[index] if index < len(ids) else str(index),
                content=text or "没搜到",
            )
            for index, text in enumerate(contents)
        ]
        spoken = getattr(response, "completion_text", None) or None
        return ToolCallsResult(
            tool_calls_info=AssistantMessageSegment(
                tool_calls=response.to_openai_tool_calls_model(),
                content=spoken,
            ),
            tool_calls_result=blocks,
        )


def _as_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            piece = getattr(block, "text", None)
            if piece:
                parts.append(str(piece))
        return "\n".join(parts)
    return str(result)
