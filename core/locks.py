"""按命名空间/键分配 asyncio.Lock。"""

from __future__ import annotations

import asyncio
from collections import defaultdict


class LockPool:
    def __init__(self) -> None:
        self._locks: dict[str, dict[str, asyncio.Lock]] = defaultdict(dict)

    def get(self, namespace: str, key: str) -> asyncio.Lock:
        bucket = self._locks[namespace]
        lock = bucket.get(key)
        if lock is None:
            lock = asyncio.Lock()
            bucket[key] = lock
        return lock
