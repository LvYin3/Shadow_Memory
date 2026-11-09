"""静态校验 main.py 里的 LLM 工具与命令声明。

AstrBot 从 docstring 的 ``Args:`` 段生成 function-calling schema；
这里用与它相同的解析库提前把错误挡在加载之前。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

docstring_parser = pytest.importorskip("docstring_parser")

MAIN = Path(__file__).resolve().parents[2] / "main.py"
ALLOWED_TYPES = {"string", "number", "object", "array", "boolean"}
EXPECTED_TOOLS = {
    "say",
    "speak",
    "silent",
    "notes",
    "memo",
    "qzone",
    "media",
    "send_image",
    "memory_search",
}


def _decorator_attr(decorator: ast.AST) -> str:
    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
        return decorator.func.attr
    return ""


def _tree() -> ast.Module:
    return ast.parse(MAIN.read_text(encoding="utf-8"))


def _functions() -> list[ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.AsyncFunctionDef)
    ]


def _tool_functions() -> dict[str, ast.AsyncFunctionDef]:
    tools: dict[str, ast.AsyncFunctionDef] = {}
    for node in _functions():
        for decorator in node.decorator_list:
            if _decorator_attr(decorator) != "llm_tool":
                continue
            name = node.name
            if isinstance(decorator, ast.Call) and decorator.args:
                first = decorator.args[0]
                if isinstance(first, ast.Constant):
                    name = str(first.value)
            tools[name] = node
    return tools


def test_expected_tools_are_declared():
    assert set(_tool_functions()) == EXPECTED_TOOLS


def test_every_tool_param_has_documented_type():
    for name, node in _tool_functions().items():
        parsed = docstring_parser.parse(ast.get_docstring(node) or "")
        documented = {param.arg_name: param for param in parsed.params}
        params = [
            arg.arg
            for arg in node.args.args
            if arg.arg not in {"self", "event"}
        ]
        for param in params:
            assert param in documented, f"{name}: 参数 {param} 缺少 Args 条目"
            assert documented[param].type_name, f"{name}: 参数 {param} 缺少类型"
        for param in parsed.params:
            base = str(param.type_name or "").split("[")[0]
            assert base in ALLOWED_TYPES, f"{name}: 参数 {param.arg_name} 类型 {base} 不支持"
        assert parsed.description, f"{name}: 缺少工具描述"


def test_tools_have_events_first():
    for name, node in _tool_functions().items():
        args = [arg.arg for arg in node.args.args]
        assert args[:2] == ["self", "event"], name


def test_commands_have_docstrings():
    commands = []
    for node in _functions():
        for decorator in node.decorator_list:
            if _decorator_attr(decorator) == "command":
                commands.append(node)
    assert commands, "没有找到命令处理器"
    for node in commands:
        assert ast.get_docstring(node), f"命令 {node.name} 缺少 docstring"


def test_commands_are_admin_guarded():
    guarded = 0
    for node in _functions():
        decorators = [_decorator_attr(item) for item in node.decorator_list]
        if "command" not in decorators:
            continue
        for decorator in node.decorator_list:
            if _decorator_attr(decorator) != "permission_type":
                continue
            source = ast.dump(decorator)
            if "ADMIN" in source:
                guarded += 1
    assert guarded >= 4


def test_handlers_exist():
    handlers = {
        _decorator_attr(decorator)
        for node in _functions()
        for decorator in node.decorator_list
    }
    for name in (
        "event_message_type",
        "on_llm_request",
        "on_llm_response",
        "on_decorating_result",
        "after_message_sent",
        "on_astrbot_loaded",
        "on_platform_loaded",
    ):
        assert name in handlers, name


def test_no_duplicate_tool_names():
    names = [
        node.name
        for node in _functions()
        for decorator in node.decorator_list
        if _decorator_attr(decorator) == "llm_tool"
    ]
    assert len(names) == len(set(names))
