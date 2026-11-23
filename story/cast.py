"""配角：晋升登记、在场名单、给背景模型挂的工具。

建号要等场景提交。这一场里的休眠登记不腾出空位，不能用来当场把人叫醒。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

LINE_MIN = 40
LINE_MAX = 90
NAME_MAX = 40
RELATION_MAX = 120


def is_npc_bot(bot_id: str) -> bool:
    return str(bot_id or "").startswith("npc:")


def npc_bot_id(owner: str, seq: int) -> str:
    return f"npc:{owner}:{seq}"


def lines_of(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def persona_errors(persona: Any) -> list[str]:
    """三层各至少一条，每条 40 到 90 字。不合格的原因交回背景模型。"""
    if not isinstance(persona, dict):
        persona = {}
    errors: list[str] = []
    for layer in ("core", "edge", "tension"):
        rows = lines_of(persona.get(layer))
        if not rows:
            errors.append(f"{layer} 至少要有一条")
            continue
        for index, text in enumerate(rows):
            if not (LINE_MIN <= len(text) <= LINE_MAX):
                errors.append(f"{layer}[{index}] 长度 {len(text)} 不在 {LINE_MIN}-{LINE_MAX}")
    return errors


def build_persona_seed(name: str, relationship: str, persona: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "persona_seed/v1",
        "bot_hint": name,
        "persona": {
            "core": lines_of(persona.get("core")),
            "edge": lines_of(persona.get("edge")),
            "tension": lines_of(persona.get("tension")),
        },
        "facts": [
            {
                "kind": "profile",
                "subject_id": "",
                "text": relationship,
                "key_fact_type": "none",
                "salience": 3,
            }
        ],
    }


def clean_present(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        text = str(item or "").strip()[:NAME_MAX]
        if text and text not in names:
            names.append(text)
    return names


def render_roster(rows: list[dict[str, Any]], cap: int) -> str:
    active = [row for row in rows if row.get("status") == "active"]
    asleep = [row for row in rows if row.get("status") != "active"]
    lines = [f"# 配角（同时活跃最多 {cap} 个，现在 {len(active)} 个）"]
    if not rows:
        lines.append("还没有配角。龙套由你代演，不要为他们调用工具。")
    for row in active:
        lines.append(f"- {row['name']}：在场时可写进 present")
    for row in asleep:
        lines.append(f"- {row['name']}：休眠。写进 present 且还有空位才会醒来，否则仍由你代演")
    lines.append(
        "开场或某一轮的 JSON 可以带 present（名字数组）。不写就沿用上一份；写成空数组表示这轮起都离场。"
        "不认识的名字由你当龙套演。晋升用 promote_npc，让在场的人休眠用 retire_npc；都要等这场提交才生效。"
    )
    return "\n".join(lines)


class CastTools:
    """背景模型这一场的工具。晋升和休眠只记在 progress 上。"""

    def __init__(
        self,
        progress: Any,
        roster: list[dict[str, Any]],
        *,
        cap: int,
        web: Any = None,
        store: Any = None,
        bot_id: str = "",
    ) -> None:
        self.progress = progress
        self.roster = roster
        self.cap = max(1, int(cap))
        self.web = web
        self.store = store
        self.bot_id = bot_id
        self.func_tool = _func_tool(web)

    def active_count(self) -> int:
        return sum(1 for row in self.roster if row.get("status") == "active")

    async def run(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "promote_npc":
            return self.promote(arguments or {})
        if name == "retire_npc":
            return self.retire(arguments or {})
        if name == "read_story_notes":
            return await self.read_notes()
        if name == "write_story_note":
            return await self.write_note(arguments or {})
        if self.web is not None:
            return str(await self.web.run(name, arguments or {}))
        return "没有这个工具"

    def pack(self, response: Any, contents: list[str]) -> Any:
        if self.web is not None:
            return self.web.pack(response, contents)
        return _pack(response, contents)

    def promote(self, arguments: dict[str, Any]) -> str:
        name = str(arguments.get("name") or "").strip()[:NAME_MAX]
        relationship = " ".join(str(arguments.get("relationship") or "").split())
        if not name:
            return "没有写成。名字是空的。"
        if not relationship or len(relationship) > RELATION_MAX:
            return f"没有写成。和她的关系要写成一句话，1 到 {RELATION_MAX} 字。"
        persona = arguments.get("persona")
        if not isinstance(persona, dict):
            persona = {
                "core": arguments.get("core"),
                "edge": arguments.get("edge"),
                "tension": arguments.get("tension"),
            }
        errors = persona_errors(persona)
        if errors:
            return "没有写成。" + "；".join(errors)
        known = {str(row.get("name") or "") for row in self.roster}
        known.update(str(item.get("name") or "") for item in self.progress.nominations)
        if name in known:
            return f"「{name}」已经是配角。让他出场就写进 present，不必再晋升。"
        if self.active_count() + len(self.progress.nominations) >= self.cap:
            living = [str(row["name"]) for row in self.roster if row.get("status") == "active"]
            who = "、".join(living) or "还没有在场的人"
            return f"空位已满（最多 {self.cap} 个）。先用 retire_npc 点名让谁休眠。现在活跃的是：{who}。这场的休眠要等提交才腾出空位。"
        self.progress.nominations.append(
            {
                "name": name,
                "relationship": relationship[:RELATION_MAX],
                "seed": build_persona_seed(name, relationship[:RELATION_MAX], persona),
            }
        )
        return f"记下了。{name} 要等这场提交才真正出现，这一场仍由你来演。"

    def retire(self, arguments: dict[str, Any]) -> str:
        name = str(arguments.get("name") or "").strip()
        row = next((item for item in self.roster if item.get("name") == name), None)
        if row is None or row.get("status") != "active":
            return f"「{name or '（空）'}」不是正在活跃的配角，不用休眠。"
        if name not in self.progress.retirements:
            self.progress.retirements.append(name)
        return f"记下了。{name} 这场提交后休眠。这个空位要到下一场才能用。"

    async def read_notes(self) -> str:
        if self.store is None:
            return "现在读不了。"
        rows = await self.store.notes(self.bot_id)
        if not rows:
            return "还没有留给下一场的提示。"
        return "\n".join(f"## {row['title']}\n{row['body']}" for row in rows)

    async def write_note(self, arguments: dict[str, Any]) -> str:
        if self.store is None:
            return "现在写不了。"
        if int(getattr(self.progress, "note_writes", 0) or 0) >= 1:
            return "这一场已经留过一条提示。留给下一场的话要节制，下一次再写。"
        title = " ".join(str(arguments.get("title") or "").split())[:NAME_MAX]
        body = str(arguments.get("body") if arguments.get("body") is not None else "").strip()[:600]
        if not title:
            return "没有写成。标题是空的。"
        await self.store.apply_notes(
            self.bot_id, [{"title": title, "body": body}], None, 0
        )
        self.progress.note_writes = 1
        if not body:
            return f"删掉了「{title}」。"
        return f"记下了。下一场可以用 read_story_notes 读到「{title}」。"


def _func_tool(web: Any) -> Any:
    try:
        from astrbot.core.agent.tool import FunctionTool, ToolSet
    except ImportError:
        tool = SimpleNamespace(tools=[], empty=lambda: False)
        if web is not None:
            tool.tools.extend(getattr(web.func_tool, "tools", []))
        return tool
    toolset = ToolSet()
    toolset.add_tool(
        FunctionTool(
            name="promote_npc",
            description="提名一个新配角。只登记，这场提交后才建号；这一场仍由你代演。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "名字"},
                    "relationship": {"type": "string", "description": "和她的关系，一句话，不超过 120 字"},
                    "core": {"type": "string", "description": "核心人格一条，40 到 90 字，第一人称"},
                    "edge": {"type": "string", "description": "具体场合下的一条反应，40 到 90 字，第一人称"},
                    "tension": {"type": "string", "description": "还没想明白的一件事，40 到 90 字，第一人称"},
                },
                "required": ["name", "relationship", "core", "edge", "tension"],
            },
        )
    )
    toolset.add_tool(
        FunctionTool(
            name="retire_npc",
            description="让一个正在活跃的配角在这场提交后休眠。空位要到下一场才能用。",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "要休眠的配角名字"}},
                "required": ["name"],
            },
        )
    )
    toolset.add_tool(
        FunctionTool(
            name="read_story_notes",
            description="一次读完留给下一场的全部提示。不调用也能把这场演完。",
            parameters={"type": "object", "properties": {}},
        )
    )
    toolset.add_tool(
        FunctionTool(
            name="write_story_note",
            description="给下一场的自己留一条提示。一场最多一条，只写故事树上没有、下一场必须知道的事。正文留空表示删掉这条。",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "标题，不超过 40 字"},
                    "body": {"type": "string", "description": "正文，不超过 600 字；留空表示删除"},
                },
                "required": ["title"],
            },
        )
    )
    if web is not None:
        for item in getattr(web.func_tool, "tools", []):
            toolset.add_tool(item)
    return toolset


def _pack(response: Any, contents: list[str]) -> Any:
    try:
        from astrbot.core.agent.message import AssistantMessageSegment, ToolCallMessageSegment
        from astrbot.core.provider.entities import ToolCallsResult
    except ImportError:
        return {"contents": list(contents)}
    ids = list(getattr(response, "tools_call_ids", None) or [])
    blocks = [
        ToolCallMessageSegment(
            tool_call_id=ids[index] if index < len(ids) else str(index),
            content=text or "没做成",
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
