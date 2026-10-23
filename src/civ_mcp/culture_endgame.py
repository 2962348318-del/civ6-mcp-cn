"""任务 AB —— 文化胜利收尾。

文化胜利是四条路线里最"非线性"的一条:判据不是文化产出,是**旅游业绩**
(你的外国游客 > 任一对手的国内游客)。最后 50 回合要做的事和前面 200 回合
完全不同 —— 前面攒的是文化,最后砸的是游客。

五块检查,全部按导出数据判定:

    Modifiers/ModifierArguments  旅游加成的实际倍率(全部反查,不是记忆):
        COMPUTERS_BOOST_ALL_TOURISM        +100% 全部旅游(TECH_COMPUTERS)
        HERITAGE_*TOURISM                  遗产旅游卡:各类巨作 ×2(200)
        SATELLITEBROADCASTS_MUSICTOURISM   音乐巨作 ×3(300)
        ONLINECOMMUNITIES_TOURISM          贸易路线旅游 +50%
        CRISTOREDENTOR_BEACHTOURISM        海滨度假区 ×2(200)
        EIFFEL_TOWER_ADDAPPEAL             全境 +2 魅力
        CONSERVATION_*_WALL_TOURISM        保护地球:城墙也产旅游(1/2/3)
    Improvements.json            IMPROVEMENT_BEACH_RESORT(海滨度假区)存在
    Units.json                   UNIT_NATURALIST(自然学家)存在 → 国家公园可建
    Boosts.json                  BOOST_TRIGGER_CREATED_NATIONAL_PARK 存在
    Civics/Technologies          各加成的解锁前置

⚠️ 三处边界:

  1. **摇滚乐队在本规则集不存在**。Units 表里没有任何 ROCK_BAND 条目
     (全表搜 ROCK 只命中 UNIT_ROCKET_ARTILLERY 火箭炮)—— 它是
     Gathering Storm 内容。计划书要求做的"选晋升 / 选演出城市 / 算解散
     概率"在本局全部无从谈起,本模块据此保持沉默,不生成假建议。

  2. **魅力(Appeal)数值不在导出表里**。Terrains 只有 Movement/Defense/
     Water/Mountain/Hills,Features 只有 MovementChange/Defense/Impassable/
     NaturalWonder —— 都没有 Appeal 列。国家公园要求"四格魅力 ≥2",这个
     判定必须靠实时查询;本模块只做**几何形状**校验(四叶菱形),魅力由
     调用方传入,没传就明说"魅力需游戏内确认"。

  3. **主题化的配对规则未导出**(没有 GreatWorks/GreatWorkSlots 表)。
     只能按调用方给的作品清单做同类型计数,不承诺具体主题化条件。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from functools import lru_cache
from pathlib import Path

_GAMEDATA = gamedata_path()

# 国家公园的几何:竖菱形四格。相对首格的偏移(平顶六边形网格的列偏移布局)。
# 只校验形状,魅力另算 —— 形状是确定的,魅力数据未导出。
NATIONAL_PARK_SHAPE_SIZE = 4
MIN_PARK_APPEAL = 2  # 计划书给定的魅力门槛(未导出,标注为机制值)


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


@lru_cache(maxsize=1)
def _mod_index() -> tuple[dict, dict]:
    mods = {r.get("ModifierId"): r.get("ModifierType", "") for r in _rows("Modifiers")}
    args: dict[str, dict] = {}
    for r in _rows("ModifierArguments"):
        args.setdefault(r.get("ModifierId", ""), {})[r.get("Name", "")] = r.get(
            "Value", ""
        )
    return mods, args


def _arg(modifier_id: str, name: str) -> str:
    return _mod_index()[1].get(modifier_id, {}).get(name, "")


@lru_cache(maxsize=1)
def rock_band_available() -> bool:
    """本规则集有没有摇滚乐队。Units 表搜不到 → False(原版就是没有)。"""
    return any("ROCK_BAND" in (r.get("UnitType") or "") for r in _rows("Units"))


@lru_cache(maxsize=1)
def naturalist_available() -> bool:
    """有自然学家才谈得上国家公园。"""
    return any("NATURALIST" in (r.get("UnitType") or "") for r in _rows("Units"))


@lru_cache(maxsize=1)
def beach_resort_available() -> bool:
    return any(
        r.get("ImprovementType") == "IMPROVEMENT_BEACH_RESORT"
        for r in _rows("Improvements")
    )


@lru_cache(maxsize=1)
def tourism_boosters() -> tuple[dict, ...]:
    """全部旅游加成来源 → [{key, cn, kind, amount, prereq, prereq_kind}]。

    数值全部从 ModifierArguments 反查,前置从 Technologies/Civics/Policies 查。
    """
    civics = {
        r["CivicType"]: r.get("Name") or r["CivicType"]
        for r in _rows("Civics")
        if r.get("CivicType")
    }
    techs = {
        r["TechnologyType"]: r.get("Name") or r["TechnologyType"]
        for r in _rows("Technologies")
        if r.get("TechnologyType")
    }
    policies = {r["PolicyType"]: r for r in _rows("Policies") if r.get("PolicyType")}

    out: list[dict] = []

    # 1) 电脑:全部旅游 +100%
    amt = _arg("COMPUTERS_BOOST_ALL_TOURISM", "Amount")
    if amt:
        out.append(
            {
                "key": "TECH_COMPUTERS",
                "cn": techs.get("TECH_COMPUTERS", "电脑"),
                "kind": "tech",
                "effect": f"全部旅游业绩 +{amt}%",
                "why": "单项最大的旅游倍率,文化收尾的第一优先科技",
            }
        )

    # 2) 遗产旅游卡:各类巨作 ×2
    heritage = [
        m
        for m in _mod_index()[0]
        if m.startswith("HERITAGE_") and m.endswith("TOURISM")
    ]
    if heritage and "POLICY_HERITAGE_TOURISM" in policies:
        scale = _arg(heritage[0], "ScalingFactor") or "200"
        row = policies["POLICY_HERITAGE_TOURISM"]
        out.append(
            {
                "key": "POLICY_HERITAGE_TOURISM",
                "cn": row.get("Name", "遗产旅游"),
                "kind": "policy",
                "effect": f"{len(heritage)} 类巨作的旅游业绩 ×{int(scale) / 100:g}",
                "why": f"解锁于 {civics.get(row.get('PrereqCivic', ''), row.get('PrereqCivic', ''))}",
            }
        )

    # 3) 卫星广播:音乐巨作 ×3
    scale = _arg("SATELLITEBROADCASTS_MUSICTOURISM", "ScalingFactor")
    if scale and "POLICY_SATELLITE_BROADCASTS" in policies:
        row = policies["POLICY_SATELLITE_BROADCASTS"]
        out.append(
            {
                "key": "POLICY_SATELLITE_BROADCASTS",
                "cn": row.get("Name", "卫星广播"),
                "kind": "policy",
                "effect": f"音乐巨作旅游 ×{int(scale) / 100:g}",
                "why": "有大音乐家作品时收益极高,没有就别插",
            }
        )

    # 4) 在线社区:贸易路线旅游 +50%
    amt = _arg("ONLINECOMMUNITIES_TOURISM", "Amount")
    if amt and "POLICY_ONLINE_COMMUNITIES" in policies:
        row = policies["POLICY_ONLINE_COMMUNITIES"]
        out.append(
            {
                "key": "POLICY_ONLINE_COMMUNITIES",
                "cn": row.get("Name", "在线社区"),
                "kind": "policy",
                "effect": f"每条贸易路线的旅游 +{amt}%",
                "why": "和「给每个文明派贸易路线」叠乘,路线越多越值",
            }
        )

    # 5) 保护地球:城墙产旅游(顺带把废墙变资产)
    wall_amounts = [
        _arg(f"CONSERVATION_{k}_WALL_TOURISM", "Amount")
        for k in ("ANCIENT", "MEDIEVAL", "RENAISSANCE")
    ]
    wall_amounts = [a for a in wall_amounts if a]
    if wall_amounts and "CIVIC_CONSERVATION" in civics:
        out.append(
            {
                "key": "CIVIC_CONSERVATION",
                "cn": civics["CIVIC_CONSERVATION"],
                "kind": "civic",
                "effect": f"城墙开始产旅游({'/'.join(wall_amounts)})",
                "why": "也是国家公园的前置市政线,文化局必点",
            }
        )

    return tuple(out)


@lru_cache(maxsize=1)
def eiffel_appeal() -> int:
    """埃菲尔铁塔的全境魅力加成(导出值)。查不到返回 0。"""
    try:
        return int(_arg("EIFFEL_TOWER_ADDAPPEAL", "Amount") or 0)
    except (TypeError, ValueError):
        return 0


@lru_cache(maxsize=1)
def beach_resort_multiplier() -> float:
    """救世基督像对海滨度假区旅游的倍率(导出值)。查不到返回 0。"""
    try:
        return int(_arg("CRISTOREDENTOR_BEACHTOURISM", "ScalingFactor") or 0) / 100
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 国家公园:几何校验(魅力未导出,由调用方给)
# ---------------------------------------------------------------------------


def is_valid_park_shape(tiles: list[dict]) -> bool:
    """四格是否构成竖菱形(国家公园的合法形状)。

    判据:恰好 4 格、同属 2 个相邻列(x 差 ≤1)、每列 2 格且纵向相邻。
    这是形状的必要条件;游戏内还要求同城归属与未开发等,以实机为准。
    """
    try:
        pts = []
        for t in tiles or []:
            if not isinstance(t, dict):
                return False
            pts.append((int(t["x"]), int(t["y"])))
        if len(pts) != NATIONAL_PARK_SHAPE_SIZE or len(set(pts)) != 4:
            return False
        xs = sorted({x for x, _y in pts})
        if len(xs) != 2 or xs[1] - xs[0] != 1:
            return False
        for x in xs:
            col = sorted(y for cx, y in pts if cx == x)
            if len(col) != 2 or col[1] - col[0] != 1:
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def check_park_candidate(tiles: list[dict]) -> tuple[bool, str]:
    """一组四格能不能建国家公园 → (行不行, 原因)。魅力缺失时明说。"""
    if not is_valid_park_shape(tiles):
        return False, "形状不是竖菱形四格"
    appeals = [t.get("appeal") for t in tiles]
    if any(a is None for a in appeals):
        return True, (
            f"形状合法;魅力值未导出(Terrains/Features 无 Appeal 列)"
            f"—— 需在游戏内确认四格魅力是否都 ≥{MIN_PARK_APPEAL}"
        )
    try:
        low = min(int(a) for a in appeals)
    except (TypeError, ValueError):
        return True, "形状合法;魅力值无法解析,需游戏内确认"
    if low >= MIN_PARK_APPEAL:
        return True, f"形状合法,最低魅力 {low} ≥ {MIN_PARK_APPEAL} —— 可建"
    return False, (
        f"形状合法但最低魅力仅 {low}(需 ≥{MIN_PARK_APPEAL})"
        f"—— 种森林/建埃菲尔铁塔可补魅力"
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def get_culture_endgame_advice(game_state: dict, max_items: int = 6) -> list[str]:
    """文化胜利收尾检查。非文化路线或无可操作项时返回空列表,不抛异常。

    game_state:
        victory_type      : 锁定的胜利路线(非 CULTURE 时静默)
        techs / civics    : set[str] 已完成
        slotted_policies  : tuple[str] 已插的卡
        park_candidates   : [[{x,y,appeal?}×4], ...] 候选公园位
        coastal_tiles     : int 可建海滨度假区的海岸格数
        has_naturalist    : bool 手上有自然学家
        faith             : int 信仰余额(买自然学家用)
        great_works       : {类型: 件数} 如 {"MUSIC": 2}
        open_borders_with / trade_routes_to : tuple[str] 已开边界/已派路线的文明
        known_civs        : tuple[str] 已认识的文明
    """
    try:
        if not isinstance(game_state, dict):
            return []  # 没有局面数据就没有建议,不拿默认值硬凑
        return _culture_inner(game_state, max_items)
    except Exception:
        return []


def _culture_inner(gs: dict, max_items: int) -> list[str]:
    vt = str(gs.get("victory_type") or "").upper()
    if vt and vt != "CULTURE":
        return []
    techs = gs.get("techs") if isinstance(gs.get("techs"), (set, frozenset)) else set()
    civics = (
        gs.get("civics") if isinstance(gs.get("civics"), (set, frozenset)) else set()
    )
    slotted = set(gs.get("slotted_policies") or ())
    have = techs | civics | slotted

    out: list[str] = []

    # 1. 旅游加成检查清单 —— 缺哪项报哪项
    missing = [b for b in tourism_boosters() if b["key"] not in have]
    if missing:
        for b in missing[:2]:
            out.append(f"· 缺「{b['cn']}」:{b['effect']} —— {b['why']}")
        if len(missing) > 2:
            rest = "、".join(b["cn"] for b in missing[2:])
            out.append(f"   (还缺:{rest})")

    # 2. 开放边界 / 贸易路线 —— 各 +25%(机制值,倍率本身未导出)
    known = tuple(gs.get("known_civs") or ())
    if known:
        no_border = [c for c in known if c not in (gs.get("open_borders_with") or ())]
        no_route = [c for c in known if c not in (gs.get("trade_routes_to") or ())]
        if no_border:
            out.append(
                f"· {len(no_border)}/{len(known)} 个文明没有开放边界 —— "
                f"开放边界给你 +25% 对他的旅游(倍率未导出,机制成立):"
                f"{('、'.join(no_border[:3]))}"
            )
        if no_route:
            out.append(
                f"· {len(no_route)}/{len(known)} 个文明没有你的贸易路线 —— "
                f"同样 +25%,而且和「在线社区」叠乘:{('、'.join(no_route[:3]))}"
            )

    # 3. 国家公园
    if naturalist_available():
        cands = gs.get("park_candidates") or []
        ok = []
        for group in cands:
            valid, why = check_park_candidate(group)
            if valid:
                ok.append((group, why))
        if ok:
            first = ok[0]
            xy = "、".join(f"({t.get('x')},{t.get('y')})" for t in first[0])
            out.append(
                f"· 国家公园候选:{xy} —— {first[1]}"
                + (f"(另有 {len(ok) - 1} 处)" if len(ok) > 1 else "")
            )
            if not gs.get("has_naturalist"):
                out.append(f"   需要自然学家(信仰购买,当前信仰 {gs.get('faith', 0)})。")
        elif cands:
            out.append(
                "· 提供的候选位没有一处合法(需竖菱形四格)——"
                "国家公园对形状的要求很硬,先按形状筛再看魅力。"
            )

    # 4. 海滨度假区
    coastal = gs.get("coastal_tiles")
    if beach_resort_available() and coastal:
        mult = beach_resort_multiplier()
        line = (
            f"· 有 {coastal} 块可建海滨度假区的海岸格 —— "
            f"产出随魅力递增(魅力数据未导出,游戏内确认)"
        )
        if mult:
            line += f";建成救世基督像后这些度假区的旅游 ×{mult:g}"
        appeal = eiffel_appeal()
        if appeal:
            line += f";埃菲尔铁塔全境 +{appeal} 魅力,是度假区和国家公园的共同前提"
        out.append(line)

    # 5. 主题化(配对规则未导出,只做同类型计数)
    works = gs.get("great_works") or {}
    if isinstance(works, dict) and works:
        pairs = [(k, v) for k, v in works.items() if isinstance(v, int) and v >= 3]
        if pairs:
            names = "、".join(f"{k}×{v}" for k, v in pairs[:2])
            out.append(
                f"· 主题化机会:{names} —— 同主题填满槽位可让该建筑旅游翻倍。"
                f"(具体配对条件未导出,以游戏内主题化提示为准)"
            )

    # 6. 摇滚乐队:本规则集没有,说清楚免得等
    if not rock_band_available():
        out.append(
            "· 摇滚乐队在本规则集【不存在】(Units 表无 ROCK_BAND,"
            "属 Gathering Storm 内容)—— 计划书里的乐队打法本局用不了,"
            "旅游全靠巨作+奇观+公园+度假区这四条腿。"
        )

    if not out:
        return []
    if len(out) > max_items:
        omitted = len(out) - max_items
        out = out[:max_items]
        out.append(f"   (另有 {omitted} 条文化收尾提醒已省略)")
    return ["== 文化胜利收尾 ==", *out]
