"""第一人称交流环境提示词：平台、场景、对方与 say 工具说明。"""

from __future__ import annotations

DEFAULT_RULES = (
    "- 我说文字消息必须调用 say 函数（say(text)）；不调用 say，我写的话就发不出去。\n"
    "- 比如对方说“在吗”，我就调 say(text=\"在吗\")，不写正文。\n"
    "- say/speak 的 text 只放正文，不要输出 <text> 或任何 XML/HTML 标签。"
)


def platform_label(platform: str) -> str:
    name = (platform or "").strip()
    if not name:
        return "即时消息"
    if name == "aiocqhttp":
        return "QQ（aiocqhttp）"
    return name


def say_example_messages(call_id: str = "shadow_memory_say_demo") -> list[dict]:
    """一次性的 say 工具调用示例，用于覆盖上下文里的纯文本 assistant 先例。"""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "say",
                        "arguments": '{"text": "在的"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "已发送"},
    ]


def environment_parts(
    *,
    platform: str,
    chat_type: str,
    group_name: str = "",
    group_id: str = "",
    sender_name: str = "",
    rules: str = "",
    enabled: bool = True,
    compact: bool = False,
    life_lines: list[str] | None = None,
) -> tuple[str, str]:
    """拆成（稳定规则, 状态卡）。

    单独拼回去时，状态卡仍在这块的末尾。聊天注入会把规则放在会变的人格前面，
    状态卡放到整段系统提示的最后。
    """
    if not enabled:
        return "", ""
    life = [line for line in (life_lines or []) if line and line.strip()]
    life_block = "\n".join(["# 我自己这边", *life]) if life else ""
    chat_type = (chat_type or "other").lower()
    label = platform_label(platform)
    lead = " " if label and ord(label[0]) < 128 else ""

    lines = ["# 我这会儿"]
    who = f"正跟{sender_name}聊天。" if sender_name else "正跟人聊天。"
    lines.append(f"- 我在{lead}{label}上，{who}")
    if chat_type == "group":
        scene = f"群聊「{group_name or group_id or '这个群'}」里"
        lines.append(f"- 这会儿在{scene}。")
    elif chat_type == "private":
        lines.append("- 这会儿是私聊。")
    else:
        lines.append("- 这会儿在别的会话里。")

    if compact:
        lines.append("- 我说文字消息必须调用 say 函数；比如对方说“在吗”，我就调 say(text=\"在吗\")。")
        lines.append("- say/speak 的 text 只放正文，不要输出 <text> 或任何 XML/HTML 标签。")
    else:
        body = (rules or "").strip() or DEFAULT_RULES
        lines.append("")
        lines.append(body)
    return "\n".join(lines), life_block


def build_environment_prompt(
    *,
    platform: str,
    chat_type: str,
    group_name: str = "",
    group_id: str = "",
    sender_name: str = "",
    rules: str = "",
    enabled: bool = True,
    compact: bool = False,
    life_lines: list[str] | None = None,
) -> str:
    """生成第一人称环境块；只说明场景与 say，不规定该场景下的动作。

    ``life_lines`` 是故事模块给的状态卡（她此刻在哪、在做什么）。它一天会变很多次，
    所以放在整块的最后，前面不变的部分仍能命中提示词缓存。
    """
    rules_block, life_block = environment_parts(
        platform=platform,
        chat_type=chat_type,
        group_name=group_name,
        group_id=group_id,
        sender_name=sender_name,
        rules=rules,
        enabled=enabled,
        compact=compact,
        life_lines=life_lines,
    )
    if rules_block and life_block:
        return f"{rules_block}\n\n{life_block}"
    return rules_block or life_block
