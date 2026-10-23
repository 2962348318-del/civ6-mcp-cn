"""任务 L —— 公民地块分配优化。

治的是:游戏自带的自动公民分配按【固定权重】排地块,不知道你锁了哪条胜利路线。
于是常见的局面是——一个公民站在 1粮1锤 的平原上,旁边 2粮2锤 的丘陵闲置;
或者你走科学胜利,自动分配却把公民全塞进金币格。

早期每回合只差 1-2 产出,200 回合累加下来等于少了一座城。

设计边界(诚实说明):
    · 这里【不含任何 gamedata 数值】——因为地块产出是局内实时值,
      不是规则表里的常量。产出必须由调用方从 Lua 查询(TileInfo.yields)传入。
    · yields 元组顺序 = (食物, 产能, 金币, 科技, 文化, 信仰),
      与 lua/models.py 的 TileInfo.yields 一致。
    · 只做【贪心最优】。文明6 的公民分配本身就是贪心的(每个公民独立选格,
      没有格子之间的耦合),所以贪心在这里就是精确最优,不是近似。
    · 专家(specialist)不参与比较——那是区域产出,不是地块产出。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 产出索引 —— 与 TileInfo.yields 对齐
FOOD, PROD, GOLD, SCIENCE, CULTURE, FAITH = range(6)

_YIELD_CN = ("粮", "锤", "金", "科", "文", "信")

# 胜利路线 → 产出权重。基准 1.0,路线相关的产出翻倍。
# 食物权重恒定为 1.0:任何路线都靠人口驱动,而人口本身就是产出乘数。
_VICTORY_WEIGHTS: dict[str, tuple[float, ...]] = {
    # (食物, 产能, 金币, 科技, 文化, 信仰)
    "SCIENCE": (1.0, 1.2, 1.0, 2.0, 1.0, 1.0),
    "CULTURE": (1.0, 1.0, 1.0, 1.0, 2.0, 1.0),
    "RELIGION": (1.0, 1.0, 1.0, 1.0, 1.0, 2.0),
    "DOMINATION": (1.0, 2.0, 1.0, 1.0, 1.0, 1.0),
}
_DEFAULT_WEIGHTS = (1.0, 1.0, 1.0, 1.0, 1.0, 1.0)

# 差距低于这个比例就不提醒 —— 每回合为 5% 的收益刷屏,反而会淹没重要信号。
MIN_GAIN_RATIO = 0.15


@dataclass(frozen=True)
class Tile:
    """一块可供公民工作的地块。产出由 Lua 实时查询提供。"""

    x: int
    y: int
    yields: tuple[int, ...]  # (食物, 产能, 金币, 科技, 文化, 信仰)
    worked: bool = False  # 当前是否有公民在工作
    locked: bool = False  # 玩家手动锁定,优化时不得挪走

    def score(self, weights: tuple[float, ...]) -> float:
        return sum(y * w for y, w in zip(self.yields, weights, strict=False))

    def brief(self) -> str:
        """把产出写成 "2粮2锤" 这样的短串,零产出的项不写。"""
        parts = [f"{v}{_YIELD_CN[i]}" for i, v in enumerate(self.yields[:6]) if v]
        return "".join(parts) or "无产出"


@dataclass
class CityCitizens:
    """一座城的公民分配现状。"""

    name: str
    tiles: list[Tile] = field(default_factory=list)  # 城市可工作范围内的全部地块


def _weights(victory_type: str | None) -> tuple[float, ...]:
    return _VICTORY_WEIGHTS.get((victory_type or "").upper(), _DEFAULT_WEIGHTS)


def optimize_city(
    city: CityCitizens, victory_type: str | None = None
) -> tuple[float, float, list[tuple[Tile, Tile]]]:
    """算出这座城的当前加权产出、最优加权产出、以及建议的换格对。

    返回 (当前分, 最优分, [(挪出的格, 挪入的格), ...])。
    锁定的格子和市中心格(如果调用方没传进来)不参与调整。
    """
    weights = _weights(victory_type)
    movable = [t for t in city.tiles if not t.locked]
    fixed_score = sum(t.score(weights) for t in city.tiles if t.locked and t.worked)

    worked = [t for t in movable if t.worked]
    idle = [t for t in movable if not t.worked]
    n = len(worked)
    if n == 0:
        return fixed_score, fixed_score, []

    current = sum(t.score(weights) for t in worked) + fixed_score

    # 贪心 = 精确最优:每个公民独立选格,取分数最高的 n 块。
    ranked = sorted(movable, key=lambda t: t.score(weights), reverse=True)
    best_set = ranked[:n]
    best = sum(t.score(weights) for t in best_set) + fixed_score

    # 配对:分数最低的在工作格 ↔ 分数最高的闲置格
    drop = sorted(
        (t for t in worked if t not in best_set), key=lambda t: t.score(weights)
    )
    take = sorted(
        (t for t in best_set if t in idle), key=lambda t: t.score(weights), reverse=True
    )
    swaps = list(zip(drop, take, strict=False))
    return current, best, swaps


def get_citizen_advice(
    cities: list[CityCitizens],
    victory_type: str | None = None,
    max_cities: int = 3,
) -> list[str]:
    """每回合的公民分配优化建议。无可观测收益时返回空列表。"""
    if not cities:
        return []

    found: list[tuple[float, str]] = []
    for city in cities:
        try:
            current, best, swaps = optimize_city(city, victory_type)
        except Exception:
            continue
        if not swaps or best <= current:
            continue
        gain = best - current
        ratio = gain / current if current > 0 else 1.0
        if ratio < MIN_GAIN_RATIO:
            continue

        head = (
            f"💡 {city.name} 公民分配可优化:加权产出 {current:.0f} → {best:.0f}"
            f"(+{ratio * 100:.0f}%)"
        )
        detail = [
            f"   地块({t_in.x},{t_in.y}) {t_in.brief()} 替换 "
            f"({t_out.x},{t_out.y}) {t_out.brief()}"
            for t_out, t_in in swaps[:2]
        ]
        if len(swaps) > 2:
            detail.append(f"   (另有 {len(swaps) - 2} 处可调整)")
        found.append((ratio, "\n".join([head, *detail])))

    if not found:
        return []

    found.sort(key=lambda p: p[0], reverse=True)
    out = [text for _, text in found[:max_cities]]
    if len(found) > max_cities:
        out.append(f"   (还有 {len(found) - max_cities} 座城可优化,已省略)")
    if victory_type:
        out.append(
            f"   权重按 {victory_type.upper()} 路线加权 —— "
            f"换格用 set_city_focus,或在城市面板手动指定地块。"
        )
    return out
