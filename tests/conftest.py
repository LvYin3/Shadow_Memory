"""共享夹具。

测试不依赖 AstrBot 进程，也不使用系统临时目录（沙箱下不可写），
所有临时文件都落在工作区内的 ``.pytest_tmp/``。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from astrbot_plugin_shadow_memory.core.config import ConfigView
from astrbot_plugin_shadow_memory.core.db import Database

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workdir():
    path = ROOT / ".pytest_tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def db(workdir):
    database = Database(workdir / "memory.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def make_configs():
    def factory(overrides: dict | None = None):
        data = overrides or {}

        def configs(bot_id: str = "") -> ConfigView:
            return ConfigView(data, bot_id)

        return configs

    return factory


@pytest.fixture
def configs(make_configs):
    return make_configs()
