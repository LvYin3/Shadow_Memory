"""下载网络图片：只允许公网 http(s)，每跳校验，流式限长。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Callable
from urllib.parse import urlparse

from ..core.http import HTTPClient


class MediaFetchError(ValueError):
    """下载被拒绝或失败，文案直接面向模型。"""


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def ensure_public_url(url: str, *, resolver: Callable[[str], list[str]] = default_resolver) -> None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise MediaFetchError("只支持 http(s) 链接。")
    host = parsed.hostname
    if not host:
        raise MediaFetchError("链接里没有地址。")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_address(str(literal)):
            raise MediaFetchError("这个链接不能取（内网地址）。")
        return
    try:
        addresses = resolver(host)
    except (OSError, UnicodeError) as exc:
        raise MediaFetchError("解析不了这个链接。") from exc
    if not addresses:
        raise MediaFetchError("解析不了这个链接。")
    for address in addresses:
        try:
            public = _is_public_address(address)
        except ValueError:
            public = False
        if not public:
            raise MediaFetchError("这个链接不能取（内网地址）。")


async def download_image(
    http: HTTPClient,
    url: str,
    *,
    max_bytes: int,
    timeout_s: float = 10.0,
    max_redirects: int = 3,
    validator: Callable[[str], None] = ensure_public_url,
) -> bytes:
    target = (url or "").strip()
    await asyncio.to_thread(validator, target)
    result = await http.get_bytes(
        target,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        max_redirects=max_redirects,
        validate=validator,
    )
    if result.error.startswith("validate:"):
        raise MediaFetchError(result.error.split(":", 1)[1])
    if not result.ok:
        raise MediaFetchError("这张图没取回来。")
    data = result.body if isinstance(result.body, bytes) else b""
    if not data:
        raise MediaFetchError("这张图没取回来。")
    if len(data) > max_bytes:
        raise MediaFetchError("这张图太大了。")
    return data
