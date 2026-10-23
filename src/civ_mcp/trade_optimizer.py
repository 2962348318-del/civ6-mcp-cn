"""任务 T —— 贸易路线精确优化。

现状:`get_trade_destinations` 把 20+ 个目的地平铺给模型,模型自己挑。
结果是随便挑一个,或者按"名字眼熟"挑。一条贸易路线管 20-30 回合,
挑错一次的损失是几百产出。

这里做的是确定性排序:把每个目的地的产出按【锁定的胜利路线】加权算成一个数,
排序,只给前 3 个。附带标注那些产出栏里看不到的额外价值:
    · 城邦任务(has_quest)—— 完成后送 1 使者,使者是宗主权的硬通货
    · 贸易站(has_trading_post)—— 已建立的贸易站给额外产出
    · 国内路线 —— 顺便修路,战时就是补给线;粮锤直接喂给目的地城市
    · 宗教压力 —— 走宗教路线时,国际路线是免费的传教管道

数据来源:全部来自 Lua 实时查询(TradeDestination),不涉及 gamedata 常量。
产出串格式 "Food:3 Prod:2 Gold:4" 由 lua/economy.py 的 _parse_compact_yields
生成,本模块只做解析,不重新定义格式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 产出串里的键 → 内部产出名
_YIELD_KEYS = {
    "Food": "food",
    "Prod": "prod",
    "Gold": "gold",
    "Sci": "science",
    "Cul": "culture",
    "Faith": "faith",
}

_YIELD_CN = {
    "food": "粮",
    "prod": "锤",
    "gold": "金",
    "science": "科",
    "culture": "文",
    "faith": "信",
}

# 胜利路线 → 产出权重。基准 1.0。
_VICTORY_WEIGHTS: dict[str, dict[str, float]] = {
    "SCIENCE": {
        "science": 2.5,
        "prod": 1.5,
        "food": 1.0,
        "gold": 1.0,
        "culture": 1.0,
        "faith": 1.0,
    },
    "CULTURE": {
        "culture": 2.5,
        "gold": 1.2,
        "food": 1.0,
        "prod": 1.0,
        "science": 1.0,
        "faith": 1.0,
    },
    "RELIGION": {
        "faith": 2.5,
        "food": 1.2,
        "prod": 1.0,
        "gold": 1.0,
        "science": 1.0,
        "culture": 1.0,
    },
    "DOMINATION": {
        "prod": 2.5,
        "gold": 1.2,
        "food": 1.0,
        "science": 1.0,
        "culture": 1.0,
        "faith": 1.0,
    },
}
_DEFAULT_WEIGHT = 1.0

# 额外价值的折算分。这些不是产出,但确实值钱,所以折成同一量纲参与排序。
# [经验] 这三个系数没有游戏库依据,是为了让"看不见的价值"能参与排序而设的
# 折算分,不是游戏内数值。改动它们只影响排序,不影响任何对局判断的正确性。
BONUS_QUEST = 3.0  # 城邦任务:完成送 1 使者
BONUS_TRADING_POST = 1.0  # 已有贸易站
BONUS_DOMESTIC_ROAD = 1.5  # 国内路线顺带修路


@dataclass(frozen=True)
class Destination:
    """一个可选的贸易目的地。字段与 lua/models.py 的 TradeDestination 对齐。"""

    city_name: str
    owner_name: str = ""
    x: int = 0
    y: int = 0
    is_domestic: bool = False
    is_city_state: bool = False
    has_quest: bool = False
    has_trading_post: bool = False
    origin_yields: str = ""  # 起点城市每回合收到的产出
    dest_yields: str = ""  # 目的地收到的(国内路线才有意义)
    pressure_out: float = 0.0
    religion_out: str = ""


@dataclass
class TradeContext:
    """一次贸易决策所需的事实。"""

    idle_traders: int = 0  # 空闲(未派出)的商人数量
    origin_city: str = ""  # 商人所在城市
    destinations: list[Destination] | None = None
    victory_type: str = ""
    hostile_civs: tuple[str, ...] = ()  # 已知敌对/高宣战风险的文明名

    def __post_init__(self):
        if self.destinations is None:
            self.destinations = []


def parse_yields(text: str) -> dict[str, float]:
    """解析 "Food:3 Prod:2 Gold:4" → {"food": 3.0, "prod": 2.0, "gold": 4.0}。

    无法识别的键忽略;格式异常返回空字典(不抛异常)。
    """
    out: dict[str, float] = {}
    if not text:
        return out
    for key, value in re.findall(r"([A-Za-z]+)\s*:\s*([0-9.]+)", text):
        name = _YIELD_KEYS.get(key)
        if not name:
            continue
        try:
            out[name] = out.get(name, 0.0) + float(value)
        except ValueError:
            continue
    return out


def _weight(victory_type: str, yield_name: str) -> float:
    table = _VICTORY_WEIGHTS.get((victory_type or "").upper())
    if not table:
        return _DEFAULT_WEIGHT
    return table.get(yield_name, _DEFAULT_WEIGHT)


def score_destination(dest: Destination, ctx: TradeContext) -> tuple[float, list[str]]:
    """给一个目的地打分,并返回打分依据(用于简报里说人话)。"""
    reasons: list[str] = []
    yields = parse_yields(dest.origin_yields)
    score = sum(
        amount * _weight(ctx.victory_type, name) for name, amount in yields.items()
    )
    if yields:
        reasons.append(
            "".join(
                f"+{amount:g}{_YIELD_CN.get(name, name)}"
                for name, amount in sorted(yields.items())
                if amount
            )
        )

    if dest.is_domestic:
        score += BONUS_DOMESTIC_ROAD
        dest_y = parse_yields(dest.dest_yields)
        if dest_y:
            reasons.append(
                "目的地另得 "
                + "".join(
                    f"+{a:g}{_YIELD_CN.get(n, n)}"
                    for n, a in sorted(dest_y.items())
                    if a
                )
            )
        reasons.append("国内路线顺带修路(战时即补给线)")
    if dest.has_quest:
        score += BONUS_QUEST
        reasons.append("完成城邦任务 → 送 1 使者")
    if dest.has_trading_post:
        score += BONUS_TRADING_POST
        reasons.append("已有贸易站")
    if (
        (ctx.victory_type or "").upper() == "RELIGION"
        and dest.pressure_out > 0
        and not dest.is_domestic
    ):
        reasons.append(f"顺带输出 {dest.pressure_out:g} 点{dest.religion_out}宗教压力")

    if dest.owner_name and dest.owner_name in ctx.hostile_civs:
        reasons.append(f"⚠️ {dest.owner_name} 是潜在敌人 —— 宣战即断路,商人可能被俘")
        score *= 0.5

    return score, reasons


def get_trade_advice(ctx: TradeContext, top_n: int = 3) -> list[str]:
    """贸易路线建议。没有空闲商人或没有目的地时返回空列表。"""
    if ctx.idle_traders <= 0 or not ctx.destinations:
        return []

    ranked = sorted(
        ((score_destination(d, ctx), d) for d in ctx.destinations),
        key=lambda pair: pair[0][0],
        reverse=True,
    )
    if not ranked or ranked[0][0][0] <= 0:
        return []

    origin = ctx.origin_city or "起点城市"
    head = f"== 贸易路线建议({ctx.idle_traders} 个商人空闲) =="
    out = [head]
    for (score, reasons), dest in ranked[:top_n]:
        kind = (
            "国内" if dest.is_domestic else ("城邦" if dest.is_city_state else "国际")
        )
        owner = (
            f"/{dest.owner_name}" if dest.owner_name and not dest.is_domestic else ""
        )
        out.append(
            f"· {origin} → {dest.city_name}{owner}({kind},{score:.1f} 分):"
            + " ‖ ".join(reasons)
        )
    if ctx.victory_type:
        out.append(
            f"   评分按 {ctx.victory_type.upper()} 路线加权。"
            f"路线一旦派出锁定 20-30 回合,选错的代价是几百产出。"
        )
    return out
