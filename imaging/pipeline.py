"""生图核心流水线：上下文收集 → 提示词转译与选图 → 生图 API → 视觉审核与反馈重试。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.jsonx import extract_json
from ..story import card as story_card
from ..story import oracle as story_oracle
from ..story import prompts as story_prompts
from ..story import world as story_world
from ..story.store import StoryStore
from . import prompts as img_prompts
from .providers import GeneratedImage, ImageProviderClient, ProviderError, RefImageInput
from .refs import REF_CATEGORY_LABELS, RefStore

ConfigProvider = Callable[[str], ConfigView]
PromptTextFn = Callable[[str, str, str, str], Awaitable[str | None]]
PromptVisionFn = Callable[
    [str, str, str, str, list[tuple[bytes, str]]],
    Awaitable[tuple[str | None, bool, str]],
]
ReviewVisionFn = Callable[
    [str, str, list[tuple[bytes, str]], str, str], Awaitable[dict[str, Any] | None]
]
BaseCardFn = Callable[[str], Awaitable[tuple[str, str]]]
QuotaHook = Callable[[str], Awaitable[bool]]

TRANSLATE_REF_MODES = ("off", "style", "style_and_self", "selected")


class PipelineError(ValueError):
    """流水线失败；code 用于区分失败原因以便日志与角色口吻回复。"""

    def __init__(self, message: str, *, code: str = "failed", retries: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.retries = retries


@dataclass(slots=True)
class PhotoOutput:
    image: GeneratedImage
    prompt: str
    description: str
    ref_count: int
    retries: int
    review_status: str
    latency_ms: int
    camera_note: str = ""


@dataclass(slots=True)
class PhotoTestOutput:
    image: GeneratedImage
    prompt: str
    aspect_ratio: str
    refs: list[dict[str, Any]]
    context_text: str
    review: dict[str, Any] | None
    latency_ms: int
    translate_meta: dict[str, Any]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "是", "通过"}


def _translate_mode(cfg: ConfigView) -> str:
    raw = cfg.str("image.translate_ref_mode", "style_and_self").strip().lower()
    return raw if raw in TRANSLATE_REF_MODES else "style_and_self"


def _attached_labels(refs: list[RefImageInput]) -> list[str]:
    out: list[str] = []
    for idx, ref in enumerate(refs, start=1):
        role = (
            "画风参考图"
            if ref.category == "style"
            else f"{REF_CATEGORY_LABELS.get(ref.category, ref.category)}参考图"
        )
        line = f"第 {idx} 张：{role}「{ref.name}」"
        if ref.description:
            line += f"（{ref.description}）"
        out.append(line)
    return out


def _style_notes(style_refs: list[RefImageInput]) -> list[str]:
    return [ref.description for ref in style_refs if ref.description]


def _clean_ref_ids(raw: Any, valid_ids: set[int], limit: int) -> list[int]:
    if not isinstance(raw, list) or limit <= 0:
        return []
    picked: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            rid = int(item)
        except (TypeError, ValueError):
            continue
        if rid in seen or rid not in valid_ids:
            continue
        seen.add(rid)
        picked.append(rid)
        if len(picked) >= limit:
            break
    return picked


class PhotoPipeline:
    def __init__(
        self,
        *,
        db: Database,
        configs: ConfigProvider,
        refs: RefStore,
        client: ImageProviderClient,
        prompt_text: PromptTextFn,
        review_vision: ReviewVisionFn,
        audit: Audit,
        prompt_vision: PromptVisionFn | None = None,
        story_store: StoryStore | None = None,
        base_card: BaseCardFn | None = None,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.refs = refs
        self.client = client
        self._prompt_text = prompt_text
        self._prompt_vision = prompt_vision
        self._review_vision = review_vision
        self.audit = audit
        self.story_store = story_store
        self._base_card = base_card
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 上下文组装
    async def build_context(
        self,
        bot_id: str,
        *,
        scene_request: str,
        umo: str = "",
        recent_chat: str = "",
        story_live: dict[str, Any] | None = None,
        include_story: bool | None = None,
    ) -> str:
        """收集喂给提示词转译模型的完整上下文。

        - 故事开启且有世界数据时：时间 + 世界大纲 + 状态卡/本场实时转录 + 上场结尾
          + 命中设定集 + 相关配角 + 主持人笔记（约束只提取已确立视觉细节）+ 基础人设 + 近期聊天。
        - 故事关闭时：时间 + 基础人设 + 常驻背景 + 近期聊天。
        """
        now = self.now_fn()
        cfg = self.configs(bot_id)
        blocks: list[str] = [f"当前现实时间：{story_oracle.real_moment(now)['text']}"]

        if self._base_card is not None:
            try:
                card_text, _ = await self._base_card(bot_id)
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("读取基础人设失败：%s", exc)
                card_text = ""
            if card_text.strip():
                blocks.append(f"## 角色基础人设\n{card_text.strip()[:900]}")

        want_story = (
            cfg.bool("story.enabled", False) if include_story is None else bool(include_story)
        )
        story_used = False
        if self.story_store is not None and want_story:
            state = await self.story_store.state(bot_id)
            outline = str((state or {}).get("outline") or "").strip()
            if state is not None and outline:
                story_used = True
                blocks.append(f"## 世界大纲\n{outline[:1000]}")
                query_parts = [scene_request]

                if story_live:
                    live_transcript = str(story_live.get("transcript") or "").strip()
                    live_cast = str(story_live.get("cast") or "").strip()
                    if live_transcript:
                        blocks.append(f"## 本场实时经过（外显）\n{live_transcript[-1200:]}")
                        query_parts.append(live_transcript[-600:])
                    if live_cast:
                        blocks.append(f"## 本场在场配角\n{live_cast[:500]}")
                        query_parts.append(live_cast[:300])
                else:
                    card_obj = story_card.load_card(state.get("card"))
                    card_rendered = story_card.render_for_gm(card_obj, now)
                    if card_rendered:
                        blocks.append(f"## 当前状态卡\n{card_rendered}")
                        query_parts.append(card_rendered)
                    previous = await self.story_store.last_transcript(bot_id)
                    if previous:
                        tail_start = max(
                            (
                                idx
                                for idx, turn in enumerate(previous)
                                if turn.get("role") == "me"
                            ),
                            default=max(0, len(previous) - 2),
                        )
                        ending = story_prompts.render_transcript(
                            previous[tail_start:],
                            viewer="gm",
                            style=cfg.str("story.narration", ""),
                        )[:500]
                        if ending.strip():
                            blocks.append(f"## 最近一场结尾片段\n{ending.strip()}")
                            query_parts.append(ending)

                query = "\n".join(query_parts)
                canon = await self.story_store.canon(bot_id)
                hits = story_world.search(canon, query, limit=8)
                if hits:
                    canon_lines = [
                        f"- [{story_world.CATEGORY_LABELS.get(str(item.get('category')), '设定')}] "
                        f"{item['name']}：{item['body']}"
                        for item in hits
                    ]
                    blocks.append("## 命中的世界设定\n" + "\n".join(canon_lines))

                if not story_live:
                    npcs = await self.story_store.npcs(bot_id)
                    matched_npcs = [
                        row
                        for row in npcs
                        if str(row.get("status") or "") == "active"
                        and (
                            str(row.get("name") or "") in query
                            or len(npcs) <= 3
                        )
                    ][:5]
                    if matched_npcs:
                        npc_lines = [
                            f"- {row['name']}：{row['relationship']}"
                            for row in matched_npcs
                        ]
                        blocks.append("## 相关配角\n" + "\n".join(npc_lines))

                notes = await self.story_store.notes(bot_id)
                if notes:
                    budget = min(1200, cfg.int("story.notes_max_chars", 2000, lo=200, hi=6000))
                    note_lines: list[str] = []
                    used_chars = 0
                    for note in notes:
                        line = f"- {note['title']}：{note['body']}"
                        if used_chars + len(line) > budget:
                            break
                        note_lines.append(line)
                        used_chars += len(line)
                    if note_lines:
                        blocks.append(
                            "## 主持人笔记（仅可提取已确立的外观与环境细节，禁止画出尚未发生的剧情）\n"
                            + "\n".join(note_lines)
                        )

        if not story_used:
            ambient_rows = await self.db.fetchall(
                "SELECT text FROM ambient_memories WHERE bot_id=? AND active=1 "
                "ORDER BY importance DESC, updated_at DESC LIMIT 6",
                (bot_id,),
            )
            if ambient_rows:
                lines = [f"- {row['text']}" for row in ambient_rows if row.get("text")]
                if lines:
                    blocks.append("## 近期常驻背景\n" + "\n".join(lines))

        if recent_chat.strip():
            blocks.append(f"## 近期聊天上下文\n{recent_chat.strip()[-900:]}")
        elif umo and not story_live:
            msgs = await self.db.fetchall(
                "SELECT role, speaker, text FROM session_messages "
                "WHERE bot_id=? AND umo=? ORDER BY id DESC LIMIT 6",
                (bot_id, umo),
            )
            if msgs:
                chat_lines = [
                    f"- {row.get('speaker') or row.get('role')}：{str(row.get('text') or '')[:160]}"
                    for row in reversed(msgs)
                ]
                blocks.append("## 近期聊天上下文\n" + "\n".join(chat_lines))

        return "\n\n".join(blocks)

    # ------------------------------------------------------------------ 提示词转译
    async def _translate_photo(
        self,
        *,
        bot_id: str,
        umo: str,
        scene_request: str,
        context_text: str,
        catalog: list[dict[str, Any]],
        style_refs: list[RefImageInput],
        max_subjects: int,
        retry_feedback: str = "",
        retries: int = 0,
    ) -> tuple[str, str, list[Any], dict[str, Any]]:
        """执行提示词转译（按 translate_ref_mode 支持带图转译与纯文本原地降级）。"""
        cfg = self.configs(bot_id)
        mode = _translate_mode(cfg)
        max_translate_refs = cfg.int("image.translate_max_refs", 6, lo=1, hi=12)
        notes = _style_notes(style_refs)
        valid_ids = {int(item["id"]) for item in catalog}

        vision_refs: list[RefImageInput] = []
        locked_ref_ids: list[int] | None = None

        if mode != "off" and self._prompt_vision is not None:
            if mode == "style":
                vision_refs = style_refs[:max_translate_refs]
            elif mode == "style_and_self":
                self_refs = await self.refs.load_self_refs(bot_id)
                vision_refs = (style_refs + self_refs)[:max_translate_refs]
            elif mode == "selected":
                if catalog and max_subjects > 0:
                    pick_user = img_prompts.build_pick_refs_user(
                        scene_request=scene_request,
                        context_text=context_text,
                        catalog=catalog,
                        max_subjects=max_subjects,
                    )
                    pick_raw = await self._prompt_text(
                        bot_id, umo, img_prompts.PICK_REFS_SYSTEM, pick_user
                    )
                    pick_parsed = extract_json(pick_raw) if pick_raw else None
                    locked_ref_ids = _clean_ref_ids(
                        pick_parsed.get("ref_ids") if isinstance(pick_parsed, dict) else [],
                        valid_ids,
                        max_subjects,
                    )
                else:
                    locked_ref_ids = []
                style_loaded, subject_loaded = await self.refs.load_for_generation(
                    bot_id, locked_ref_ids
                )
                vision_refs = (style_loaded + subject_loaded)[:max_translate_refs]

        raw_plan: str | None = None
        used_vision = False
        fallback_reason = ""
        sent_ref_count = 0

        if vision_refs and self._prompt_vision is not None:
            vision_user = img_prompts.build_translate_user(
                scene_request=scene_request,
                context_text=context_text,
                catalog=catalog,
                max_subjects=max_subjects,
                has_style_refs=bool(style_refs),
                style_notes=notes,
                attached_refs=_attached_labels(vision_refs),
                locked_ref_ids=locked_ref_ids,
                retry_feedback=retry_feedback,
            )
            images_payload = [(r.data, r.mime) for r in vision_refs]
            v_text, v_ok, v_reason = await self._prompt_vision(
                bot_id, umo, img_prompts.TRANSLATE_SYSTEM, vision_user, images_payload
            )
            if v_ok and v_text:
                raw_plan = v_text
                used_vision = True
                sent_ref_count = len(vision_refs)
            else:
                fallback_reason = v_reason or "转译模型不支持视觉，已降级纯文本"

        if raw_plan is None:
            text_user = img_prompts.build_translate_user(
                scene_request=scene_request,
                context_text=context_text,
                catalog=catalog,
                max_subjects=max_subjects,
                has_style_refs=bool(style_refs),
                style_notes=notes,
                attached_refs=None,
                locked_ref_ids=locked_ref_ids,
                retry_feedback=retry_feedback,
            )
            raw_plan = await self._prompt_text(
                bot_id, umo, img_prompts.TRANSLATE_SYSTEM, text_user
            )

        plan = extract_json(raw_plan) if raw_plan else None
        if not isinstance(plan, dict) or not str(plan.get("prompt") or "").strip():
            raise PipelineError(
                "提示词转译模型未返回有效提示词",
                code="translate_failed",
                retries=retries,
            )
        gen_prompt = str(plan["prompt"]).strip()
        aspect_ratio = str(plan.get("aspect_ratio") or "").strip()
        camera_note = str(plan.get("camera_note") or "").strip()[:200]
        if locked_ref_ids is not None:
            selected_ids: list[Any] = list(locked_ref_ids)
        else:
            selected_ids = plan.get("ref_ids") if isinstance(plan.get("ref_ids"), list) else []
        meta = {
            "mode": mode,
            "used_vision": used_vision,
            "ref_count": sent_ref_count,
            "fallback_reason": fallback_reason,
            "camera_note": camera_note,
        }
        return gen_prompt, aspect_ratio, selected_ids, meta

    # ------------------------------------------------------------------ 拍照流水线
    async def run_photo(
        self,
        *,
        bot_id: str,
        provider: dict[str, Any],
        scene_request: str,
        umo: str = "",
        recent_chat: str = "",
        story_live: dict[str, Any] | None = None,
        consume_quota: QuotaHook | None = None,
    ) -> PhotoOutput:
        started = time.monotonic()
        cfg = self.configs(bot_id)
        max_retries = cfg.int("image.max_retries", 1, lo=0, hi=3)
        review_enabled = cfg.bool("image.review_enabled", True)
        review_include_refs = cfg.bool("image.review_include_refs", False)
        max_subjects = cfg.int("image.max_subject_refs", 4, lo=0, hi=12)
        max_bytes = int(cfg.float("image.max_image_mb", 8.0, lo=1.0, hi=30.0) * 1024 * 1024)

        context_text = await self.build_context(
            bot_id,
            scene_request=scene_request,
            umo=umo,
            recent_chat=recent_chat,
            story_live=story_live,
        )
        catalog = await self.refs.active_catalog(bot_id)
        style_refs = await self.refs.load_style_refs(bot_id)

        retry_feedback = ""
        retries = 0
        last_reason = ""
        for attempt in range(max_retries + 1):
            if consume_quota is not None and not await consume_quota(bot_id):
                raise PipelineError(
                    "今天的拍照额度用完了。", code="quota", retries=retries
                )

            gen_prompt, aspect_ratio, selected_ids, translate_meta = await self._translate_photo(
                bot_id=bot_id,
                umo=umo,
                scene_request=scene_request,
                context_text=context_text,
                catalog=catalog,
                style_refs=style_refs,
                max_subjects=max_subjects,
                retry_feedback=retry_feedback,
                retries=retries,
            )
            camera_note = str(translate_meta.get("camera_note") or "").strip()

            style_loaded, subject_refs = await self.refs.load_for_generation(
                bot_id, selected_ids
            )
            all_refs = style_loaded + subject_refs
            try:
                generated = await self.client.generate(
                    provider,
                    gen_prompt,
                    all_refs,
                    aspect_ratio=aspect_ratio,
                    max_bytes=max_bytes,
                )
            except ProviderError as exc:
                raise PipelineError(str(exc), code="provider_failed", retries=retries) from exc

            if not review_enabled:
                return PhotoOutput(
                    image=generated,
                    prompt=gen_prompt,
                    description="",
                    ref_count=len(all_refs),
                    retries=retries,
                    review_status="skipped",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    camera_note=camera_note,
                )

            review_images: list[tuple[bytes, str]] = [(generated.data, generated.mime)]
            attached_refs = 0
            if review_include_refs and subject_refs:
                for ref in subject_refs:
                    review_images.append((ref.data, ref.mime))
                attached_refs = len(subject_refs)

            subjects_desc = [
                f"{REF_CATEGORY_LABELS.get(ref.category, ref.category)}「{ref.name}」"
                + (f"：{ref.description}" if ref.description else "")
                for ref in subject_refs
            ]
            review_user = img_prompts.build_review_user(
                scene_request=scene_request,
                prompt=gen_prompt,
                subjects=subjects_desc,
                attached_ref_count=attached_refs,
                camera_note=camera_note,
            )
            verdict = await self._review_vision(
                bot_id, umo, review_images, img_prompts.REVIEW_SYSTEM, review_user
            )
            if not isinstance(verdict, dict) or "pass" not in verdict:
                self.audit.warn("视觉审核模型不可用或返回异常，阻断本次生图输出（bot=%s）", bot_id)
                raise PipelineError(
                    "视觉审核不可用，已阻断本次成图输出",
                    code="review_unavailable",
                    retries=retries,
                )
            if _as_bool(verdict.get("pass")):
                desc = str(verdict.get("description") or "").strip()[:120]
                return PhotoOutput(
                    image=generated,
                    prompt=gen_prompt,
                    description=desc,
                    ref_count=len(all_refs),
                    retries=retries,
                    review_status="passed",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    camera_note=camera_note,
                )

            last_reason = str(verdict.get("reason") or "画面与预期不符").strip()
            fix_hint = str(verdict.get("fix_hint") or "").strip()
            retry_feedback = (
                f"审核驳回原因：{last_reason}"
                + (f"\n修正建议：{fix_hint}" if fix_hint else "")
            )
            if attempt < max_retries:
                retries += 1
                self.audit.event(
                    "image_review_retry",
                    bot_id=bot_id,
                    attempt=attempt + 1,
                    reason=last_reason[:120],
                    camera_note=camera_note or None,
                )
                continue
            raise PipelineError(
                f"审核未通过：{last_reason}", code="review_rejected", retries=retries
            )

        raise PipelineError(last_reason or "拍照失败", code="failed", retries=retries)

    async def run_photo_test(
        self,
        *,
        bot_id: str,
        provider: dict[str, Any],
        scene_request: str,
        include_story: bool | None = None,
        recent_chat: str = "",
        run_review: bool = False,
    ) -> PhotoTestOutput:
        """WebUI 调试生图流水线：转译 + 参考图自动装配 + 生图；默认跳过审核，不重试。"""
        started = time.monotonic()
        cfg = self.configs(bot_id)
        max_subjects = cfg.int("image.max_subject_refs", 4, lo=0, hi=12)
        max_bytes = int(cfg.float("image.max_image_mb", 8.0, lo=1.0, hi=30.0) * 1024 * 1024)
        review_include_refs = cfg.bool("image.review_include_refs", False)

        context_text = await self.build_context(
            bot_id,
            scene_request=scene_request,
            recent_chat=recent_chat,
            include_story=include_story,
        )
        catalog = await self.refs.active_catalog(bot_id)
        style_refs = await self.refs.load_style_refs(bot_id)

        gen_prompt, aspect_ratio, selected_ids, translate_meta = await self._translate_photo(
            bot_id=bot_id,
            umo="",
            scene_request=scene_request,
            context_text=context_text,
            catalog=catalog,
            style_refs=style_refs,
            max_subjects=max_subjects,
        )

        style_loaded, subject_refs = await self.refs.load_for_generation(
            bot_id, selected_ids
        )
        all_refs = style_loaded + subject_refs
        try:
            generated = await self.client.generate(
                provider,
                gen_prompt,
                all_refs,
                aspect_ratio=aspect_ratio,
                max_bytes=max_bytes,
            )
        except ProviderError as exc:
            raise PipelineError(str(exc), code="provider_failed") from exc

        review_info: dict[str, Any] | None = None
        if run_review:
            review_images: list[tuple[bytes, str]] = [(generated.data, generated.mime)]
            attached_refs = 0
            if review_include_refs and subject_refs:
                for ref in subject_refs:
                    review_images.append((ref.data, ref.mime))
                attached_refs = len(subject_refs)
            subjects_desc = [
                f"{REF_CATEGORY_LABELS.get(ref.category, ref.category)}「{ref.name}」"
                + (f"：{ref.description}" if ref.description else "")
                for ref in subject_refs
            ]
            review_user = img_prompts.build_review_user(
                scene_request=scene_request,
                prompt=gen_prompt,
                subjects=subjects_desc,
                attached_ref_count=attached_refs,
                camera_note=str(translate_meta.get("camera_note") or "").strip(),
            )
            verdict = await self._review_vision(
                bot_id, "", review_images, img_prompts.REVIEW_SYSTEM, review_user
            )
            if not isinstance(verdict, dict) or "pass" not in verdict:
                review_info = {
                    "status": "unavailable",
                    "pass": False,
                    "reason": "视觉审核模型不可用或返回异常",
                    "fix_hint": "",
                    "description": "",
                }
            else:
                passed = _as_bool(verdict.get("pass"))
                review_info = {
                    "status": "passed" if passed else "rejected",
                    "pass": passed,
                    "reason": str(verdict.get("reason") or "").strip(),
                    "fix_hint": str(verdict.get("fix_hint") or "").strip(),
                    "description": str(verdict.get("description") or "").strip()[:120],
                }

        refs_used = [
            {
                "role": "style",
                "category": ref.category,
                "category_label": REF_CATEGORY_LABELS.get(ref.category, ref.category),
                "name": ref.name,
                "description": ref.description,
            }
            for ref in style_loaded
        ] + [
            {
                "role": "subject",
                "category": ref.category,
                "category_label": REF_CATEGORY_LABELS.get(ref.category, ref.category),
                "name": ref.name,
                "description": ref.description,
            }
            for ref in subject_refs
        ]
        return PhotoTestOutput(
            image=generated,
            prompt=gen_prompt,
            aspect_ratio=aspect_ratio,
            refs=refs_used,
            context_text=context_text,
            review=review_info,
            latency_ms=int((time.monotonic() - started) * 1000),
            translate_meta=translate_meta,
        )

    # ------------------------------------------------------------------ 自动参考图流水线
    async def run_auto_ref(
        self,
        *,
        bot_id: str,
        provider: dict[str, Any],
        category: str,
        name: str,
        appearance: str,
        consume_quota: QuotaHook | None = None,
    ) -> PhotoOutput:
        started = time.monotonic()
        cfg = self.configs(bot_id)
        max_bytes = int(cfg.float("image.max_image_mb", 8.0, lo=1.0, hi=30.0) * 1024 * 1024)
        cat_label = REF_CATEGORY_LABELS.get(category, category)

        world_outline = ""
        if self.story_store is not None:
            state = await self.story_store.state(bot_id)
            world_outline = str((state or {}).get("outline") or "")

        style_refs = await self.refs.load_style_refs(bot_id)
        if consume_quota is not None and not await consume_quota(bot_id):
            raise PipelineError("今日生图额度已用完", code="quota")

        mode = _translate_mode(cfg)
        max_translate_refs = cfg.int("image.translate_max_refs", 6, lo=1, hi=12)
        notes = _style_notes(style_refs)
        raw_plan: str | None = None
        if mode != "off" and style_refs and self._prompt_vision is not None:
            vision_styles = style_refs[:max_translate_refs]
            vision_user = img_prompts.build_auto_ref_user(
                category_label=cat_label,
                name=name,
                appearance=appearance,
                world_outline=world_outline,
                has_style_refs=True,
                style_notes=notes,
                attached_refs=_attached_labels(vision_styles),
            )
            v_text, v_ok, _ = await self._prompt_vision(
                bot_id,
                "",
                img_prompts.AUTO_REF_SYSTEM,
                vision_user,
                [(r.data, r.mime) for r in vision_styles],
            )
            if v_ok and v_text:
                raw_plan = v_text

        if raw_plan is None:
            user_prompt = img_prompts.build_auto_ref_user(
                category_label=cat_label,
                name=name,
                appearance=appearance,
                world_outline=world_outline,
                has_style_refs=bool(style_refs),
                style_notes=notes,
            )
            raw_plan = await self._prompt_text(
                bot_id, "", img_prompts.AUTO_REF_SYSTEM, user_prompt
            )
        plan = extract_json(raw_plan) if raw_plan else None
        if isinstance(plan, dict) and str(plan.get("prompt") or "").strip():
            gen_prompt = str(plan["prompt"]).strip()
            aspect_ratio = str(plan.get("aspect_ratio") or "").strip()
        else:
            gen_prompt = f"{cat_label}设定参考图，{name}：{appearance or '清晰展示外观细节'}"
            aspect_ratio = "3:4" if category == "person" else "1:1"

        try:
            generated = await self.client.generate(
                provider,
                gen_prompt,
                style_refs,
                aspect_ratio=aspect_ratio,
                max_bytes=max_bytes,
            )
        except ProviderError as exc:
            raise PipelineError(str(exc), code="provider_failed") from exc

        review_status = "skipped"
        description = (appearance or "").strip()[:200]
        if cfg.bool("image.review_enabled", True) and not cfg.bool(
            "image.ref.require_review", True
        ):
            # 关闭人工审核时，自动参考图必须通过视觉审核才可直接生效。
            review_user = img_prompts.build_review_user(
                scene_request=f"{cat_label}「{name}」设定参考图：{appearance}",
                prompt=gen_prompt,
                subjects=[f"{cat_label}「{name}」：{appearance}"],
            )
            verdict = await self._review_vision(
                bot_id,
                "",
                [(generated.data, generated.mime)],
                img_prompts.REVIEW_SYSTEM,
                review_user,
            )
            if not isinstance(verdict, dict) or not _as_bool(verdict.get("pass")):
                reason = (
                    str((verdict or {}).get("reason") or "视觉审核未通过或不可用").strip()
                )
                raise PipelineError(reason, code="review_rejected")
            review_status = "passed"
            if str(verdict.get("description") or "").strip():
                description = str(verdict["description"]).strip()[:200]

        return PhotoOutput(
            image=generated,
            prompt=gen_prompt,
            description=description,
            ref_count=len(style_refs),
            retries=0,
            review_status=review_status,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
