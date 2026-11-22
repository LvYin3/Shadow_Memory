"""神谕：程序只掌握随机数，由背景模型负责解释。

- 裁定骰：背景模型申报可能性，程序掷出成败（外加小概率的转折）。
- 偶然种子：每次唤醒开场抽一个事件焦点和两个意象词。
- 现实时刻：日期、星期、时段、公历固定节日。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# 可能性档位 → 成功概率
LIKELIHOODS: dict[str, float] = {
    "几乎不可能": 0.05,
    "不太可能": 0.25,
    "五五开": 0.50,
    "很可能": 0.75,
    "几乎必然": 0.95,
}
DEFAULT_LIKELIHOOD = "五五开"

_ALIASES: dict[str, str] = {
    "不可能": "几乎不可能",
    "极不可能": "几乎不可能",
    "almost_never": "几乎不可能",
    "impossible": "几乎不可能",
    "不大可能": "不太可能",
    "可能性不大": "不太可能",
    "unlikely": "不太可能",
    "一半一半": "五五开",
    "五五": "五五开",
    "even": "五五开",
    "可能": "很可能",
    "大概率": "很可能",
    "likely": "很可能",
    "必然": "几乎必然",
    "几乎肯定": "几乎必然",
    "almost_certain": "几乎必然",
    "certain": "几乎必然",
}

# “成，但有代价 / 没成，但有转机”的出现概率
TWIST_CHANCE = 0.15

FOCUSES: tuple[str, ...] = (
    "与主线无关的旁人",
    "旧事重提",
    "环境变化",
    "身体与状态",
    "一件小物",
    "计划被打乱",
    "意外的空闲",
    "远处传来的消息",
    "熟人的近况",
    "主线向前一步",
    "一次小小的选择",
    "什么也没发生的平常",
)

WORDS: tuple[str, ...] = (
    "钥匙", "雨", "借", "迟到", "气味", "排队", "旧照片", "修理", "走错", "安静",
    "重复", "多出来的", "少了一个", "声音", "热", "冷", "等待", "门", "账单", "礼物",
    "误会", "顺路", "停电", "落下的东西", "陌生人", "动物", "食物", "颜色", "台阶", "窗",
    "约定", "取消", "提前", "拥挤", "空座位", "纸条", "电话", "影子", "风", "灰尘",
    "新开的", "关门了", "涨价", "打折", "问路", "帮忙", "拒绝", "道歉", "夸奖", "抱怨",
    "谣言", "规矩", "例外", "失而复得", "换位置", "绕路", "停下来", "加快", "慢下来", "回头",
    "数字", "名字", "方言", "音乐", "广播", "告示", "包裹", "零钱", "伞", "鞋",
    "伤口", "困", "饿", "镜子", "屏幕", "电量", "信号", "队伍", "角落", "屋顶",
)

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 只认公历固定日期的节日；农历节日（春节、中秋等）算不出来，不在这里。
_HOLIDAYS: dict[tuple[int, int], str] = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (4, 1): "愚人节",
    (5, 1): "劳动节",
    (6, 1): "儿童节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
    (12, 31): "跨年夜",
}


def parse_likelihood(value: Any) -> str:
    """把模型写的可能性归到五个档位之一；认不出来按五五开。"""
    text = str(value or "").strip().lower().replace(" ", "")
    if text in LIKELIHOODS:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    for name in LIKELIHOODS:
        if name in text:
            return name
    return DEFAULT_LIKELIHOOD


@dataclass(frozen=True, slots=True)
class Roll:
    likelihood: str
    chance: float
    value: float
    success: bool
    twist: bool

    @property
    def label(self) -> str:
        if self.success:
            return "成了，但有代价" if self.twist else "成了"
        return "没成，但有转机" if self.twist else "没成"

    def as_dict(self) -> dict[str, Any]:
        return {
            "likelihood": self.likelihood,
            "chance": self.chance,
            "value": round(self.value, 4),
            "success": self.success,
            "twist": self.twist,
            "label": self.label,
        }


def roll(likelihood: Any, rng: random.Random) -> Roll:
    name = parse_likelihood(likelihood)
    chance = LIKELIHOODS[name]
    value = rng.random()
    twist = rng.random() < TWIST_CHANCE
    return Roll(
        likelihood=name, chance=chance, value=value, success=value < chance, twist=twist
    )


@dataclass(frozen=True, slots=True)
class ChanceSeed:
    focus: str
    words: tuple[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"focus": self.focus, "words": list(self.words)}


def draw_seed(rng: random.Random) -> ChanceSeed:
    first, second = rng.sample(WORDS, 2)
    return ChanceSeed(focus=rng.choice(FOCUSES), words=(first, second))


def period_of(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 8:
        return "清晨"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def real_moment(now_ms: int) -> dict[str, Any]:
    """现实里的此刻（本地时区）。故事时间与它 1:1。"""
    moment = datetime.fromtimestamp(now_ms / 1000.0)
    weekday = _WEEKDAYS[moment.weekday()]
    period = period_of(moment.hour)
    holiday = _HOLIDAYS.get((moment.month, moment.day), "")
    weekend = moment.weekday() >= 5
    extras = [item for item in (holiday, "周末" if weekend else "") if item]
    text = (
        f"{moment.year}年{moment.month}月{moment.day}日 {weekday} "
        f"{period} {moment.hour:02d}:{moment.minute:02d}"
    )
    if extras:
        text += f"（{'，'.join(extras)}）"
    return {
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M"),
        "weekday": weekday,
        "period": period,
        "holiday": holiday,
        "weekend": weekend,
        "text": text,
    }
