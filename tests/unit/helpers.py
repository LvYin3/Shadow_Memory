"""把记忆模块的服务栈拼起来，供单元/集成测试复用。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.core.db import Database
from astrbot_plugin_shadow_memory.interaction.gate import Gate
from astrbot_plugin_shadow_memory.interaction.notes import NoteService
from astrbot_plugin_shadow_memory.interaction.sessions import SessionCatalog
from astrbot_plugin_shadow_memory.memory.consolidate import Consolidator
from astrbot_plugin_shadow_memory.memory.decay import Decayer
from astrbot_plugin_shadow_memory.memory.distill import Distiller
from astrbot_plugin_shadow_memory.memory.dynamics import Dynamics
from astrbot_plugin_shadow_memory.memory.edges import EdgeRepo
from astrbot_plugin_shadow_memory.memory.facts import FactService
from astrbot_plugin_shadow_memory.memory.insights import InsightRepo
from astrbot_plugin_shadow_memory.memory.persona import PersonaService
from astrbot_plugin_shadow_memory.memory.recall import RecallService
from astrbot_plugin_shadow_memory.memory.seed import SeedService
from astrbot_plugin_shadow_memory.memory.segmenter import Segmenter
from astrbot_plugin_shadow_memory.memory.service import MemoryService

from ..fakes import FakeJev, FakeText


def _quiet_audit() -> Audit:
    logger = logging.getLogger("pm-test")
    logger.setLevel(logging.CRITICAL)
    return Audit(logger)


def build_stack(
    db: Database,
    configs,
    *,
    text: Any = None,
    jev: Any = None,
) -> SimpleNamespace:
    audit = _quiet_audit()
    text = text if text is not None else FakeText()
    jev = jev if jev is not None else FakeJev()
    dynamics = Dynamics(configs(""))
    edges = EdgeRepo(db)
    persona = PersonaService(db, configs, audit)
    insights = InsightRepo(db)
    facts = FactService(db, configs, dynamics, text, audit)
    segmenter = Segmenter(db, configs, dynamics, edges, text, audit)
    recall = RecallService(db, configs, dynamics, edges, jev, persona, audit)
    distiller = Distiller(db, configs, persona, insights, text, audit)
    consolidator = Consolidator(db, configs, dynamics, edges, persona, insights, facts, text, audit)
    decayer = Decayer(db, configs, dynamics, facts, audit)
    seed = SeedService(db, configs, persona, facts, edges, dynamics, audit)
    notes = NoteService(db)
    catalog = SessionCatalog(db, configs)
    service = MemoryService(
        db=db,
        configs=configs,
        dynamics=dynamics,
        edges=edges,
        persona=persona,
        insights=insights,
        facts=facts,
        segmenter=segmenter,
        recall=recall,
        distiller=distiller,
        consolidator=consolidator,
        decayer=decayer,
        seed=seed,
        jev=jev,
        textgen=text,
        audit=audit,
    )
    gate = Gate(db, configs, jev, persona, audit)
    return SimpleNamespace(
        db=db,
        configs=configs,
        audit=audit,
        text=text,
        jev=jev,
        dynamics=dynamics,
        edges=edges,
        persona=persona,
        insights=insights,
        facts=facts,
        segmenter=segmenter,
        recall=recall,
        distiller=distiller,
        consolidator=consolidator,
        decayer=decayer,
        seed=seed,
        notes=notes,
        catalog=catalog,
        service=service,
        gate=gate,
    )


def text_of_len(length: int, seed: str = "我") -> str:
    base = "会在意那些安静的时刻，也会把话说得慢一点"
    body = seed
    while len(body) < length:
        body += base
    return body[:length]
