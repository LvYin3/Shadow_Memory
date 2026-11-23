"""静态检查：error_response 的状态码必须用关键字传入。

AstrBot 把 status_code 定成只能用关键字。第二个位置参数会再抛 TypeError，
原来的中文拒绝原因就到不了页面。
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "main.py"


def test_error_response_status_is_keyword():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "error_response"
        and len(node.args) > 1
    ]
    assert offenders == []
