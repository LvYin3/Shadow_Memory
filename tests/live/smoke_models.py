"""真实模型冒烟（手动运行，不进 CI）。

用法（在工作区根目录）：
    .venv\\Scripts\\python.exe -X utf8 astrbot_plugin_shadow_memory/tests/live/smoke_models.py

读取工作区根目录的 .env（或同名环境变量）：
- DSH_TEST_DECISION_BASE_URL / DSH_TEST_DECISION_API_KEY / DSH_TEST_DECISION_MODEL
- DSH_TEST_TEXT_BASE_URL / DSH_TEST_TEXT_API_KEY / DSH_TEST_TEXT_MODEL
- DSH_TEST_VISION_MODEL（默认 deepseek-flash）

它只探测协议与解析结果，不写数据库、不打印密钥。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from astrbot_plugin_shadow_memory.core.audit import Audit  # noqa: E402
from astrbot_plugin_shadow_memory.core.config import ConfigView  # noqa: E402
from astrbot_plugin_shadow_memory.core.decision import (  # noqa: E402
    JevClient,
    chat_url,
    systemone_url,
)
from astrbot_plugin_shadow_memory.core.http import HTTPClient, HTTPResult  # noqa: E402
from astrbot_plugin_shadow_memory.core.textgen import TextClient  # noqa: E402

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def load_env() -> dict[str, str]:
    data = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data.setdefault(key.strip(), value.strip())
    return data


class UrllibClient(HTTPClient):
    """标准库实现，避免冒烟脚本额外安装 httpx。"""

    async def post_json(self, url, payload, headers=None, timeout_s=5.0):
        return await asyncio.to_thread(self._post, url, payload, headers or {}, timeout_s)

    @staticmethod
    def _post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout_s: float):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                text = response.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
                return HTTPResult(status=response.status, body=parsed, text=text)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return HTTPResult(status=exc.code, text=text, error=f"HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            return HTTPResult(error=f"{type(exc).__name__}: {exc}")


def build_decision(env: dict[str, str]) -> JevClient:
    config = ConfigView(
        {
            "decision": {
                "base_url": env.get("DSH_TEST_DECISION_BASE_URL", ""),
                "api_key": env.get("DSH_TEST_DECISION_API_KEY", ""),
                "model": env.get("DSH_TEST_DECISION_MODEL", "decision-model"),
                "protocol": "auto",
                "timeout_ms": 8000,
            }
        },
        "",
    )
    return JevClient(config, UrllibClient(), Audit())


async def probe_systemone(env: dict[str, str]) -> None:
    base = env.get("DSH_TEST_DECISION_BASE_URL", "").strip()
    if not base:
        print("[decision] 未配置 base_url，跳过")
        return
    url = systemone_url(base)
    client = UrllibClient()
    started = time.perf_counter()
    result = await client.post_json(
        url,
        {
            "model": env.get("DSH_TEST_DECISION_MODEL", "decision-model"),
            "state": "用户说他明天要去北京出差。",
            "questions": {"q": {"type": "noul", "instructions": "Is this a durable fact?"}},
        },
        {"Authorization": f"Bearer {env.get('DSH_TEST_DECISION_API_KEY', '')}"},
        timeout_s=8.0,
    )
    elapsed = (time.perf_counter() - started) * 1000
    print(f"[decision] systemone {url} -> status={result.status} {elapsed:.0f}ms")
    if result.ok:
        print("[decision] systemone body:", json.dumps(result.body, ensure_ascii=False)[:300])
    else:
        print(f"[decision] systemone 不可用：{result.error or result.text[:160]}")


async def run_decision_checks(env: dict[str, str]) -> None:
    client = build_decision(env)
    print(f"[decision] configured protocol=auto base={client.base_url or '(empty)'}")
    if not client.available:
        print("[decision] 跳过：没有 base_url 或 model")
        return

    started = time.perf_counter()
    probability = await client.noul({"message": "用户说他明天要去北京出差。"}, "Is this durable?")
    print(
        f"[decision] noul={probability} protocol={client.protocol} "
        f"{(time.perf_counter() - started) * 1000:.0f}ms"
    )

    choice = await client.choice(
        {"message": "他提到下周要搬家。"},
        "Which tag fits?",
        {"none": "not worth remembering", "fact": "a durable fact", "smalltalk": "just chatter"},
    )
    print(f"[decision] choice={choice}")

    score = await client.score(
        {"message": "他第一次主动说了自己的事。"},
        "How significant is this?",
        ["1 trivial", "2 ordinary", "3 notable", "4 important", "5 pivotal"],
    )
    print(f"[decision] score={score} (0 基，对应 salience {None if score is None else round(score) + 1})")


async def run_text_check(env: dict[str, str]) -> None:
    base = env.get("DSH_TEST_TEXT_BASE_URL", "").strip()
    key = env.get("DSH_TEST_TEXT_API_KEY", "").strip()
    if not base or not key:
        print("[text] 跳过：没有配置直连站点")
        return
    config = ConfigView(
        {
            "text": {
                "base_url": base,
                "api_key": key,
                "model": env.get("DSH_TEST_TEXT_MODEL", "deepseek-chat"),
                "temperature": 0.4,
            }
        },
        "",
    )
    textgen = TextClient(config, UrllibClient(), Audit())
    started = time.perf_counter()
    payload = await textgen.generate_json(
        "你是一个人的记忆整理器。只输出 JSON。",
        "对话：\n小明: 我下周要去北京出差。\n我: 那你记得带厚衣服。\n\n"
        '请输出 {"fragment": "≤60字第一人称", "entities": ["实体"]}',
        schema_hint='{"fragment": string, "entities": string[]}',
    )
    elapsed = (time.perf_counter() - started) * 1000
    print(f"[text] {elapsed:.0f}ms parsed={payload}")


async def run_vision_check(env: dict[str, str]) -> None:
    base = env.get("DSH_TEST_TEXT_BASE_URL", "").strip()
    key = env.get("DSH_TEST_TEXT_API_KEY", "").strip()
    if not base or not key:
        print("[vision] 跳过：没有配置直连站点")
        return
    model = env.get("DSH_TEST_VISION_MODEL", "deepseek-flash").strip() or "deepseek-flash"
    config = ConfigView(
        {"text": {"base_url": base, "api_key": key, "model": model}}, ""
    )
    textgen = TextClient(config, UrllibClient(), Audit())
    started = time.perf_counter()
    caption = await textgen.describe_image(
        PNG_1PX,
        mime="image/png",
        prompt="用一句中文客观描述这张图里有什么。",
        model=model,
        max_chars=80,
        timeout_s=30.0,
        max_tokens=800,
        disable_thinking=True,
    )
    elapsed = (time.perf_counter() - started) * 1000
    print(f"[vision] model={model} {elapsed:.0f}ms caption={caption!r}")
    if caption:
        return
    # 失败时再裸调一次，把真实 HTTP 原因打出来（不打印密钥）
    data_uri = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode("ascii")
    result = await UrllibClient().post_json(
        chat_url(base),
        {
            "model": model,
            "max_tokens": 80,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "用一句中文客观描述这张图里有什么。"},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        },
        {"Authorization": f"Bearer {key}"},
        timeout_s=30.0,
    )
    detail = result.body if result.body is not None else result.text
    print(f"[vision] 直连探测 status={result.status} body={str(detail)[:400]}")


async def run_thinking_check(env: dict[str, str]) -> None:
    base = env.get("DSH_TEST_TEXT_BASE_URL", "").strip()
    key = env.get("DSH_TEST_TEXT_API_KEY", "").strip()
    if not base or not key:
        print("[thinking] 跳过：没有配置直连站点")
        return
    model = env.get("DSH_TEST_TEXT_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    url = chat_url(base)
    headers = {"Authorization": f"Bearer {key}"}
    for label, extra in (
        ("disabled", {"thinking": {"type": "disabled"}}),
        ("enabled-high", {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ):
        started = time.perf_counter()
        result = await UrllibClient().post_json(
            url,
            {
                "model": model,
                "max_tokens": 400,
                "temperature": 0.4,
                "messages": [{"role": "user", "content": "用一句中文写：今天风很大。"}],
                **extra,
            },
            headers,
            timeout_s=60.0,
        )
        elapsed = (time.perf_counter() - started) * 1000
        if not result.ok:
            print(
                f"[thinking] {label} status={result.status} "
                f"error={result.error or str(result.text)[:120]}"
            )
            continue
        body = result.body if isinstance(result.body, dict) else {}
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        print(
            f"[thinking] {label} {elapsed:.0f}ms "
            f"completion={usage.get('completion_tokens')} "
            f"reasoning={details.get('reasoning_tokens')} "
            f"content={(message.get('content') or '')[:40]!r}"
        )


async def main() -> int:
    env = load_env()
    print("== 判断协议探测 ==")
    await probe_systemone(env)
    print("== 判断 ==")
    await run_decision_checks(env)
    print("== 写作模型 ==")
    await run_text_check(env)
    print("== 思考模式 ==")
    await run_thinking_check(env)
    print("== 视觉描述 ==")
    await run_vision_check(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
