"""任务 B 科技/市政路径规划 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import tech_path as tp

_GS = {
    "builders": 1,
    "military_units": 2,
    "known_camps": 1,
    "cities": 2,
    "empire_population": 5,
    "max_city_population": 4,
    "has_pantheon": False,
    "trade_capacity": 1,
    "land_units": 3,
    "unique_specialty_districts": 0,
    "coastal_settle_available": True,
}


# --- 图加载:节点与边数量对齐导出表 ---


def test_graphs_load_all_nodes_and_edges():
    assert len(tp._tech_nodes()) == 68
    assert len(tp._civic_nodes()) == 51
    assert sum(len(v) for v in tp._tech_prereqs().values()) == 90
    assert sum(len(v) for v in tp._civic_prereqs().values()) == 69


def test_trees_are_independent():
    """科技树和市政树绝不互接。"""
    assert not set(tp._tech_nodes()) & set(tp._civic_nodes())
    for pres in tp._tech_prereqs().values():
        assert all(p.startswith("TECH_") for p in pres)
    for pres in tp._civic_prereqs().values():
        assert all(p.startswith("CIVIC_") for p in pres)


# --- 闭包(计划书点名的两条已知路径) ---


def test_iron_working_closure():
    """铁器 ← 铸铜 ← 采矿:计划书要求验证的第一条已知路径。"""
    path = tp.prereq_closure("TECH_IRON_WORKING", set())
    assert path == ["TECH_MINING", "TECH_BRONZE_WORKING", "TECH_IRON_WORKING"]


def test_satellites_closure_contains_rocketry_before_it():
    """卫星的闭包必须含火箭研究且排在前面:第二条已知路径。"""
    path = tp.prereq_closure("TECH_SATELLITES", set())
    assert path.index("TECH_ROCKETRY") < path.index("TECH_SATELLITES")


def test_closure_respects_all_prereqs_for_every_node():
    """结构不变量:闭包里每个节点的全部前置,要么已研究要么排在它前面。
    对全部 68+51 个节点逐一验证 —— AND 语义的正确性证明。"""
    for kind, nodes, prereqs in (
        ("tech", tp._tech_nodes(), tp._tech_prereqs()),
        ("civic", tp._civic_nodes(), tp._civic_prereqs()),
    ):
        for target in nodes:
            path = tp.prereq_closure(target, set(), kind)
            seen: set[str] = set()
            for node in path:
                assert all(p in seen for p in prereqs.get(node, ())), (
                    f"{kind}:{target} 的闭包里 {node} 的前置未先出现"
                )
                seen.add(node)


def test_closure_excludes_owned_and_handles_unknown():
    have = {"TECH_MINING"}
    path = tp.prereq_closure("TECH_IRON_WORKING", have)
    assert "TECH_MINING" not in path
    assert tp.prereq_closure("TECH_BOGUS", set()) == []
    assert tp.prereq_closure("TECH_MINING", {"TECH_MINING"}) == []


# --- 代价与回合 ---


def test_path_cost_uses_real_costs_from_json():
    path = tp.prereq_closure("TECH_IRON_WORKING", set())
    # 采矿 25 + 铸铜 80 + 铁器 —— 至少验证求和使用了导出 Cost
    assert tp.path_cost(path) == sum(tp.node_cost(n) for n in path)
    assert tp.node_cost("TECH_MINING") == 25


def test_discount_reduces_cost():
    path = tp.prereq_closure("TECH_IRON_WORKING", set())
    full = tp.path_cost(path)
    disc = tp.path_cost(path, {"TECH_BRONZE_WORKING": 0.6})
    assert disc == full - tp.node_cost("TECH_BRONZE_WORKING") * 0.4


def test_estimate_turns():
    assert tp.estimate_turns(100, 10) == 10
    assert tp.estimate_turns(101, 10) == 11
    assert tp.estimate_turns(5, 10) == 1  # 最少 1 回合
    assert tp.estimate_turns(100, 0) == 0  # 产出未知 → 无法估算


# --- 解锁反查 ---


def test_unlock_count_from_gamedata():
    assert tp.unlock_count("TECH_IRON_WORKING") >= 1  # 至少解锁剑客
    assert "剑客" in tp.unlock_summary("TECH_IRON_WORKING")
    assert tp.unlock_count("TECH_BOGUS") == 0


# --- 胜利路线目标(全部动态反查) ---


def test_space_chain_derived_from_projects():
    chain = tp._space_chain_techs()
    assert len(chain) == 5
    assert chain[0] == "TECH_ROCKETRY"  # Cost 最低 = 最早
    assert all(t in tp._tech_nodes() for t in chain)


def test_yield_district_mapping():
    assert tp._district_prereq_for_yield("YIELD_SCIENCE", "tech") == "TECH_WRITING"
    assert tp._district_prereq_for_yield("YIELD_FAITH", "tech") == "TECH_ASTROLOGY"
    civ = tp._district_prereq_for_yield("YIELD_CULTURE", "civic")
    assert civ in tp._civic_nodes()


def test_domination_targets_exclude_unique_units():
    """狂暴武士(挪威独有)不能出现在不知道玩家文明的推荐里。"""
    targets = tp._domination_target_techs({"TECH_POTTERY"})
    names = [name for _, name, _ in targets]
    assert names, "统治候选不该为空"
    uniques = tp._unique_units()
    for r in tp._rows("Units"):
        if r.get("UnitType") in uniques:
            assert (r.get("Name") or "") not in names


# --- 建议输出 ---


def test_science_advice_prioritises_campus_early():
    out = tp.get_tech_advice({"TECH_POTTERY"}, 8, "SCIENCE", _GS, set())
    assert out[0] == "== 科技路径 =="
    assert "科研区域" in out[1]
    assert any("太空链锚点" in line for line in out)  # 远期只给一行摘要


def test_science_advice_expands_space_chain_when_close():
    have = set(tp._tech_nodes()) - {"TECH_ROCKETRY"}  # 只差火箭研究
    out = tp.get_tech_advice(have, 50, "SCIENCE", _GS, set())
    assert any("太空链下一关键" in line for line in out)


def test_domination_advice_lists_two_candidates():
    out = tp.get_tech_advice(
        {"TECH_POTTERY", "TECH_ANIMAL_HUSBANDRY"}, 8, "DOMINATION", _GS, set()
    )
    assert sum(1 for line in out if line.startswith("· 军事升级")) == 2


def test_civic_advice_targets_next_government():
    out = tp.get_civic_advice({"CIVIC_CODE_OF_LAWS"}, 4, "", _GS, set())
    assert any("下一个政体" in line for line in out)


def test_culture_civic_advice_adds_theater_line():
    out = tp.get_civic_advice({"CIVIC_CODE_OF_LAWS"}, 4, "CULTURE", _GS, set())
    assert any("文化区域" in line for line in out)


def test_default_advice_ranks_by_unlock_count():
    out = tp.get_tech_advice(set(), 5)
    assert any("前沿" in line for line in out)


def test_standard_speed_note_present():
    out = tp.get_tech_advice({"TECH_POTTERY"}, 8, "SCIENCE", _GS, set())
    assert any("标准速度" in line for line in out)


def test_everything_researched_is_silent():
    assert tp.get_tech_advice(set(tp._tech_nodes()), 100, "SCIENCE") == []


def test_no_exceptions_on_garbage():
    assert tp.get_tech_advice(None, 0) is not None
    assert tp.get_civic_advice(None, -5, "BOGUS", {"bad": object()}) is not None
    assert tp.prereq_closure("", set()) == []
