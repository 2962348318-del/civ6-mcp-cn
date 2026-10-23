"""任务 U —— 砍树溢出(Chop Overflow)。

机制:建造者移除森林/雨林/沼泽,或收获资源,会把一笔【一次性产能】直接打进
城市当前的生产项。如果这笔产能超过剩余需求,多出来的部分溢出到下一个项目。
人类高手用它在古典时代一回合秒奇观;AI 通常把建造者次数花在修农场上。

⚠️ 三条必须说清楚的边界,否则这个模块会输出编造的数字:

1. **砍树产能不在 gamedata 里**。实测 `docs/gamedata/Features.json` 只有
   MovementChange / DefenseModifier / Impassable / NaturalWonder 四列,没有任何
   移除产出字段。而且砍树收益随游戏进度(已研究科技+市政数)增长,本来就不是常量。
   ⇒ 本模块【要求调用方传入实测的砍树产能】(`chop_yield`)。没传就只讲机制、不报数字。

2. **马格努斯(Magnus)是 Rise & Fall 的总督**。本机规则集是 RULESET_STANDARD,
   `_all.json` 里根本没有 Governors 表。⇒ 加成百分比由调用方从 `get_governors`
   的实时返回里读出来传进来,本模块不假设它存在。

3. **溢出的具体上限规则不做承诺**。只报"砍完还剩多少锤没用掉",
   不宣称那部分一定 100% 结转 —— 写错的数字比不写更糟。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass, field
from pathlib import Path

_GAMEDATA = gamedata_path()


def _rows(table: str) -> list[dict[str, str]]:
    """读一张导出的规则表。任何失败都返回空列表 —— 本模块不抛异常。"""
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _known_features() -> dict[str, str]:
    return {
        r.get("FeatureType", ""): r.get("Name", "")
        for r in _rows("Features")
        if r.get("FeatureType")
    }


# 可移除并产生产能的地貌。名字对 Features.json 校验过 —— 表里没有的一律不认。
# ⚠️ "移除后给产能"这一条本身【不是】从游戏库导出的(表里没有产出列),
# 属于机制常识;所以下面只用它来筛选候选格,不用它推导任何数值。
_CHOPPABLE_CANDIDATES = ("FEATURE_FOREST", "FEATURE_JUNGLE", "FEATURE_MARSH")

CHOPPABLE_FEATURES: dict[str, str] = {
    ft: name for ft, name in _known_features().items() if ft in _CHOPPABLE_CANDIDATES
}

# 值得为它砍树的生产项。砍树只有一次机会(建造者次数有限),
# 花在 60 锤的纪念碑上是浪费。
HIGH_VALUE_PREFIXES = ("BUILDING_", "PROJECT_", "UNIT_SETTLER", "DISTRICT_")


@dataclass(frozen=True)
class ChopSite:
    """一个建造者当前站着、且可以砍的格子。"""

    x: int
    y: int
    feature: str  # FEATURE_FOREST / FEATURE_JUNGLE / ...
    builder_id: int = 0
    builder_charges: int = 0  # 剩余建造次数
    chop_yield: int | None = None  # 实测砍树基础产能。None = 未知,只讲机制不报数

    @property
    def feature_cn(self) -> str:
        return CHOPPABLE_FEATURES.get(self.feature, self.feature)

    @property
    def is_choppable(self) -> bool:
        return self.feature in CHOPPABLE_FEATURES


@dataclass
class ChopContext:
    """砍树决策所需的事实,全部来自实时查询。"""

    city_name: str = ""
    producing: str = ""  # 当前生产项,如 "BUILDING_PYRAMIDS"
    producing_cn: str = ""  # 中文名(有就用,没有就用 producing)
    production_remaining: int = 0  # 还差多少锤完成
    production_per_turn: int = 0  # 每回合产能
    sites: list[ChopSite] = field(default_factory=list)
    # 加成:百分比。名称由调用方给,数值也由调用方给 —— 本模块不假设任何一条存在。
    # 例:[("马格努斯·地面清理", 50), ("政策卡 XXX", 30)]
    bonuses: list[tuple[str, int]] = field(default_factory=list)


def effective_chop(base_yield: int, bonuses: list[tuple[str, int]]) -> int:
    """基础砍树产能 + 全部加成 → 实际到手产能(向下取整)。"""
    total_pct = sum(pct for _, pct in bonuses)
    return int(base_yield * (100 + total_pct) / 100)


def _is_high_value(producing: str) -> bool:
    up = (producing or "").upper()
    return any(up.startswith(p) for p in HIGH_VALUE_PREFIXES)


def get_chop_advice(ctx: ChopContext, max_sites: int = 2) -> list[str]:
    """砍树溢出机会提醒。没有可砍的格子就返回空列表。"""
    sites = [s for s in ctx.sites if s.is_choppable and s.builder_charges > 0]
    if not sites:
        return []
    if not ctx.producing or not _is_high_value(ctx.producing):
        # 当前生产的是廉价项目,砍树等于浪费一次建造者次数,不提醒。
        return []

    item = ctx.producing_cn or ctx.producing
    out = ["💡 砍树机会(Chop Overflow):"]

    bonus_note = ""
    if ctx.bonuses:
        detail = "、".join(f"{name} +{pct}%" for name, pct in ctx.bonuses)
        bonus_note = f"(已计入 {detail})"

    unknown_yield = False
    for site in sites[:max_sites]:
        loc = f"({site.x},{site.y}) 的{site.feature_cn}"
        if site.chop_yield is None:
            unknown_yield = True
            out.append(
                f"· 建造者在 {loc},可移除 —— 砍树产能随已研究科技+市政数增长,"
                f"游戏库里没有这张表,请在城市面板确认实际数值再决定。"
            )
            continue
        gained = effective_chop(site.chop_yield, ctx.bonuses)
        line = f"· 建造者在 {loc}:砍得约 {gained} 锤{bonus_note}"
        if ctx.production_remaining > 0:
            if gained >= ctx.production_remaining:
                spare = gained - ctx.production_remaining
                saved = (
                    ctx.production_remaining / ctx.production_per_turn
                    if ctx.production_per_turn > 0
                    else 0
                )
                line += (
                    f" → 直接完成「{item}」(还差 {ctx.production_remaining} 锤)"
                    f",余 {spare} 锤"
                )
                if saved >= 1:
                    line += f",省下约 {saved:.0f} 回合"
            else:
                turns = (
                    gained / ctx.production_per_turn
                    if ctx.production_per_turn > 0
                    else 0
                )
                pct = gained / ctx.production_remaining * 100
                line += f" → 「{item}」进度 +{pct:.0f}%"
                if turns >= 1:
                    line += f",约等于 {turns:.0f} 回合产能"
        out.append(line)

    if len(sites) > max_sites:
        out.append(f"   (另有 {len(sites) - max_sites} 个可砍的格子)")
    if unknown_yield:
        out.append("   ⚠️ 标注为未知的格子:砍树产能不是游戏库常量,本工具不编造数字。")
    out.append(
        "   注意:建造者次数有限。砍树只值得用在奇观 / 开拓者 / 区域 / 太空项目上。"
    )
    return out
