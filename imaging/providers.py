"""生图提供商协议适配器：OpenAI Images（generations / edits）、Gemini 原生与 OpenAI Chat 兼容。"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..core.audit import Audit
from ..core.decision import chat_url
from ..core.http import HTTPClient
from ..interaction.media_dir import IMAGE_MIME_HINTS, sniff_image_ext
from ..interaction.media_fetch import MediaFetchError, download_image, ensure_public_url

PROTOCOLS = ("openai_images", "gemini", "openai_chat")
PROTOCOL_LABELS = {
    "openai_images": "OpenAI Images（generations / edits）",
    "gemini": "Gemini 原生（generateContent）",
    "openai_chat": "OpenAI Chat 兼容（chat/completions）",
}

_DATA_URI_RE = re.compile(
    r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE
)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"(https?://[^\s<>'\"()]+)")


class ProviderError(ValueError):
    """生图提供商配置或请求失败。"""


@dataclass(slots=True)
class RefImageInput:
    category: str
    name: str
    description: str
    data: bytes
    mime: str
    filename: str = "ref.png"


@dataclass(slots=True)
class GeneratedImage:
    data: bytes
    mime: str
    ext: str
    revised_prompt: str = ""


def normalize_provider(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """规整单条生图提供商档案。"""
    src = dict(raw or {})
    protocol = str(src.get("protocol") or "openai_images").strip().lower()
    if protocol not in PROTOCOLS:
        protocol = "openai_images"
    try:
        timeout_s = max(5, min(300, int(float(src.get("timeout_s") or 60))))
    except (TypeError, ValueError):
        timeout_s = 60
    return {
        "id": str(src.get("id") or "").strip()[:64],
        "name": str(src.get("name") or "").strip()[:64],
        "protocol": protocol,
        "base_url": str(src.get("base_url") or "").strip(),
        "api_key": str(src.get("api_key") or "").strip(),
        "model": str(src.get("model") or "").strip()[:120],
        "size": str(src.get("size") or "").strip()[:32],
        "aspect_ratio": str(src.get("aspect_ratio") or "").strip()[:16],
        "quality": str(src.get("quality") or "").strip()[:32],
        "timeout_s": timeout_s,
    }


def pick_provider(
    providers: list[Any] | None, provider_id: str = ""
) -> dict[str, Any] | None:
    """按 ID 选提供商；留空时取第一条有效档案；显式指定但找不到返回 None。"""
    items = [
        normalize_provider(item)
        for item in (providers or [])
        if isinstance(item, Mapping)
    ]
    valid = [item for item in items if item["id"] and item["base_url"] and item["model"]]
    wanted = (provider_id or "").strip()
    if wanted:
        for item in valid:
            if item["id"] == wanted:
                return item
        return None
    return valid[0] if valid else None


def openai_images_url(base_url: str, *, edit: bool) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    suffix = "/images/edits" if edit else "/images/generations"
    if base.endswith(("/images/generations", "/images/edits")):
        return base.rsplit("/images/", 1)[0] + suffix
    if base.endswith("/v1"):
        return base + suffix
    return base + "/v1" + suffix


def gemini_generate_url(base_url: str, model: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base or not model:
        return ""
    if ":generateContent" in base:
        return base
    model_path = model if model.startswith("models/") else f"models/{model}"
    if base.endswith(("/v1beta", "/v1")):
        return f"{base}/{model_path}:generateContent"
    return f"{base}/v1beta/{model_path}:generateContent"


def _decode_image_bytes(raw_b64: str, max_bytes: int) -> GeneratedImage:
    cleaned = re.sub(r"\s+", "", raw_b64 or "")
    try:
        data = base64.b64decode(cleaned, validate=False)
    except (ValueError, TypeError) as exc:
        raise ProviderError("生图接口返回的 base64 无法解码") from exc
    return _wrap_image(data, max_bytes=max_bytes)


def _wrap_image(
    data: bytes, *, max_bytes: int, revised_prompt: str = ""
) -> GeneratedImage:
    if not data:
        raise ProviderError("生图接口返回了空图片")
    if len(data) > max_bytes:
        raise ProviderError(f"生图结果超过体积上限（{len(data)} bytes）")
    ext = sniff_image_ext(data)
    if ext is None:
        raise ProviderError("生图接口返回的不是有效图片格式")
    return GeneratedImage(
        data=data,
        mime=IMAGE_MIME_HINTS.get(ext, "image/png"),
        ext=ext,
        revised_prompt=revised_prompt,
    )


def _format_ref_role(idx: int, ref: RefImageInput) -> str:
    """生成清晰的参考图角色指令，彻底解耦画风参考与人物身份参考。"""
    if ref.category == "style":
        text = (
            f"Reference image {idx} [ART STYLE REFERENCE ONLY - {ref.name}]: "
            "Strictly replicate the brushwork, edge softness (match whether it is soft-edged "
            "lineless painterly or crisp-outline), atmospheric haze, color palette, tonal "
            "contrast, and lighting texture of this image. Ignore and do NOT copy any character, "
            "clothing patch/emblem, pose, weather, wet surface, or scene object from it."
        )
        if ref.description:
            text += f" Style note: {ref.description}"
        return text
    if ref.category in ("self", "person"):
        text = (
            f"Reference image {idx} [CHARACTER IDENTITY REFERENCE ONLY - {ref.category}: {ref.name}]: "
            "Preserve ONLY this character's face, eyes, hair color/style, and body build. "
            "Do NOT copy the outfit from this reference unless explicitly described in the scene "
            "(dress according to the scene prompt). Do NOT copy this image's lineart or coloring "
            "style — render this character 100% in the art style and brushwork of the ART STYLE REFERENCE."
        )
        if ref.description:
            text += f" Identity details: {ref.description}"
        return text
    text = (
        f"Reference image {idx} [SUBJECT REFERENCE - {ref.category}: {ref.name}]: "
        "Preserve the visual structure and appearance of this subject, rendered in the art style "
        "of the ART STYLE REFERENCE."
    )
    if ref.description:
        text += f" Details: {ref.description}"
    return text


def _prompt_with_style_directive(prompt: str, refs: list[RefImageInput]) -> str:
    """若附带画风参考图，在最终提示词前补一条强画风遵循与解耦声明。"""
    if not any(ref.category == "style" for ref in refs):
        return prompt
    return (
        "Render the entire image — including all reference characters and backgrounds — 100% in "
        "the exact brushwork, edge softness, atmospheric haze, coloring, and visual medium of the "
        "ART STYLE REFERENCE image(s) above. Do NOT adopt the lineart or shading style of any "
        "CHARACTER IDENTITY REFERENCE, do NOT default to photorealism unless the art style "
        "reference itself is photorealistic, and do NOT copy any subject, emblem, or weather from "
        "the art style reference.\n\n"
        f"{prompt}"
    )


def _openai_edits_prompt(prompt: str, refs: list[RefImageInput]) -> str:
    """OpenAI /images/edits 无逐图文本槽位，按文件序号把参考图职责拼到 prompt 头部。"""
    if not refs:
        return prompt
    lines = ["[Reference Image Roles]"]
    for idx, ref in enumerate(refs, start=1):
        lines.append(f"- Image {idx}: {_format_ref_role(idx, ref)}")
    body = _prompt_with_style_directive(prompt, refs)
    return "\n".join(lines) + "\n\n[Scene to Generate]\n" + body


def _extract_error(body: Any, text: str, status: int, error: str) -> str:
    if error:
        return error
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("status") or "").strip()
            if msg:
                return f"HTTP {status}: {msg[:240]}"
        elif isinstance(err, str) and err.strip():
            return f"HTTP {status}: {err.strip()[:240]}"
        msg = str(body.get("message") or "").strip()
        if msg:
            return f"HTTP {status}: {msg[:240]}"
    preview = (text or "").strip().replace("\n", " ")[:200]
    return f"HTTP {status}: {preview}" if preview else f"HTTP {status}"


class ImageProviderClient:
    def __init__(
        self,
        http: HTTPClient,
        audit: Audit,
        *,
        url_validator: Callable[[str], None] = ensure_public_url,
    ) -> None:
        self.http = http
        self.audit = audit
        self.url_validator = url_validator

    async def generate(
        self,
        provider: Mapping[str, Any],
        prompt: str,
        refs: list[RefImageInput] | None = None,
        *,
        aspect_ratio: str = "",
        size: str = "",
        max_bytes: int = 8 * 1024 * 1024,
    ) -> GeneratedImage:
        cfg = normalize_provider(provider)
        if not cfg["base_url"] or not cfg["model"]:
            raise ProviderError("生图提供商缺少 base_url 或 model")
        text = (prompt or "").strip()
        if not text:
            raise ProviderError("生图提示词为空")
        ref_list = list(refs or [])
        protocol = cfg["protocol"]
        if protocol == "openai_images":
            return await self._openai_images(
                cfg, text, ref_list, size=size or cfg["size"], max_bytes=max_bytes
            )
        if protocol == "gemini":
            return await self._gemini(
                cfg,
                text,
                ref_list,
                aspect_ratio=aspect_ratio or cfg["aspect_ratio"],
                max_bytes=max_bytes,
            )
        if protocol == "openai_chat":
            return await self._openai_chat(cfg, text, ref_list, max_bytes=max_bytes)
        raise ProviderError(f"不支持的生图协议：{protocol}")

    async def _download(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
        try:
            return await download_image(
                self.http,
                url,
                max_bytes=max_bytes,
                timeout_s=min(60.0, max(10.0, timeout_s)),
                max_redirects=3,
                validator=self.url_validator,
            )
        except MediaFetchError as exc:
            raise ProviderError(f"拉取生图结果 URL 失败：{exc}") from exc

    # ------------------------------------------------------------------ OpenAI Images
    async def _openai_images(
        self,
        cfg: dict[str, Any],
        prompt: str,
        refs: list[RefImageInput],
        *,
        size: str,
        max_bytes: int,
    ) -> GeneratedImage:
        headers: dict[str, str] = {}
        if cfg["api_key"]:
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        timeout = float(cfg["timeout_s"])

        if refs:
            url = openai_images_url(cfg["base_url"], edit=True)
            form_data: dict[str, Any] = {
                "model": cfg["model"],
                "prompt": _openai_edits_prompt(prompt, refs),
            }
            if size:
                form_data["size"] = size
            if cfg["quality"]:
                form_data["quality"] = cfg["quality"]
            field_name = "image[]" if len(refs) > 1 else "image"
            files = [
                (
                    field_name,
                    (
                        ref.filename or f"ref_{idx}.png",
                        ref.data,
                        ref.mime or "image/png",
                    ),
                )
                for idx, ref in enumerate(refs)
            ]
            result = await self.http.post_multipart(
                url, form_data, files, headers=headers, timeout_s=timeout
            )
        else:
            url = openai_images_url(cfg["base_url"], edit=False)
            headers["Content-Type"] = "application/json"
            payload: dict[str, Any] = {"model": cfg["model"], "prompt": prompt}
            if size:
                payload["size"] = size
            if cfg["quality"]:
                payload["quality"] = cfg["quality"]
            result = await self.http.post_json(
                url, payload, headers=headers, timeout_s=timeout
            )

        if not result.ok:
            raise ProviderError(
                _extract_error(result.body, result.text, result.status, result.error)
            )
        body = result.body
        if not isinstance(body, dict):
            raise ProviderError("OpenAI 生图接口未返回 JSON 对象")
        items = body.get("data")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ProviderError("OpenAI 生图响应缺少 data 字段")
        first = items[0]
        revised = str(first.get("revised_prompt") or "").strip()
        b64 = first.get("b64_json")
        if isinstance(b64, str) and b64.strip():
            img = _decode_image_bytes(b64, max_bytes=max_bytes)
            img.revised_prompt = revised
            return img
        img_url = first.get("url")
        if isinstance(img_url, str) and img_url.strip():
            raw = await self._download(img_url.strip(), max_bytes=max_bytes, timeout_s=timeout)
            return _wrap_image(raw, max_bytes=max_bytes, revised_prompt=revised)
        raise ProviderError("OpenAI 生图响应既没有 b64_json 也没有 url")

    # ------------------------------------------------------------------ Gemini 原生
    async def _gemini(
        self,
        cfg: dict[str, Any],
        prompt: str,
        refs: list[RefImageInput],
        *,
        aspect_ratio: str,
        max_bytes: int,
    ) -> GeneratedImage:
        url = gemini_generate_url(cfg["base_url"], cfg["model"])
        headers = {"Content-Type": "application/json"}
        if cfg["api_key"]:
            headers["x-goog-api-key"] = cfg["api_key"]
        parts: list[dict[str, Any]] = []
        for idx, ref in enumerate(refs, start=1):
            parts.append({"text": _format_ref_role(idx, ref)})
            parts.append(
                {
                    "inlineData": {
                        "mimeType": ref.mime or "image/png",
                        "data": base64.b64encode(ref.data).decode("ascii"),
                    }
                }
            )
        parts.append({"text": _prompt_with_style_directive(prompt, refs)})
        gen_cfg: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        if aspect_ratio:
            gen_cfg["imageConfig"] = {"aspectRatio": aspect_ratio}
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_cfg,
        }
        result = await self.http.post_json(
            url, payload, headers=headers, timeout_s=float(cfg["timeout_s"])
        )
        if not result.ok:
            raise ProviderError(
                _extract_error(result.body, result.text, result.status, result.error)
            )
        body = result.body
        if not isinstance(body, dict):
            raise ProviderError("Gemini 生图接口未返回 JSON 对象")
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError("Gemini 生图响应没有 candidates")
        revised_texts: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str) and part["text"].strip():
                    revised_texts.append(part["text"].strip())
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict):
                    data_b64 = inline.get("data")
                    mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
                    if isinstance(data_b64, str) and data_b64.strip() and (
                        not mime or mime.startswith("image/")
                    ):
                        img = _decode_image_bytes(data_b64, max_bytes=max_bytes)
                        img.revised_prompt = " ".join(revised_texts)[:300]
                        return img
        raise ProviderError("Gemini 响应中没有找到生成的图片")

    # ------------------------------------------------------------------ OpenAI Chat 兼容
    async def _openai_chat(
        self,
        cfg: dict[str, Any],
        prompt: str,
        refs: list[RefImageInput],
        *,
        max_bytes: int,
    ) -> GeneratedImage:
        url = chat_url(cfg["base_url"])
        headers = {"Content-Type": "application/json"}
        if cfg["api_key"]:
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        content: list[dict[str, Any]] = []
        for idx, ref in enumerate(refs, start=1):
            content.append({"type": "text", "text": _format_ref_role(idx, ref)})
            uri = (
                f"data:{ref.mime or 'image/png'};base64,"
                f"{base64.b64encode(ref.data).decode('ascii')}"
            )
            content.append({"type": "image_url", "image_url": {"url": uri}})
        content.append({"type": "text", "text": _prompt_with_style_directive(prompt, refs)})
        payload: dict[str, Any] = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": content}],
        }
        timeout = float(cfg["timeout_s"])
        result = await self.http.post_json(
            url, payload, headers=headers, timeout_s=timeout
        )
        if not result.ok:
            raise ProviderError(
                _extract_error(result.body, result.text, result.status, result.error)
            )
        body = result.body
        if not isinstance(body, dict):
            raise ProviderError("OpenAI Chat 生图接口未返回 JSON 对象")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("OpenAI Chat 生图响应缺少 choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderError("OpenAI Chat 生图响应缺少 message")

        # 兼容部分中转站把图片放在 message.images
        raw_images = message.get("images")
        if isinstance(raw_images, list):
            for entry in raw_images:
                img = await self._extract_from_chat_item(entry, max_bytes, timeout)
                if img is not None:
                    return img

        msg_content = message.get("content")
        if isinstance(msg_content, list):
            texts: list[str] = []
            for part in msg_content:
                if isinstance(part, dict):
                    img = await self._extract_from_chat_item(part, max_bytes, timeout)
                    if img is not None:
                        return img
                    if isinstance(part.get("text"), str):
                        texts.append(part["text"])
            msg_content = "\n".join(texts)

        if isinstance(msg_content, str) and msg_content.strip():
            match = _DATA_URI_RE.search(msg_content)
            if match:
                return _decode_image_bytes(match.group(2), max_bytes=max_bytes)
            md_match = _MD_IMG_RE.search(msg_content)
            if md_match:
                raw = await self._download(
                    md_match.group(1), max_bytes=max_bytes, timeout_s=timeout
                )
                return _wrap_image(raw, max_bytes=max_bytes)
            url_match = _URL_RE.search(msg_content)
            if url_match:
                raw = await self._download(
                    url_match.group(1), max_bytes=max_bytes, timeout_s=timeout
                )
                return _wrap_image(raw, max_bytes=max_bytes)
        raise ProviderError("OpenAI Chat 响应中没有找到图片内容")

    async def _extract_from_chat_item(
        self, item: Any, max_bytes: int, timeout: float
    ) -> GeneratedImage | None:
        if isinstance(item, str):
            m = _DATA_URI_RE.search(item)
            if m:
                return _decode_image_bytes(m.group(2), max_bytes=max_bytes)
            if item.startswith(("http://", "https://")):
                raw = await self._download(item, max_bytes=max_bytes, timeout_s=timeout)
                return _wrap_image(raw, max_bytes=max_bytes)
            return None
        if not isinstance(item, dict):
            return None
        if isinstance(item.get("b64_json"), str) and item["b64_json"].strip():
            return _decode_image_bytes(item["b64_json"], max_bytes=max_bytes)
        image_url = item.get("image_url")
        url_val = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url_val, str) and url_val.strip():
            m = _DATA_URI_RE.search(url_val)
            if m:
                return _decode_image_bytes(m.group(2), max_bytes=max_bytes)
            if url_val.startswith(("http://", "https://")):
                raw = await self._download(url_val, max_bytes=max_bytes, timeout_s=timeout)
                return _wrap_image(raw, max_bytes=max_bytes)
        return None
