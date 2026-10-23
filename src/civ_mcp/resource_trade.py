"""任务 E —— 奢侈资源自动交易建议。

规则(全部从 docs/gamedata/Resources.json 导出,不是记忆):
    · 每种奢侈资源的 Happiness 列 = 它能供给的城市数(绝大多数是 4,
      香水/肉桂/丁香是 6)。一种奢侈只对【还没有这种奢侈】的城市生效。
    · ⇒ 同一种奢侈拿第 2 份对宜居度【完全没有增益】。多出来的份额只有一个用途:
      卖掉。这是全局最稳的白捡收益,而 AI 从来不做。
    · 反过来:缺一种没有的奢侈 = 4(或 6)座城各差 1 点宜居度。

所以每回合的判断只有三句话:
    1. 我有哪些重复的奢侈?→ 卖
    2. 对面有哪些我完全没有的奢侈?→ 买
    3. 别把资源卖给下回合要宣我的人(卖了也是白卖,宣战即毁约)

⚠️ 成交价格不做预测。AI 的估价受关系、议程、金库影响,唯一可靠的做法是
用 `propose_trade(mode="test")` 实测。本模块只负责【指出该谈哪一笔】。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_GAMEDATA = gamedata_path()

LUXURY_CLASS = "RESOURCECLASS_LUXURY"


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_luxuries() -> dict[str, tuple[str, int]]:
    """RESOURCE_SILK -> ("丝绸", 4)。第二项 = 该奢侈能供给的城市数。"""
    out: dict[str, tuple[str, int]] = {}
    for row in _rows("Resources"):
        if row.get("ResourceClassType") != LUXURY_CLASS:
            continue
        rtype = row.get("ResourceType", "")
        if not rtype:
            continue
        try:
            cities = int(row.get("Happiness") or 0)
        except (TypeError, ValueError):
            cities = 0
        out[rtype] = (row.get("Name") or rtype, cities)
    return out


LUXURIES: dict[str, tuple[str, int]] = _load_luxuries()

# 名字 → 标识,让调用方可以传中文名或裸名("丝绸" / "Silk" / "RESOURCE_SILK")
# "柑橘 x3" / "Citrus x2" 尾部的份数标记。大小写都收,中间空格可有可无。
_COUNT_SUFFIX = re.compile(r"\s*[xX×]\s*\d+\s*$")

_BY_NAME: dict[str, str] = {}
for _rtype, (_cn, _n) in LUXURIES.items():
    _BY_NAME[_cn] = _rtype
    _BY_NAME[_rtype] = _rtype
    _BY_NAME[_rtype.removeprefix("RESOURCE_").replace("_", " ").title()] = _rtype


def normalize(name: str) -> str | None:
    """把任意写法的资源名归一到 RESOURCE_XXX。不是奢侈资源返回 None。

    份数后缀必须先剥掉。上游 get_trade_options 对持有 2 份以上的资源发的
    是 "柑橘 x3" 这种标签(1 份时才是裸名),归一失败就退回原字符串当键,
    于是【持有越多越匹配不上】。

    2026-08-11 T286 实锤:同一份 get_trade_options 输出里,上半区列着
    "我方可交易:柑橘 x3",下半区的建议却说"你完全没有柑橘,值得开价买"。
    同时西班牙的棉花 x2、熏香 x2 —— 我真正缺的两种 —— 因为对方也带后缀,
    一样归一失败,从"该买"名单里整个漏掉。一个后缀同时造成误报和漏报,
    而且刚好把结论指反:该卖的被说成该买,该买的一个字没提。
    """
    if not name:
        return None
    key = _COUNT_SUFFIX.sub("", name.strip()).strip()
    return _BY_NAME.get(key) or _BY_NAME.get(key.title()) or _BY_NAME.get(key.upper())


def parse_count(name: str) -> int:
    """从 "柑橘 x3" 这种标签里读出份数;没有后缀就是 1 份。

    份数不是装饰:第 2 份及以后对宜居度【零贡献】,只能卖。上游一直把
    这个数字印在标签里,而消费方一直只当它是名字的一部分 —— 于是
    "卖富余"这条建议(本模块的头号收益项)从来没有过数据,永远静默。
    """
    m = _COUNT_SUFFIX.search(name or "")
    if not m:
        return 1
    digits = re.sub(r"\D", "", m.group(0))
    return int(digits) if digits else 1


def luxury_cn(resource: str) -> str:
    entry = LUXURIES.get(resource)
    return entry[0] if entry else resource


def cities_served(resource: str) -> int:
    entry = LUXURIES.get(resource)
    return entry[1] if entry else 0


@dataclass(frozen=True)
class RivalHoldings:
    """一个已认识文明的奢侈资源持有情况(来自 get_trade_options)。"""

    player_id: int
    civ_name: str
    luxuries: tuple[str, ...] = ()  # 他方可交易的奢侈
    war_risk: str = "低"  # "高" / "中" / "低",来自 opponents.py
    is_at_war: bool = False


@dataclass
class ResourceTradeContext:
    """交易判断所需的事实。"""

    my_luxuries: dict[str, int] = field(default_factory=dict)  # 资源 -> 持有份数
    rivals: list[RivalHoldings] = field(default_factory=list)
    city_count: int = 0
    amenity_shortfall: int = 0  # 全帝国缺多少宜居度(0 = 不缺)


def surplus_luxuries(ctx: ResourceTradeContext) -> list[tuple[str, int]]:
    """我手上重复的奢侈 —— 第 2 份起对宜居度零贡献,纯粹是可卖的库存。"""
    out = []
    for res, count in ctx.my_luxuries.items():
        norm = normalize(res) or res
        if count > 1:
            out.append((norm, count - 1))
    out.sort(key=lambda p: (-p[1], p[0]))
    return out


def missing_luxuries(ctx: ResourceTradeContext) -> dict[str, list[str]]:
    """对面有、我一份都没有的奢侈 → {资源: [持有它的文明名]}。"""
    mine = {normalize(r) or r for r, n in ctx.my_luxuries.items() if n > 0}
    out: dict[str, list[str]] = {}
    for rival in ctx.rivals:
        if rival.is_at_war:
            continue
        for res in rival.luxuries:
            norm = normalize(res)
            if not norm or norm in mine:
                continue
            out.setdefault(norm, []).append(rival.civ_name)
    return out


def get_resource_trade_advice(
    ctx: ResourceTradeContext, max_items: int = 4
) -> list[str]:
    """奢侈资源交易建议。没有可谈的交易就返回空列表。"""
    if not LUXURIES:
        return []

    surplus = surplus_luxuries(ctx)
    missing = missing_luxuries(ctx)
    if not surplus and not missing:
        return []

    out = ["== 资源交易 =="]
    risky = {r.civ_name for r in ctx.rivals if r.war_risk == "高"}

    for res, spare in surplus[:max_items]:
        buyers = [
            r.civ_name
            for r in ctx.rivals
            if not r.is_at_war and res not in {normalize(x) for x in r.luxuries}
        ]
        line = (
            f"· 你有 {spare + 1} 份{luxury_cn(res)},多出的 {spare} 份对宜居度"
            f"【零贡献】—— 重复的奢侈只有卖掉这一个用途。"
        )
        if buyers:
            safe = [b for b in buyers if b not in risky]
            targets = safe or buyers
            line += f" 缺它的:{('、'.join(targets[:3]))}。"
            if not safe:
                line += " ⚠️ 这些都是高宣战风险对象,卖了可能连本带利收不回。"
        out.append(line)

    remaining = max_items - len(surplus[:max_items])
    if remaining > 0 and missing:
        for res, holders in sorted(missing.items())[:remaining]:
            n = cities_served(res)
            served = f"{n} 座城" if n else "若干城"
            out.append(
                f"· 你完全没有{luxury_cn(res)}(可供 {served} 各 +1 宜居度),"
                f"{('、'.join(holders[:3]))} 有 —— 值得开价买。"
            )

    if ctx.amenity_shortfall > 0:
        out.append(
            f"   当前全帝国缺 {ctx.amenity_shortfall} 点宜居度 —— "
            f"买一种新奢侈是最快的补法,比建娱乐中心快得多。"
        )
    out.append(
        "   价格不可预测(受关系/议程/金库影响),先用 "
        'propose_trade(mode="test") 实测 AI 的接受线,再正式提。'
    )
    return out
