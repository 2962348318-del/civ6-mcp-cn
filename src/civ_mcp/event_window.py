"""任务 F —— 关键时间窗口提醒。

有些机会只在很窄的几个回合里存在,错过就再也没有:
    · 科技完成的那一刻,一批单位突然可以升级 —— 但你得先有战略资源
    · 第一次换政体免费 —— 之后每次都要花钱
    · 黄金/英雄时代快结束 —— 时代得分不够就掉进黑暗时代
    · 对手抢先建成了你路线上的关键奇观 —— 你还在造的那个已经废了

这些全部是确定性判断,不需要任何"AI 判断"。麻烦之处只在于:
没有人在正确的那一回合把它说出来。这个模块就干这一件事。

数据来源(全部 docs/gamedata/ 导出,不含记忆值):
    Units.json         — PrereqTech / StrategicResource / Combat
    UnitUpgrades.json  — 升级链(59 条)
    Technologies.json  — Cost / EraType
    Governments.json   — PrereqCivic
    Resources.json     — 战略资源的 PrereqTech(决定什么时候能"看见"它)
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass, field
from pathlib import Path

_GAMEDATA = gamedata_path()

# 提醒窗口:科技还剩几回合时开始提醒
TECH_WINDOW = 5
# 时代还剩几回合时开始提醒
ERA_WINDOW = 10


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_units() -> dict[str, dict[str, str]]:
    return {r["UnitType"]: r for r in _rows("Units") if r.get("UnitType")}


def _load_upgrades() -> dict[str, str]:
    return {
        r["Unit"]: r["UpgradeUnit"]
        for r in _rows("UnitUpgrades")
        if r.get("Unit") and r.get("UpgradeUnit")
    }


def _load_techs() -> dict[str, dict[str, str]]:
    return {
        r["TechnologyType"]: r for r in _rows("Technologies") if r.get("TechnologyType")
    }


def _load_governments() -> dict[str, dict[str, str]]:
    return {
        r["GovernmentType"]: r for r in _rows("Governments") if r.get("GovernmentType")
    }


def _load_strategic_reveals() -> dict[str, tuple[str, str]]:
    """TECH_XXX -> (RESOURCE_XXX, 中文名);只含真的需要科技才能看见的战略资源。"""
    out: dict[str, tuple[str, str]] = {}
    for row in _rows("Resources"):
        if row.get("ResourceClassType") != "RESOURCECLASS_STRATEGIC":
            continue
        tech = row.get("PrereqTech") or ""
        if tech:
            out[tech] = (row.get("ResourceType", ""), row.get("Name", ""))
    return out


UNITS = _load_units()
UPGRADES = _load_upgrades()
TECHS = _load_techs()
GOVERNMENTS = _load_governments()
STRATEGIC_REVEALS = _load_strategic_reveals()


def unit_cn(unit_type: str) -> str:
    row = UNITS.get(unit_type)
    return (row or {}).get("Name") or unit_type


def tech_type_from_name(localized: str) -> str:
    """本地化科技名 → TECH_*(2026-08-05 接线:overview/tech_civics 的
    current_research 是本地化名,而本模块的窗口判断吃类型名)。
    传入的已是 TECH_* 则原样返回;查不到返回空串(窗口静默,不硬凑)。"""
    s = (localized or "").strip()
    if not s:
        return ""
    if s.upper().startswith("TECH_"):
        return s.upper()
    for t, row in TECHS.items():
        if (row.get("Name") or "").strip() == s:
            return t
    return ""


def resource_type_from_name(name: str) -> str:
    """本地化资源名 → RESOURCE_*。已是类型名原样返回;查不到返回原串。"""
    s = (name or "").strip()
    if not s or s.upper().startswith("RESOURCE_"):
        return s.upper() if s else s
    for r in _rows("Resources"):
        if (r.get("Name") or "").strip() == s:
            return r.get("ResourceType") or s
    return s


def tech_cn(tech_type: str) -> str:
    row = TECHS.get(tech_type)
    return (row or {}).get("Name") or tech_type


def government_cn(gov_type: str) -> str:
    row = GOVERNMENTS.get(gov_type)
    return (row or {}).get("Name") or gov_type


def upgrades_unlocked_by(tech: str) -> list[tuple[str, str, str]]:
    """这个科技一完成,哪些【已有单位】就能升级?

    返回 [(旧单位, 新单位, 新单位需要的战略资源或空串), ...]。
    判据全部来自 UnitUpgrades + Units.PrereqTech —— 不含任何记忆。
    """
    out = []
    for old, new in UPGRADES.items():
        row = UNITS.get(new)
        if not row or row.get("PrereqTech") != tech:
            continue
        out.append((old, new, row.get("StrategicResource") or ""))
    return sorted(out)


@dataclass
class EventContext:
    """时间窗口判断所需的事实,全部由实时查询提供。"""

    turn: int = 0
    # 科技 / 市政
    researching_tech: str = ""
    tech_turns_left: int = 99
    researching_civic: str = ""
    civic_turns_left: int = 99
    # 我方
    my_unit_types: tuple[str, ...] = ()  # 现有单位的 UnitType
    my_strategic_stock: dict[str, int] = field(default_factory=dict)
    current_government: str = ""
    unlocked_governments: tuple[str, ...] = ()
    has_changed_government: bool = False
    # 时代
    era_turns_left: int = 99
    era_score: int = 0
    era_score_needed_for_golden: int = 0
    is_golden_age: bool = False
    # 对手
    victory_type: str = ""
    # [(文明名, 奇观中文名, 该奇观是否在你的路线上), ...]
    rival_wonders_completed: tuple[tuple[str, str, bool], ...] = ()
    rival_same_victory: tuple[str, ...] = ()


def _check_upgrade_window(ctx: EventContext) -> list[str]:
    if not ctx.researching_tech or ctx.tech_turns_left > TECH_WINDOW:
        return []
    unlocks = upgrades_unlocked_by(ctx.researching_tech)
    if not unlocks:
        return []
    owned = set(ctx.my_unit_types)
    out = []
    for old, new, resource in unlocks:
        count = sum(1 for u in ctx.my_unit_types if u == old)
        if old not in owned:
            continue
        line = (
            f"⏳ {tech_cn(ctx.researching_tech)}还有 {ctx.tech_turns_left} 回合完成,"
            f"届时 {count} 个{unit_cn(old)}可升级为{unit_cn(new)}"
        )
        if resource:
            stock = ctx.my_strategic_stock.get(resource, 0)
            if stock > 0:
                line += f"(需要{resource},库存 {stock},够用)"
            else:
                line += (
                    f"。⚠️ {unit_cn(new)}需要战略资源 {resource},你库存为 0 —— "
                    f"现在就用 get_empire_resources 找矿,或者准备交易换取,"
                    f"否则科技到手也升不了级。"
                )
        out.append(line)
    return out


def _check_strategic_reveal(ctx: EventContext) -> list[str]:
    if not ctx.researching_tech or ctx.tech_turns_left > TECH_WINDOW:
        return []
    entry = STRATEGIC_REVEALS.get(ctx.researching_tech)
    if not entry:
        return []
    resource, name = entry
    return [
        f"⏳ {tech_cn(ctx.researching_tech)}还有 {ctx.tech_turns_left} 回合完成 —— "
        f"完成后地图上会显现{name}({resource})。完成当回合就扫一遍边境,"
        f"没被别人占的矿点值得抢先铺城或买地。"
    ]


def _check_government_window(ctx: EventContext) -> list[str]:
    fresh = [
        g
        for g in ctx.unlocked_governments
        if g in GOVERNMENTS and g != ctx.current_government
    ]
    if not fresh:
        return []
    names = "、".join(government_cn(g) for g in fresh[:3])
    line = f"⏳ 已解锁但未采用的政体:{names}"
    if not ctx.has_changed_government:
        line += " —— 第一次换政体【免费】,之后每次都要花钱。现在比较槽位差异最划算。"
    else:
        line += " —— 换政体要花钱,先比较槽位配置再决定。"
    return [line]


def _check_era_window(ctx: EventContext) -> list[str]:
    if ctx.era_turns_left > ERA_WINDOW:
        return []
    if ctx.era_score_needed_for_golden <= 0:
        return []
    gap = ctx.era_score_needed_for_golden - ctx.era_score
    if gap > 0:
        return [
            f"⏳ 本时代还剩 {ctx.era_turns_left} 回合,时代得分 {ctx.era_score}/"
            f"{ctx.era_score_needed_for_golden},还差 {gap} 分进黄金时代。"
            f"优先做能加时代得分的事(首次建成奇观、拆营地、首次进入新科技等),"
            f"分不够会掉进黑暗时代。"
        ]
    return [
        f"⏳ 本时代还剩 {ctx.era_turns_left} 回合,时代得分 {ctx.era_score}/"
        f"{ctx.era_score_needed_for_golden} 已达标 —— 提前想好黄金时代要选哪个专注。"
    ]


def _check_rival_wonders(ctx: EventContext) -> list[str]:
    out = []
    for civ, wonder, on_my_path in ctx.rival_wonders_completed:
        if on_my_path:
            out.append(
                f"⚠️ {civ}建成了{wonder} —— 这是你路线上的奇观,已经没了。"
                f"如果你正在造它,立刻换生产项(产能会退成金币,但回合是白扔的)。"
            )
        elif civ in ctx.rival_same_victory:
            out.append(
                f"⏳ {civ}建成了{wonder},而他和你走同一条"
                f"{ctx.victory_type or '胜利'}路线 —— 他在加速,盯住他的进度。"
            )
    return out


def get_event_window_advice(ctx: EventContext, max_items: int = 4) -> list[str]:
    """关键时间窗口提醒。没有处在任何窗口里时返回空列表。"""
    checks = (
        _check_rival_wonders(ctx),
        _check_upgrade_window(ctx),
        _check_government_window(ctx),
        _check_era_window(ctx),
        _check_strategic_reveal(ctx),
    )
    out: list[str] = []
    for group in checks:
        out.extend(group)
    if not out:
        return []
    if len(out) > max_items:
        omitted = len(out) - max_items
        out = out[:max_items]
        out.append(f"   (另有 {omitted} 条时间窗口提醒已省略)")
    return ["== 时间窗口 ==", *out]
