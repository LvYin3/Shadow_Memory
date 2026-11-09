"""最小 HTTP 抽象，便于测试替换。

运行期用 httpx（AstrBot 自带依赖）；测试注入假实现即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urljoin

REDIRECT_STATUS = {301, 302, 303, 307, 308}


@dataclass(slots=True)
class HTTPResult:
    status: int = 0
    body: Any = None
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error == ""


class HTTPClient(Protocol):
    async def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_s: float = 5.0,
    ) -> HTTPResult: ...

    async def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_s: float = 10.0,
        max_redirects: int = 3,
        validate: Callable[[str], Any] | None = None,
    ) -> HTTPResult: ...


class HttpxClient:
    """懒加载 httpx，避免在纯逻辑测试里也要求安装它。"""

    async def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_s: float = 5.0,
    ) -> HTTPResult:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - AstrBot 环境一定有
            return HTTPResult(error=f"httpx unavailable: {exc}")

        try:
            async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
                response = await client.post(url, json=payload, headers=headers or {})
        except Exception as exc:  # noqa: BLE001 - 网络异常统一降级
            return HTTPResult(error=f"{type(exc).__name__}: {exc}")

        text = response.text or ""
        body: Any = None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应
            body = None
        return HTTPResult(status=response.status_code, body=body, text=text)

    async def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_s: float = 10.0,
        max_redirects: int = 3,
        validate: Callable[[str], Any] | None = None,
    ) -> HTTPResult:
        """下载原始字节；手动跟随重定向，每一跳都可被 validate 拒绝。"""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            return HTTPResult(error=f"httpx unavailable: {exc}")

        current = url
        try:
            async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
                for _ in range(max(0, int(max_redirects)) + 1):
                    if validate is not None:
                        try:
                            validate(current)
                        except Exception as exc:  # noqa: BLE001 - 把校验原因带回调用方
                            return HTTPResult(error=f"validate:{exc}")
                    async with client.stream("GET", current) as response:
                        if response.status_code in REDIRECT_STATUS:
                            location = response.headers.get("location")
                            if not location:
                                return HTTPResult(
                                    status=response.status_code,
                                    error="redirect without location",
                                )
                            current = urljoin(current, location)
                            continue
                        if not 200 <= response.status_code < 300:
                            return HTTPResult(
                                status=response.status_code,
                                error=f"HTTP {response.status_code}",
                            )
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                return HTTPResult(
                                    status=response.status_code, error="too large"
                                )
                            chunks.append(chunk)
                        return HTTPResult(status=response.status_code, body=b"".join(chunks))
                return HTTPResult(error="too many redirects")
        except Exception as exc:  # noqa: BLE001
            return HTTPResult(error=f"{type(exc).__name__}: {exc}")
