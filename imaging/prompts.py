"""生图提示词转译、两阶段选图、自动参考图生成与视觉审核提示词。"""

from __future__ import annotations

from typing import Any

PICK_REFS_SYSTEM = """你是影忆生图系统的参考图筛选器。请根据角色的「拍照意图」、当前情境和可选生效参考图目录，挑出本画面真正需要出镜的主体参考图 ID。

要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块，不要多余解释：
   {"ref_ids": [可选整数ID列表]}
2. 只选本张画面实际出镜的人物、场景或物件（例如拍自拍选角色本人 self，拍某地选对应 place）；画风参考图由程序固定附带，不在目录中。
3. 数量不要超过上限；如果没有需要用到的参考图，返回 {"ref_ids": []}。"""

TRANSLATE_SYSTEM = """你是影忆生图系统的画面转译师。你的任务是把角色的「拍照意图」结合当前情境、人设、世界设定与可用参考图，转译成一条可直接交给生图模型的高质量提示词。

要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块，不要多余解释：
   {"prompt": "详细的英文或中英混合生图提示词", "ref_ids": [可选整数ID列表], "aspect_ratio": "1:1|3:4|4:3|9:16|16:9"}
2. 构图与自拍视角：
   - 构图应自然贴合拍摄意图与当前光线；优先细致描写构图内实际可见的人物、动作、表情、服饰与环境，避免描写画面外看不见的部位（如半身图不必写鞋袜），也不要写“A or B”之类的二选一不确定描述。
   - **普通前置自拍规范**：镜头本身就是手机前置摄像头，请写成第一人称自拍视角（如 `selfie POV looking at the viewer, one arm extending forward out of frame toward the camera`），**不要**描写她手里举着手机对着自己，也不要把手机画进手里（除非拍摄意图明确要求“对镜自拍”）。
3. 画风规则（精准辨别技法，如实对齐，严禁串台）：
   - 若本次调用附带了画风参考图：请先仔细判断画风参考图的**轮廓边缘与笔触类型**（例如区分「无粗黑轮廓线 / 弱轮廓柔和厚涂、空气雾感、低对比柔和光晕」还是「清晰线稿平涂赛璐璐」还是「水彩 / 真实摄影」），只提取**纯绘画技法**（媒介、轮廓软硬度、笔触、饱和度、明暗对比、光晕与空气透视）精炼写在 prompt **最开头**；如果用户还提供了画风要点说明，请结合校验防止误判。**严禁**引入与画风参考图相冲突的媒介词（如画风图是柔和厚涂无黑描边时不要误写 crisp dark lineart，是二次元时不要写 realistic/photorealistic/DSLR；若画风图本身就是真实胶片摄影则如实描写摄影质感）。**严禁**把画风参考图里的任何具体内容、人物、衣物装饰（如袖章/徽章）、天气或潮湿反光地面抄进画风描述或画面里。
   - 若存在画风参考图但本次为纯文本调用（未附带图片）：如有用户提供的画风说明，请将其作为风格锚点写在 prompt 开头且不写冲突媒介词；如未提供说明，则**不要**自行猜测或编造任何画风/媒介词。
   - 若没有任何画风参考图：按世界设定、人设基调与拍摄意图自然决定画面风格。
4. 人物身份锁定与随季换装规则：
   - 对人物参考图（角色本人 `self` 或配角 `person`），只严格锁定其**身份特征**（脸型、五官、瞳色、发色发型、体型年龄）；
   - **着装必须随场景与天气/季节自适应**：优先严格采用拍摄意图、近期聊天、故事状态卡或主持人笔记中提到的当日穿着；若都未提及当日衣着，请**保留该人物在参考图中的穿衣审美基因与色系偏好**，但**必须依据当前现实日期的季节、昼夜与室内外温度自动换上合理厚度的服装**（例如 11 月底深秋/冬季户外夜晚应穿厚外套/大衣/毛衣/围巾/长裤或厚打底袜，不要死抄参考图里的轻薄夏装或光腿短裙）。
5. 参考图绑定由生图接口层自动按序号声明，**不要**在 prompt 末尾重复写“Strictly follow the attached style reference image...”之类的模板套话。
6. 严禁把「主持人笔记」或设定里尚未发生的未来事件画进画面，只提取已确立的外貌、服装、场景与物件视觉细节。
7. 画风参考图会由程序固定附带，不要在 ref_ids 里填画风；只从给定的「生效参考图目录」里选出本画面真正出镜的 ID（不要超过上限；若目录已预选锁定或为空，按提示填写）。"""

AUTO_REF_SYSTEM = """你是影忆生图系统的美术设定师。你的任务是为故事中新出现的人物、场景或物件编写一张「标准设定参考图」的生图提示词，方便后续拍照时保持外观一致。

要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块，不要多余解释：
   {"prompt": "清晰的设定参考图生图提示词", "aspect_ratio": "1:1|3:4|4:3|16:9"}
2. 画风规则：
   - 若本次附带了画风参考图：请准确辨别画风参考图的艺术媒介、轮廓边缘软硬度（无黑描边柔和厚涂 vs 清晰线稿）、笔触、上色与空气质感，精炼写在 prompt 开头；严禁引入冲突媒介词，严禁复制画风图里的原有主体、装饰徽章或天气场景；
   - 若存在画风参考图但未附带图片：有画风说明则前置，无说明则不自行猜测画风/媒介词；
   - 不必在末尾重复“严格遵循所附画风参考图”套话（由接口层自动声明）。
3. 人物：正面或半身清晰展示脸部、发型、服饰与气质，背景简洁干净。
4. 场景：环境全景或代表性视角，光线清晰，无杂乱前景人物遮挡。
5. 物件：主体居中特写，结构材质清晰，背景简洁。"""

REVIEW_SYSTEM = """你是影忆生图系统的视觉质检员。请仔细查看生成的图片，判断是否符合拍摄意图且没有明显崩坏。

检查项：
1. 主体内容是否符合拍摄意图与画面描述（拍的人/物/场景/动作/光线是否对得上）；
2. 是否存在严重画面崩坏（严重畸形肢体/多头/五官错位、乱码文字遮挡、纯黑纯白或完全跑题）；
3. 若一并附带了主体参考图，检查核心人物或物件的外观是否与参考图严重背离；
4. 画面风格以实际画风参考图或世界设定为准，不要因为画面是二次元、插画或非写实风格而判定不通过。

只输出一个 JSON 对象，不要 markdown 代码块，不要多余解释：
{"pass": true或false, "reason": "简短判定原因", "fix_hint": "若不通过，给提示词转译师的具体修正建议；通过则留空", "description": "一句话客观描述成图画面（不超过60字）"}"""


def build_pick_refs_user(
    *,
    scene_request: str,
    context_text: str,
    catalog: list[dict[str, Any]],
    max_subjects: int,
) -> str:
    lines: list[str] = [f"# 拍摄意图\n{scene_request.strip()}"]
    if context_text.strip():
        lines.append(f"# 当前情境与设定\n{context_text.strip()[:1600]}")
    cat_lines = [
        f"- id={item['id']}｜{item['category_label']}「{item['name']}」"
        + (f"：{item['description']}" if item.get("description") else "")
        for item in catalog
    ]
    lines.append(
        f"# 可选生效参考图目录（最多选 {max_subjects} 个出镜主体 ID）\n"
        + "\n".join(cat_lines)
    )
    return "\n\n".join(lines)


def build_translate_user(
    *,
    scene_request: str,
    context_text: str,
    catalog: list[dict[str, Any]],
    max_subjects: int,
    has_style_refs: bool,
    style_notes: list[str] | None = None,
    attached_refs: list[str] | None = None,
    locked_ref_ids: list[int] | None = None,
    retry_feedback: str = "",
) -> str:
    lines: list[str] = [f"# 拍摄意图\n{scene_request.strip()}"]
    if context_text.strip():
        lines.append(f"# 当前情境与设定\n{context_text.strip()}")

    notes = [str(n).strip() for n in (style_notes or []) if str(n).strip()]
    attached = [str(a).strip() for a in (attached_refs or []) if str(a).strip()]

    if attached:
        hint = (
            "# 本次附带给你查看的参考图（按顺序）\n"
            + "\n".join(f"- {item}" for item in attached)
            + "\n请准确辨别画风参考图的轮廓软硬（无黑描边柔和厚涂 vs 清晰线稿）、笔触、色调与光影质感，把纯绘画技法写在 prompt 最开头；"
            "严禁抄录画风参考图里的原有角色、袖章/衣饰细节、天气或地面元素。"
            "对角色参考图只锁定五官、发色发型与体型，服装必须结合当前季节/昼夜/室内外与故事上下文合理穿着。"
        )
        if notes:
            hint += "\n用户补充的画风要点（校验参考）：" + "；".join(notes)
        lines.append(hint)
    elif has_style_refs:
        if notes:
            lines.append(
                "# 画风参考图说明（纯文本模式）\n"
                "程序会固定附带画风参考图。用户标注的画风特征为："
                + "；".join(notes)
                + "。\n请将这些画风特征作为风格锚点放在 prompt 开头，严禁写入与之冲突的媒介词。"
            )
        else:
            lines.append(
                "# 画风参考图（纯文本模式）\n"
                "程序会固定附带画风参考图作为本次出图的唯一画风基准。\n"
                "你本次无法看到图片，请不要在 prompt 中自行猜测或编造任何画风、媒介或写实摄影词（如 realistic / photorealistic / photograph / DSLR 等），专注描写构图、人物、服饰、场景与光线。"
            )

    if locked_ref_ids is not None:
        lines.append(
            f"# 已选定的主体参考图 ID\n{locked_ref_ids}（ref_ids 请原样返回该列表，无需重新筛选）。"
        )
    elif catalog and max_subjects > 0:
        cat_lines = [
            f"- id={item['id']}｜{item['category_label']}「{item['name']}」"
            + (f"：{item['description']}" if item.get("description") else "")
            for item in catalog
        ]
        lines.append(
            f"# 可选生效参考图目录（最多选 {max_subjects} 个出镜主体 ID）\n"
            + "\n".join(cat_lines)
        )
    else:
        lines.append("# 可选生效参考图目录\n暂无，ref_ids 请返回 []。")

    if retry_feedback.strip():
        lines.append(
            f"# 上一次成图审核未通过，请据此修正提示词\n{retry_feedback.strip()}"
        )
    return "\n\n".join(lines)


def build_auto_ref_user(
    *,
    category_label: str,
    name: str,
    appearance: str,
    world_outline: str = "",
    has_style_refs: bool = False,
    style_notes: list[str] | None = None,
    attached_refs: list[str] | None = None,
) -> str:
    parts = [
        f"# 目标设定\n分类：{category_label}\n名称：{name}",
        f"# 外观与特征描述\n{appearance.strip() or '按世界观基调补全合理且有辨识度的细节。'}",
    ]
    if world_outline.strip():
        parts.append(f"# 世界基调\n{world_outline.strip()[:800]}")
    notes = [str(n).strip() for n in (style_notes or []) if str(n).strip()]
    attached = [str(a).strip() for a in (attached_refs or []) if str(a).strip()]
    if attached:
        hint = (
            "# 本次附带的画风参考图（按顺序）\n"
            + "\n".join(f"- {item}" for item in attached)
            + "\n请准确辨别其轮廓软硬、笔触、色调与质感并精炼写在 prompt 开头，严禁引入冲突媒介词，严禁复制画风图里的原有主体、装饰或场景。"
        )
        if notes:
            hint += "\n用户补充的画风要点（校验参考）：" + "；".join(notes)
        parts.append(hint)
    elif has_style_refs:
        if notes:
            parts.append(
                "# 画风参考图说明\n用户标注的画风特征为："
                + "；".join(notes)
                + "。请前置该风格特征并严禁引入冲突媒介词。"
            )
        else:
            parts.append(
                "# 画风参考图（纯文本模式）\n"
                "程序将固定附带画风参考图。请不要自行猜测或编造画风/媒介词（如 realistic / photorealistic / photograph 等），专注描写该设定自身的外观特征。"
            )
    return "\n\n".join(parts)


def build_review_user(
    *,
    scene_request: str,
    prompt: str,
    subjects: list[str],
    attached_ref_count: int = 0,
) -> str:
    lines = [
        f"# 拍摄意图\n{scene_request.strip()}",
        f"# 生图提示词\n{prompt.strip()}",
    ]
    if subjects:
        lines.append("# 出镜事物特征\n" + "\n".join(f"- {s}" for s in subjects))
    if attached_ref_count > 0:
        lines.append(
            f"# 图片顺序说明\n第 1 张为本次生成的成图；后面 {attached_ref_count} 张为对应的主体参考图。"
        )
    else:
        lines.append("# 图片顺序说明\n所附图片即为本次生成的成图。")
    return "\n\n".join(lines)
