"""跨模块共享的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Turn:
    bot_id: str
    umo: str
    role: str  # "user" | "assistant" | "system"
    text: str
    ts: int
    speaker_id: str = ""
    speaker_name: str = ""


@dataclass(slots=True)
class Candidate:
    event_id: int
    fragment: str
    kind: str
    salience: int
    storage_strength: float
    retrievability: float
    last_recalled_at: int | None
    ended_at: int
    degree: int = 0
    edge_weight: float = 0.0
    score: float = 0.0


@dataclass(slots=True)
class Injection:
    main: Candidate | None = None
    aux: str | None = None
    reason: str = "none_wins"
    p_top: float = 0.0
    p_none: float = 0.0
    candidates: int = 0


@dataclass(slots=True)
class PersonaEntry:
    id: int
    bot_id: str
    layer: str  # core | edge | tension
    text: str
    source_event_ids: list[int] = field(default_factory=list)
    created_day: int = 0
    last_edited_day: int = 0


@dataclass(slots=True)
class OpResult:
    applied: int = 0
    rejected: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConsolidationResult:
    clusters: int = 0
    merged: int = 0
    superseded: int = 0
    insights: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass(slots=True)
class DistillResult:
    applied: int = 0
    rejected: list[str] = field(default_factory=list)
    skipped: str = ""
