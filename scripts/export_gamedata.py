"""从运行中的游戏导出全部规则数据。

为什么从游戏导而不是查攻略:
  1. 自动匹配本机实际加载的规则集(原版 / 迭起兴衰 / 风云变幻 / MOD),
     不会出现"按资料片数值写的规则在原版上不成立"
  2. 数值零错误,不依赖模型记忆或攻略网站
  3. 一次导出永久可用,不占运行时查询预算

用法(游戏必须开着并进入存档):
    .venv\\Scripts\\python.exe scripts\\export_gamedata.py

输出:docs/gamedata/*.json 和 *.md
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from civ_mcp.connection import GameConnection  # noqa: E402

OUT = ROOT / "docs" / "gamedata"


def _lua(table: str, cols: list[str], localize: str | None = "Name") -> str:
    """生成遍历 GameInfo 表的 Lua。缺列返回空串,不会炸。"""
    parts = []
    for c in cols:
        if c == localize:
            # Name 是 LOC_ 键,查本地化拿到可读名
            parts.append(
                f'(function() local v = row.{c}; '
                f'if v == nil then return "" end; '
                f'local ok, s = pcall(Locale.Lookup, v); '
                f'return ok and tostring(s) or tostring(v) end)()'
            )
        else:
            parts.append(f'(function() local v = row.{c}; '
                         f'if v == nil then return "" end; return tostring(v) end)()')
    expr = ' .. "\\t" .. '.join(parts)
    return (
        f'local n = 0 '
        f'local ok = pcall(function() '
        f'for row in GameInfo.{table}() do '
        f'print({expr}) '
        f'n = n + 1 '
        f'end end) '
        f'if not ok then print("__TABLE_MISSING__") end '
        f'print("__COUNT__\\t" .. tostring(n)) '
        f'print("---END---")'
    )


# 表名 -> (列清单, 说明)
TABLES: dict[str, tuple[list[str], str]] = {
    "Units": (
        ["UnitType", "Name", "Cost", "Combat", "RangedCombat", "Bombard", "Range",
         "BaseMoves", "BaseSightRange", "PrereqTech", "PrereqCivic", "PromotionClass",
         "FormationClass", "Domain", "StrategicResource", "Maintenance",
         "MustSetUpToRangeAttack", "CanCapture", "ZoneOfControl"],
        "全时代单位数据 — 战力/射程/移动力/前置/兵种类别",
    ),
    "UnitUpgrades": (["Unit", "UpgradeUnit"], "单位升级链"),
    "UnitReplaces": (["CivUniqueUnitType", "ReplacesUnitType"], "文明独有单位替换关系"),
    "Technologies": (
        ["TechnologyType", "Name", "Cost", "EraType", "UITreeRow"],
        "科技树节点",
    ),
    "TechnologyPrereqs": (["Technology", "PrereqTech"], "科技前置关系"),
    "Civics": (["CivicType", "Name", "Cost", "EraType", "UITreeRow"], "市政树节点"),
    "CivicPrereqs": (["Civic", "PrereqCivic"], "市政前置关系"),
    "Boosts": (
        ["TechnologyType", "CivicType", "BoostClass", "Amount", "NumItems",
         "TriggerDescription"],
        "尤里卡 / 鼓舞的触发条件",
    ),
    "Policies": (
        ["PolicyType", "Name", "PrereqCivic", "GovernmentSlotType",
         "RequiresGovernmentUnlock"],
        "全部政策卡",
    ),
    "Governments": (
        ["GovernmentType", "Name", "PrereqCivic", "BonusType"],
        "政体",
    ),
    "Government_SlotCounts": (
        ["GovernmentType", "GovernmentSlotType", "NumSlots"],
        "各政体的槽位配置",
    ),
    "Districts": (
        ["DistrictType", "Name", "Cost", "PrereqTech", "PrereqCivic",
         "RequiresPopulation", "NoAdjacentCity", "Maintenance"],
        "区域",
    ),
    "District_Adjacencies": (
        ["DistrictType", "YieldChangeId"],
        "区域相邻加成挂载",
    ),
    "Adjacency_YieldChanges": (
        ["ID", "YieldType", "YieldChange", "TilesRequired", "AdjacentResource",
         "AdjacentRiver", "AdjacentDistrict", "AdjacentTerrain", "AdjacentFeature",
         "AdjacentWonder", "OtherDistrictAdjacent"],
        "相邻加成的具体规则 — 区域放置算法的依据",
    ),
    "Buildings": (
        ["BuildingType", "Name", "Cost", "PrereqTech", "PrereqCivic",
         "PrereqDistrict", "IsWonder", "Maintenance", "RequiresPlacement"],
        "建筑与奇观(IsWonder=true 即奇观)",
    ),
    "Terrains": (
        ["TerrainType", "Name", "Movement", "DefenseModifier", "Water", "Mountain",
         "Hills"],
        "地形 — 移动力消耗",
    ),
    "Features": (
        ["FeatureType", "Name", "MovementChange", "DefenseModifier", "Impassable",
         "NaturalWonder"],
        "地貌特征 — 移动力修正",
    ),
    "Improvements": (
        ["ImprovementType", "Name", "PrereqTech", "PrereqCivic", "BarbarianCamp",
         "Goody", "Buildable"],
        "改良设施(含蛮族营地标识)",
    ),
    "Resources": (
        ["ResourceType", "Name", "ResourceClassType", "PrereqTech", "Happiness"],
        "资源",
    ),
    "Difficulties": (["DifficultyType", "Name"], "难度档位"),
    "Eras": (["EraType", "Name", "ChronologyIndex"], "时代"),
    "Victories": (["VictoryType", "Name", "EnabledByDefault"], "胜利类型"),
    "GreatPersonClasses": (["GreatPersonClassType", "Name"], "伟人类别"),
    "UnitPromotions": (["UnitPromotionType", "Name", "PromotionClass", "Level",
                        "Column", "Description"],
                       "单位晋升树"),
    "UnitAbilityModifiers": (["UnitAbilityType", "ModifierId"], "单位能力挂载"),
    "TypeTags": (["Type", "Tag"], "类型标签 — 兵种克制判定依据"),
    # --- 难度参数化(方案 §8) ---
    "DifficultyBonuses": (["DifficultyType", "BonusType", "Amount"], "难度加成"),
    "HandicapInfos": (["HandicapType", "Name", "AIStartingUnits",
                       "AIStartingSettlers", "AIPerEraModifier"], "难度句柄"),
    "Difficulty_Bonuses": (["DifficultyType", "ModifierId"], "难度修正器挂载"),
    "MapSizes": (["MapSizeType", "Name", "MaxPlayers", "MaxCityStates",
                  "NumNaturalWonders"], "地图大小与文明数"),
    # --- 意图推演:AI 议程就是对手的策略函数 ---
    "Agendas": (["AgendaType", "Name", "Description"], "AI 议程"),
    "HistoricalAgendas": (["LeaderType", "AgendaType"], "领袖固定议程"),
    "Leaders": (["LeaderType", "Name", "InheritFrom"], "领袖"),
    # --- 政策卡效果(自动检查用) ---
    "PolicyModifiers": (["PolicyType", "ModifierId"], "政策卡效果挂载"),
    "Modifiers": (["ModifierId", "ModifierType", "SubjectRequirementSetId"],
                  "修正器定义"),
    "ModifierArguments": (["ModifierId", "Name", "Value"], "修正器参数"),
    # --- 中后期:胜利与项目 ---
    "Projects": (["ProjectType", "Name", "Cost", "PrereqTech", "PrereqCivic",
                  "MaxPlayerInstances", "PrereqDistrict"], "项目(太空船等)"),
    "GreatPersonIndividuals": (["GreatPersonIndividualType", "Name",
                                "GreatPersonClassType", "EraType"], "伟人个体"),
}


async def export_one(conn, table: str, cols: list[str], desc: str) -> dict:
    lua = _lua(table, cols)
    try:
        lines = await conn.execute_read(lua)
    except Exception as e:
        print(f"  ✗ {table:26s} 查询失败: {type(e).__name__}: {str(e)[:80]}")
        return {"table": table, "desc": desc, "columns": cols,
                "error": f"{type(e).__name__}: {e}", "rows": []}

    if any("__TABLE_MISSING__" in l for l in lines):
        print(f"  – {table:26s} 本规则集不存在")
        return {"table": table, "desc": desc, "columns": cols,
                "missing": True, "rows": []}

    rows = []
    for l in lines:
        if l.startswith("__COUNT__") or l.startswith("---END---") or not l.strip():
            continue
        vals = l.split("\t")
        # 容错:列数对不上也保留,便于排查
        rows.append(dict(zip(cols, vals + [""] * (len(cols) - len(vals)))))

    print(f"  ✓ {table:26s} {len(rows):4d} 行   {desc}")
    return {"table": table, "desc": desc, "columns": cols, "rows": rows}


def write_markdown(data: dict) -> str:
    """生成人读的 Markdown 表格。"""
    t, rows, cols = data["table"], data["rows"], data["columns"]
    out = [f"# {t}", "", f"> {data['desc']}", ""]
    if data.get("missing"):
        out.append("**本规则集不存在此表。**")
        return "\n".join(out)
    if data.get("error"):
        out.append(f"**导出失败:** `{data['error']}`")
        return "\n".join(out)
    out.append(f"共 {len(rows)} 行。")
    out.append("")
    # 只保留有值的列,避免一片空
    used = [c for c in cols if any(r.get(c, "") not in ("", "false") for r in rows)]
    if not used:
        used = cols
    out.append("| " + " | ".join(used) + " |")
    out.append("|" + "|".join(["---"] * len(used)) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")).replace("|", "\\|")
                                     for c in used) + " |")
    return "\n".join(out)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("连接游戏 ...")
    conn = GameConnection()
    await conn.connect()
    print(f"已连接。GameCore={conn.gamecore_index} InGame={conn.ingame_index}\n")

    # 先记录本局规则集,导出结果只对这个规则集有效
    meta_lines = await conn.execute_read(
        'print("RULESET\\t" .. tostring(GameConfiguration.GetValue("RULESET"))) '
        'print("---END---")'
    )
    ruleset = next((l.split("\t")[1] for l in meta_lines if l.startswith("RULESET")),
                   "UNKNOWN")
    print(f"本局规则集: {ruleset}\n")

    all_data = {"ruleset": ruleset, "tables": {}}
    for table, (cols, desc) in TABLES.items():
        d = await export_one(conn, table, cols, desc)
        all_data["tables"][table] = d
        (OUT / f"{table}.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        (OUT / f"{table}.md").write_text(write_markdown(d), encoding="utf-8")

    (OUT / "_all.json").write_text(
        json.dumps(all_data, ensure_ascii=False, indent=1), encoding="utf-8")

    # 汇总索引
    idx = [f"# 游戏规则数据导出", "",
           f"> 规则集: **{ruleset}** — 从运行中的游戏直接导出,与本机实际加载的规则一致。", "",
           "| 表 | 行数 | 说明 |", "|---|---|---|"]
    for t, d in all_data["tables"].items():
        n = "不存在" if d.get("missing") else ("失败" if d.get("error") else len(d["rows"]))
        idx.append(f"| [{t}]({t}.md) | {n} | {d['desc']} |")
    (OUT / "README.md").write_text("\n".join(idx), encoding="utf-8")

    await conn.disconnect()
    print(f"\n导出完成 -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
