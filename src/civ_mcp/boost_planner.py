"""任务 K —— 尤里卡/鼓舞链规划。

尤里卡(科技)/鼓舞(市政)砍掉一大截研究成本——人类高手开局就排好
"先做什么触发什么"的链,AI 随机选科技等于每回合白扔这笔折扣。

⚠️ 导出数据的三个硬边界(决定了本模块能诚实做到哪一步):

1. **Boost 百分比未导出**。Boosts.json 有 Amount 列但 112 行全为空串。
   计划书按 40% 计,原版社区共识为 50%——本模块用保守值 0.4 做数学
   (BOOST_FRACTION),文案写"40-50%"。宁可低估节省,不虚报。

2. **具体目标物未导出**。Boosts.json 只有 6 列(Tech/Civic/BoostClass/
   Amount/NumItems/TriggerDescription),TriggerDescription 是 LOC_ 本地化键
   不是文本。于是"BOOST_TRIGGER_HAVE_X_BUILDINGS"知道要"拥有 X 个某建筑",
   但**不知道是哪个建筑**(真实库里的目标列没被导出脚本带出来)。
   ⇒ 这类 boost 一律归 ❓,理由写明"目标物未导出",绝不猜。

3. **跨树引用的目标同样未导出**。BOOST_TRIGGER_RESEARCH_TECH(市政的
   触发条件是研究某科技)/BOOST_TRIGGER_CULTURVATE_CIVIC(反向)——
   哪个科技/市政,数据里没有 ⇒ ❓。

三分类(计划书任务 K 的定义):
    ✅ 已触发  caller 传入的 completed_boosts 里、且对应节点还没研究完
              ——折扣已入账,等着收割
    🔜 可达    触发条件明确、且当前局面有能力达成(判定器见 _PREDICATES)
    ❓ 不可达/无法判定
              依赖未来变数(遇文明/探大陆),或判定所需字段缺失,
              或目标物未导出

另有一类特殊:BOOST_TRIGGER_NONE_LATE_GAME_CRITICAL_TECH(6 条)——
这些后期关键科技**设计上没有尤里卡**,永不参与折扣。

## game_state 输入契约(dict,全部可缺省;缺失 → 相关判定归 ❓)
    builders / military_units / land_units / cities : int
    empire_population / max_city_population : int
    improved_tiles : int          已改良地块数
    known_camps : int             已发现的蛮族营地数
    met_civs / met_city_states / continents_discovered : int
    has_pantheon / has_religion : bool
    trade_capacity / active_trade_routes : int
    unique_specialty_districts : int   已建的不同专业区域种数
    coastal_settle_available : bool    有已知的沿海铺城位
    policy_slots : int                 当前政策槽总数
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from functools import lru_cache
from pathlib import Path

from civ_mcp import tech_path

_GAMEDATA = gamedata_path()

# 折扣比例:导出表 Amount 列全空,精确值未知。保守取 0.4(计划书值);
# 原版社区共识 50%。文案统一写"40-50%",数学用 0.4。
BOOST_FRACTION = 0.4
DISCOUNT = 1.0 - BOOST_FRACTION  # 路径权重系数 0.6

_NONE_CLASS = "BOOST_TRIGGER_NONE_LATE_GAME_CRITICAL_TECH"

ST_BANKED = "banked"  # ✅ 已触发待收割
ST_REACHABLE = "reachable"  # 🔜 可达
ST_UNDECIDABLE = "undecidable"  # ❓
ST_NONE = "none"  # 设计上无 boost


def _rows() -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / "Boosts.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


@lru_cache(maxsize=1)
def all_boosts() -> tuple[dict, ...]:
    """全部 boost 行,附解析出的 (kind, node)。格式异常的行丢弃。"""
    out = []
    for r in _rows():
        tech, civic = r.get("TechnologyType") or "", r.get("CivicType") or ""
        if tech and not civic:
            kind, node = "tech", tech
        elif civic and not tech:
            kind, node = "civic", civic
        else:
            continue  # 两者皆空/皆有 → 格式异常,丢弃
        try:
            num = int(r.get("NumItems") or 0)
        except (TypeError, ValueError):
            num = 0
        out.append(
            {"kind": kind, "node": node, "cls": r.get("BoostClass") or "", "num": num}
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# 可达性判定器
# ---------------------------------------------------------------------------
# 每个判定器:(game_state, num) -> (status, 行动提示)。
# 只有"条件不含未导出目标物、且可从 game_state 判定"的类别才有判定器;
# 其余一律 ❓。判定所需字段缺失 → ❓ 并写明缺哪个。


def _need(gs: dict, key: str):
    """取字段;缺失返回 None(判定器据此归 ❓)。"""
    return gs.get(key) if isinstance(gs, dict) else None


def _p_improved_tiles(gs, num):
    b = _need(gs, "builders")
    if b is None:
        return ST_UNDECIDABLE, "缺 builders 字段"
    if b >= 1:
        done = _need(gs, "improved_tiles")
        prog = f"(当前 {done}/{num})" if done is not None else ""
        return ST_REACHABLE, f"用建造者改良 {num} 块地{prog}"
    return ST_UNDECIDABLE, "没有建造者"


def _p_clear_camp(gs, num):
    mil, camps = _need(gs, "military_units"), _need(gs, "known_camps")
    if mil is None or camps is None:
        return ST_UNDECIDABLE, "缺 military_units/known_camps 字段"
    if mil >= 1 and camps >= 1:
        return ST_REACHABLE, f"出兵拆已发现的营地({camps} 个已知)"
    return ST_UNDECIDABLE, "无军队" if mil < 1 else "没发现营地"


def _p_empire_pop(gs, num):
    if not _need(gs, "cities"):
        return ST_UNDECIDABLE, "缺 cities 字段或无城市"
    cur = _need(gs, "empire_population")
    prog = f"(当前 {cur})" if cur is not None else ""
    return ST_REACHABLE, f"全国人口达 {num}{prog}——自然增长可达"


def _p_city_pop(gs, num):
    if not _need(gs, "cities"):
        return ST_UNDECIDABLE, "缺 cities 字段或无城市"
    cur = _need(gs, "max_city_population")
    prog = f"(当前最高 {cur})" if cur is not None else ""
    return ST_REACHABLE, f"单城人口达 {num}{prog}——粮食堆上去就行"


def _p_pantheon(gs, num):
    hp = _need(gs, "has_pantheon")
    if hp is None:
        return ST_UNDECIDABLE, "缺 has_pantheon 字段"
    if not hp:
        return ST_REACHABLE, "攒信仰立万神殿"
    return ST_REACHABLE, "万神殿已立——若尚未入账,下回合应自动触发"


def _p_found_religion(gs, num):
    hr, hp = _need(gs, "has_religion"), _need(gs, "has_pantheon")
    if hr is None:
        return ST_UNDECIDABLE, "缺 has_religion 字段"
    if hr:
        return ST_REACHABLE, "已创教——若尚未入账,应自动触发"
    if hp:
        return ST_REACHABLE, "用大预言家创教(已有万神殿)"
    return ST_UNDECIDABLE, "还没有万神殿,创教尚远"


def _p_unique_districts(gs, num):
    if not _need(gs, "cities"):
        return ST_UNDECIDABLE, "缺 cities 字段或无城市"
    cur = _need(gs, "unique_specialty_districts")
    prog = f"(当前 {cur}/{num})" if cur is not None else ""
    return ST_REACHABLE, f"建满 {num} 种不同的专业区域{prog}"


def _p_barbs_killed(gs, num):
    mil = _need(gs, "military_units")
    if mil is None:
        return ST_UNDECIDABLE, "缺 military_units 字段"
    if mil >= 1:
        return ST_REACHABLE, f"击杀 {num} 个蛮族单位——蛮族无限刷,早晚凑够"
    return ST_UNDECIDABLE, "无军队"


def _p_trade_routes(gs, num):
    cap = _need(gs, "trade_capacity")
    if cap is None:
        return ST_UNDECIDABLE, "缺 trade_capacity 字段"
    if cap >= 1:
        cur = _need(gs, "active_trade_routes")
        prog = f"(当前 {cur}/{num})" if cur is not None else ""
        return ST_REACHABLE, f"维持 {num} 条贸易路线{prog}"
    return ST_UNDECIDABLE, "还没有贸易路线容量"


def _p_land_units(gs, num):
    if not _need(gs, "cities"):
        return ST_UNDECIDABLE, "缺 cities 字段或无城市"
    cur = _need(gs, "land_units")
    prog = f"(当前 {cur}/{num})" if cur is not None else ""
    return ST_REACHABLE, f"拥有 {num} 个陆军单位{prog}——产能问题而已"


def _p_settle_coast(gs, num):
    ok = _need(gs, "coastal_settle_available")
    if ok is None:
        return ST_UNDECIDABLE, "缺 coastal_settle_available 字段"
    if ok:
        return ST_REACHABLE, "在沿海铺一座城"
    return ST_UNDECIDABLE, "没有已知的沿海铺城位"


def _p_policy_slots(gs, num):
    """槽位总数类别自含语义(NumItems 即目标),不需要未导出的目标物。"""
    cur = _need(gs, "policy_slots")
    if cur is None:
        return ST_UNDECIDABLE, "缺 policy_slots 字段"
    return ST_REACHABLE, f"政策槽总数达 {num}(当前 {cur})——换更高级政体推进"


_PREDICATES = {
    "BOOST_TRIGGER_NUM_IMPROVED_TILES": _p_improved_tiles,
    "BOOST_TRIGGER_CLEAR_CAMP": _p_clear_camp,
    "BOOST_TRIGGER_EMPIRE_POPULATION": _p_empire_pop,
    "BOOST_TRIGGER_CITY_POPULATION": _p_city_pop,
    "BOOST_TRIGGER_CREATE_PANTHEON": _p_pantheon,
    "BOOST_TRIGGER_FOUND_RELIGION": _p_found_religion,
    "BOOST_TRIGGER_HAVE_X_UNIQUE_SPECIALTY_DISTRICTS": _p_unique_districts,
    "BOOST_TRIGGER_NUM_BARBS_KILLED": _p_barbs_killed,
    "BOOST_TRIGGER_MAINTAIN_X_TRADE_ROUTES": _p_trade_routes,
    "BOOST_TRIGGER_HAVE_X_LAND_UNITS": _p_land_units,
    "BOOST_TRIGGER_SETTLE_COAST": _p_settle_coast,
    "BOOST_TRIGGER_HAVE_X_POLICY_SLOTS": _p_policy_slots,
}

# 依赖未来变数的类别 → ❓ 的原因(计划书自己的例子:"遇见另一个文明→待定")
_FUTURE_CLASSES = {
    "BOOST_TRIGGER_MEET_CIV": "依赖探索,遇不遇得到不由你定",
    "BOOST_TRIGGER_MEET_X_CITY_STATES": "依赖探索",
    "BOOST_TRIGGER_DISCOVER_CONTINENT": "依赖未探明的地理",
    "BOOST_TRIGGER_FIND_NATURAL_WONDER": "依赖未探明的地理",
    "BOOST_TRIGGER_RECEIVE_DOW": "依赖对手行为(被宣战)",
    "BOOST_TRIGGER_DOW_CASUS_BELLI": "依赖外交局面",
    "BOOST_TRIGGER_HAVE_AN_ALLIANCE": "依赖外交进度",
    "BOOST_TRIGGER_HAVE_RESEARCH_AGREEMENT": "依赖外交进度",
}


def classify_boost(
    boost: dict,
    researched: set[str],
    completed_boosts: set[str],
    game_state: dict,
) -> tuple[str, str]:
    """单条 boost 三分类 → (status, 说明)。见模块 docstring 的分类定义。"""
    node, cls = boost["node"], boost["cls"]
    if cls == _NONE_CLASS:
        return ST_NONE, "该科技设计上没有尤里卡"
    if node in completed_boosts:
        return ST_BANKED, "已触发,研究它时自动享受折扣"
    pred = _PREDICATES.get(cls)
    if pred is not None:
        try:
            return pred(game_state or {}, boost["num"])
        except Exception:
            return ST_UNDECIDABLE, "判定器异常"
    if cls in _FUTURE_CLASSES:
        return ST_UNDECIDABLE, _FUTURE_CLASSES[cls]
    return ST_UNDECIDABLE, "触发目标物未导出(Boosts 表只有类别没有对象)"


# ---------------------------------------------------------------------------
# 与 tech_path 的协作:折扣表
# ---------------------------------------------------------------------------


def boost_discounts(
    kind: str,
    researched: set[str],
    completed_boosts: set[str],
    game_state: dict,
) -> dict[str, float]:
    """节点 → 权重系数。已触发/可达的 boost → ×0.6(DISCOUNT);
    其余不出现(全额)。供 tech_path 的闭包代价加权。"""
    out: dict[str, float] = {}
    for b in all_boosts():
        if b["kind"] != kind or b["node"] in researched:
            continue
        status, _ = classify_boost(b, researched, completed_boosts, game_state)
        if status in (ST_BANKED, ST_REACHABLE):
            out[b["node"]] = DISCOUNT
    return out


# ---------------------------------------------------------------------------
# 每回合建议
# ---------------------------------------------------------------------------


def get_boost_advice(
    current_techs: set[str],
    completed_boosts: set[str],
    game_state: dict,
    current_civics: set[str] | None = None,
    current_science: float = 0,
    max_items: int = 3,
) -> list[str]:
    """当前最值得触发的尤里卡/鼓舞。无可达项时返回空列表,不抛异常。"""
    try:
        return _advice_inner(
            set(current_techs or ()),
            set(completed_boosts or ()),
            game_state or {},
            set(current_civics or ()),
            current_science,
            max_items,
        )
    except Exception:
        return []


# 目标离当前超过这么多步的 boost 不进建议——"单城人口 15"对远古玩家是噪音
NEAR_HORIZON = 8
# 这么多步以内算"眼前的",优先于远一点但省更多的
IMMEDIATE_HORIZON = 3


def _advice_inner(techs, completed, gs, civics, science, max_items) -> list[str]:
    researched = techs | civics
    reachable: list[tuple[int, float, dict, str]] = []
    banked: list[dict] = []
    undecidable = 0
    for b in all_boosts():
        if b["node"] in researched:
            continue
        status, note = classify_boost(b, researched, completed, gs)
        if status == ST_BANKED:
            banked.append(b)
        elif status == ST_REACHABLE:
            have = techs if b["kind"] == "tech" else civics
            steps = len(tech_path.prereq_closure(b["node"], have, b["kind"]))
            if steps == 0 or steps > NEAR_HORIZON:
                continue  # 已研究(不该发生)或太远期——链规划只管眼前
            saving = tech_path.node_cost(b["node"]) * BOOST_FRACTION
            reachable.append((steps, saving, b, note))
        elif status == ST_UNDECIDABLE:
            undecidable += 1
    if not reachable and not banked:
        return []

    out = ["== 尤里卡/鼓舞机会(白送的 40-50% 研究) =="]
    # 眼前的(≤3 步)优先;同层按节省额降序
    reachable.sort(key=lambda x: (x[0] > IMMEDIATE_HORIZON, -x[1], x[2]["node"]))
    for _steps, saving, b, note in reachable[:max_items]:
        cn = tech_path.node_cn(b["node"])
        cost = tech_path.node_cost(b["node"])
        line = f"· {cn}({cost}):{note} → 省约 {saving:.0f}"
        turns_saved = tech_path.estimate_turns(saving, science)
        if turns_saved:
            line += f"(≈{turns_saved} 回合)"
        out.append(line)

    # 衔接 tech_path:给排名第一的可达 boost 一条"先触发再研究"的路线
    if reachable:
        _, _, top, _ = reachable[0]
        kind = top["kind"]
        have = techs if kind == "tech" else civics
        disc = boost_discounts(kind, researched, completed, gs)
        path = tech_path.prereq_closure(top["node"], have, kind)
        if path and science > 0:
            raw = tech_path.path_cost(path)
            eff = tech_path.path_cost(path, disc)
            t_raw = tech_path.estimate_turns(raw, science)
            t_eff = tech_path.estimate_turns(eff, science)
            if t_raw > t_eff:
                out.append(
                    f"   衔接:先触发上面第一条,再走 "
                    f"{'→'.join(tech_path.node_cn(n) for n in path[:3])}"
                    f"{'…' if len(path) > 3 else ''}"
                    f" —— 原约 {t_raw} 回合,触发后约 {t_eff} 回合。"
                )

    if banked:
        names = "、".join(tech_path.node_cn(b["node"]) for b in banked[:4])
        more = f" 等 {len(banked)} 项" if len(banked) > 4 else ""
        out.append(f"⚡ 已触发待收割:{names}{more}——研究它们时折扣自动生效。")
    if undecidable:
        out.append(
            f"   (另有 {undecidable} 条无法判定:依赖探索/外交,或触发目标物未在导出表中)"
        )
    out.append("   (折扣比例未导出,按保守 40% 估;实际为 40-50%)")
    return out
