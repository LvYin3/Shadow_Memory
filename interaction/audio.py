"""官方 QQ 语音的 WAV 规范化与 SILK 预检。"""

from __future__ import annotations

import asyncio
import array
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path


def _temp_path(prefix: str, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return Path(name)


def _convert_wav(source: Path, target: Path) -> None:
    with wave.open(str(source), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if width != 2:
        raise ValueError(f"unsupported WAV sample width: {width}")
    samples = array.array("h")
    samples.frombytes(frames)
    if channels > 1:
        mono = array.array("h")
        for index in range(0, len(samples), channels):
            chunk = samples[index : index + channels]
            mono.append(int(sum(chunk) / channels))
        samples = mono
    if rate != 24_000 and samples:
        ratio = rate / 24_000
        length = max(1, int(len(samples) / ratio))
        resampled = array.array("h")
        for index in range(length):
            position = index * ratio
            left = min(int(position), len(samples) - 1)
            right = min(left + 1, len(samples) - 1)
            fraction = position - left
            value = samples[left] * (1.0 - fraction) + samples[right] * fraction
            resampled.append(max(-32768, min(32767, int(value))))
        samples = resampled
        rate = 24_000

    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())


def normalize_wav_24k(source: str | Path) -> Path:
    """把 WAV 转成 QQ 官方需要的 24kHz/单声道/16-bit PCM WAV。"""
    source_path = Path(source)
    target_path = _temp_path("shadow_voice_", ".wav")
    try:
        _convert_wav(source_path, target_path)
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    return target_path


async def normalize_audio_for_official(source: str | Path) -> Path:
    """优先用标准库处理 WAV；非 WAV 时退回 ffmpeg。"""
    source_path = Path(source)
    try:
        return normalize_wav_24k(source_path)
    except Exception:
        if not shutil.which("ffmpeg"):
            raise
        target_path = _temp_path("shadow_voice_", ".wav")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            "-hide_banner",
            str(target_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        code = await proc.wait()
        if code != 0 or not target_path.exists() or target_path.stat().st_size == 0:
            target_path.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg 语音转换失败（exit={code}）")
        return target_path


async def preflight_tencent_silk(wav_path: str | Path) -> tuple[bool, str]:
    """在 event.send 前验证 AstrBot 的 SILK 转换，避免吞异常。"""
    try:
        import importlib

        helper = importlib.import_module(
            "astrbot.core.utils.tencent_record_helper"
        )
        wav_to_tencent_silk = getattr(helper, "wav_to_tencent_silk")
    except Exception:
        return True, "preflight_unavailable"
    out = _temp_path("shadow_voice_", ".silk")
    try:
        duration = await wav_to_tencent_silk(str(wav_path), str(out))
        if duration <= 0 or not out.exists() or out.stat().st_size == 0:
            return False, "SILK 转换结果为空"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc) or repr(exc)
    finally:
        out.unlink(missing_ok=True)
