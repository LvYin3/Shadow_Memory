from __future__ import annotations

import array
import os
import tempfile
import wave
from pathlib import Path

import pytest

from astrbot_plugin_shadow_memory.interaction.audio import normalize_wav_24k


def _write_wav(*, rate: int, channels: int, frames: int = 3200) -> Path:
    fd, name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    path = Path(name)
    samples = array.array("h")
    for index in range(frames):
        sample = int(12000 * ((index // 80) % 2 * 2 - 1))
        samples.extend([sample] * channels)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return path


def test_normalizes_32k_mono():
    source = _write_wav(rate=32000, channels=1)
    normalized = None
    try:
        normalized = normalize_wav_24k(source)
        with wave.open(str(normalized), "rb") as wav:
            assert wav.getframerate() == 24000
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getnframes() > 0
    finally:
        source.unlink(missing_ok=True)
        if normalized is not None:
            normalized.unlink(missing_ok=True)


def test_normalizes_48k_stereo():
    source = _write_wav(rate=48000, channels=2)
    normalized = None
    try:
        normalized = normalize_wav_24k(source)
        with wave.open(str(normalized), "rb") as wav:
            assert wav.getframerate() == 24000
            assert wav.getnchannels() == 1
            assert wav.getnframes() > 0
    finally:
        source.unlink(missing_ok=True)
        if normalized is not None:
            normalized.unlink(missing_ok=True)


def test_invalid_wav_is_rejected():
    fd, name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    source = Path(name)
    source.write_bytes(b"not a wav")
    try:
        with pytest.raises(Exception):
            normalize_wav_24k(source)
    finally:
        source.unlink(missing_ok=True)
