"""第一人称交流环境提示词：平台、场景、对方与 say 工具说明。"""

from __future__ import annotations

DEFAULT_RULES = (
    "- 我说文字消息必须调用 say 函数（say(text)）；不调用 say，我写的话就发不出去。\n"
    "- 比如对方说“在吗”，我就调 say(text=\"在吗\")，不写正文。"
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
) -> str:
    """生成第一人称环境块；只说明场景与 say，不规定该场景下的动作。"""
    if not enabled:
        return ""
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
        return "\n".join(lines)

    body = (rules or "").strip() or DEFAULT_RULES
    lines.append("")
    lines.append(body)
    return "\n".join(lines)
