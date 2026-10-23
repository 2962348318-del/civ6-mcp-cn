"""Wiring pass(2026-08-01)—— 专家模块接线的回归测试。

这层测的不是各模块的业务逻辑(各自的测试文件管),而是**接线本身**:
数据映射对不对、相位对不对、降级路径通不通。接线是最容易被后续改动
悄悄破坏的层 —— 模块改个输出格式,简报就静默少一段。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from civ_mcp import advice as advice_mod
from civ_mcp import end_turn as et
from civ_mcp import world_model as wm_mod


class FakeGS:
    """最小可用的 GameState 假体。字段名与 lua/models.py 对齐。"""

    def __init__(self):
        self._expert_cache = {
            "overview": NS(
                total_population=6,
                our_religion=None,
                gold=300,
                gold_per_turn=8,
                player_id=0,
                faith=50,
            ),
            "trade_cap": (1, 0),
        }

    async def get_tech_civics(self):
        return NS(
            available_techs=[NS(tech_type="TECH_WRITING", boosted=True)],
            available_civics=[NS(civic_type="CIVIC_MYSTICISM", boosted=False)],
            locked_techs=[],
            locked_civics=[],
        )

    async def get_governors(self):
        return NS(
            points_available=1,
            appointed=[],
            available_to_appoint=[NS(governor_type="GOVERNOR_PINGALA")],
        )

    async def get_city_states(self):
        return NS(
            tokens_available=2,
            city_states=[
                NS(
                    player_id=20,
                    name="日内瓦",
                    city_state_type="Scientific",
                    envoys_sent=1,
                    suzerain_id=-1,
                    suzerain_name="None",
                    can_send_envoy=True,
                )
            ],
        )

    async def get_victory_progress(self):
        return NS(
            players=list(range(8)),
            religion_majority={"西班牙": 6},
            space_projects={},
            capitals_held={},
        )

    async def get_great_people(self):
        return [
            NS(
                individual_name="希帕蒂娅",
                cost=60,
                player_points=20,
                gold_cost=600,
                faith_cost=0,
                can_recruit=False,
                claimant="Unclaimed",
                ability="学院+1科技",
            )
        ]

    async def get_spies(self):
        return []

    async def get_trade_routes(self):
        return NS(capacity=1, active_count=0, traders=[7], ghost_count=0)

    async def get_trade_destinations(self, uid):
        return [
            NS(
                city_name="南特",
                owner_name="我",
                x=5,
                y=5,
                is_domestic=True,
                is_city_state=False,
                has_quest=False,
                has_trading_post=False,
                origin_yields="Food:3 Prod:2",
                dest_yields="",
                pressure_out=0,
                religion_out="",
            )
        ]


def _fixtures(victory="SCIENCE", civics=("CIVIC_CODE_OF_LAWS",)):
    wm = wm_mod.WorldModel()
    wm.victory_type = victory
    wm.military_count, wm.city_count = 3, 2
    scan = NS(
        techs={"TECH_POTTERY"},
        civics=set(civics),
        science=8,
        culture=4,
        cities=2,
        military=3,
        units={},
    )
    cities = [
        NS(
            name="罗马",
            x=10,
            y=10,
            districts=["DISTRICT_CAMPUS"],
            loyalty=100,
            loyalty_per_turn=2,
            currently_building="UNIT_SETTLER",
            population=4,
        )
    ]
    return wm, scan, cities


def _gather(
    turn,
    era="ERA_CLASSICAL",
    wm=None,
    scan=None,
    cities=None,
    gs=None,
    reads=(),
    lost=(),
    events=(),
):
    _wm, _scan, _cities = _fixtures()
    return asyncio.run(
        et._gather_expert_advice(
            gs or FakeGS(),
            wm or _wm,
            scan or _scan,
            turn,
            era,
            list(reads),
            cities if cities is not None else _cities,
            None,
            list(lost),
            list(events),
        )
    )


def _modules(items):
    return {a.module for a in items}


# --- 每回合组 ---


def test_tech_and_boost_wired_every_turn():
    mods = _modules(_gather(25))
    assert "tech_path" in mods and "boost" in mods


def test_empty_civics_suppresses_civic_and_boost_groups():
    """civics 扫描失败(空集)→ 市政/尤里卡组缺席 —— 空集会把已完成市政
    当未研究来推荐,宁缺勿错。"""
    wm, scan, cities = _fixtures(civics=())
    items = _gather(25, wm=wm, scan=scan, cities=cities)
    assert "boost" not in _modules(items)
    assert "tech_path" in _modules(items)  # 科技侧不受影响


def test_loyalty_crisis_fires_from_hoisted_cities():
    wm, scan, _ = _fixtures()
    sick = [
        NS(
            name="危城",
            x=9,
            y=9,
            districts=[],
            loyalty=60,
            loyalty_per_turn=-7,
            currently_building="",
            population=3,
        )
    ]
    items = _gather(25, wm=wm, scan=scan, cities=sick)
    crisis = [a for a in items if a.module == "crisis"]
    assert crisis and crisis[0].priority is advice_mod.Priority.SURVIVAL
    assert "叛变" in crisis[0].action


def test_civilian_death_crisis_from_diff_units():
    items = _gather(29, lost=[("UNIT_BUILDER", 12, 34)])
    crisis = [a for a in items if a.module == "crisis"]
    assert any("被杀" in a.action for a in crisis)


def test_joint_war_uses_opponent_reads():
    reads = [
        NS(
            name="英国",
            their_military=300,
            war_risk="高",
            at_war=False,
            agendas=[],
            player_id=1,
        ),
        NS(
            name="蒙古",
            their_military=280,
            war_risk="高",
            at_war=False,
            agendas=[],
            player_id=2,
        ),
    ]
    items = _gather(25, reads=reads)
    assert "diplomacy" in _modules(items)


# --- 相位错开的周期组 ---


def test_phase_stagger():
    """相位全部 mod 10 且互斥 —— mod 5 会撞相(T37%5==2 抢走伟人相位)。"""
    assert "governor_advisor" in _modules(_gather(26))  # T%10==6
    assert "city_state_advisor" in _modules(_gather(32))  # T%10==2
    assert "victory_block" in _modules(_gather(33))  # T%10==3, ≥30
    assert "great_person" in _modules(_gather(37))  # T%10==7
    assert "governor_advisor" not in _modules(_gather(25))
    assert "victory_block" not in _modules(_gather(13))  # <T30 不查


def test_victory_block_priority_is_blocking():
    items = [a for a in _gather(33) if a.module == "victory_block"]
    assert items
    assert any(a.priority is advice_mod.Priority.BLOCKING for a in items)


def test_great_person_offer_mapping():
    items = [a for a in _gather(37) if a.module == "great_person"]
    assert any("希帕蒂娅" in a.action for a in items)


def test_trade_wired_conditionally():
    items = [a for a in _gather(24) if a.module == "trade_optimizer"]
    assert items and "南特" in items[0].action
    # 无空闲容量 → 不查
    gs = FakeGS()
    gs._expert_cache["trade_cap"] = (1, 1)
    items = [a for a in _gather(24, gs=gs) if a.module == "trade_optimizer"]
    assert items == []


# --- 降级与安全 ---


def test_all_sources_failing_returns_empty_not_raise():
    class DeadGS:
        _expert_cache = {}

        def __getattr__(self, name):
            async def boom(*a, **k):
                raise RuntimeError("dead")

            return boom

    wm, scan, cities = _fixtures()
    items = asyncio.run(
        et._gather_expert_advice(
            DeadGS(), wm, scan, 26, "ERA_CLASSICAL", [], cities, None, [], []
        )
    )
    # 周期查询失败缺席;纯计算组(tech_path 等)仍在
    assert isinstance(items, list)
    assert "governor_advisor" not in _modules(items)


# --- 扫描扩展(civics/culture/units 行) ---


def test_world_scan_parses_new_lines():
    scan = wm_mod.parse_world_scan(
        [
            "EXPLORE|100|3404",
            "STATS|2|3",
            "YIELD|14|9|false",
            "TECH|TECH_POTTERY",
            "CIVIC|CIVIC_CODE_OF_LAWS",
            "CULTURE|7",
            "UNIT|65537|UNIT_WARRIOR|10|12|1",
            "UNIT|65538|UNIT_BUILDER|11|12|0",
        ]
    )
    assert scan.civics == {"CIVIC_CODE_OF_LAWS"}
    assert scan.culture == 7
    assert scan.units["65537"] == "UNIT_WARRIOR|10|12|1"
    assert scan.units["65538"].endswith("|0")


def test_scan_query_contains_new_sections():
    q = wm_mod.build_world_scan_query()
    assert "CIVIC|" in q and "CULTURE|" in q and "UNIT|" in q
    assert "HasCivic" in q


def test_death_detection_runs_before_save():
    """2026-08-01 实机 bug:diff_units 排在 wm.save() 之后,快照永远落不了盘,
    于是每回合都是"首次初始化",死亡永远检测不到(建造者阵亡而 death_tiles
    仍为空)。这条锁死执行顺序。"""
    import inspect

    src = inspect.getsource(et._update_world_model)
    assert src.index("wm.diff_units") < src.index("wm.save(path)")


def test_diff_units_persists_snapshot_then_detects():
    """两回合序列:首轮建快照,次轮真正报出阵亡单位。"""
    wm = wm_mod.WorldModel()
    t31 = {
        "65537": "UNIT_WARRIOR|10|12|1",
        "65538": "UNIT_BUILDER|11|12|0",
        "65539": "UNIT_SCOUT|9|9|1",
    }
    assert wm.diff_units(t31, 31) == []  # 首轮只初始化,不误报
    assert len(wm.last_units) == 3  # 快照已就绪(此刻才可 save)
    t32 = {"65538": "UNIT_BUILDER|11|12|0"}  # 勇士与侦察兵阵亡
    lost = wm.diff_units(t32, 32)
    assert sorted(u for u, _x, _y in lost) == ["UNIT_SCOUT", "UNIT_WARRIOR"]
    assert "10,12" in wm.death_tiles and "9,9" in wm.death_tiles
    # 平民消失不计阵亡(可能是建城/耗尽充能)
    lost2 = wm.diff_units({}, 33)
    assert lost2 == []


# --- advice 注册表与段头变体 ---


def test_new_legacy_registry_entries():
    reg = advice_mod.LEGACY_REGISTRY
    assert reg["victory_block"].red is advice_mod.Priority.BLOCKING
    assert reg["crisis"].red is advice_mod.Priority.SURVIVAL
    assert reg["tech_path"].warn is advice_mod.Priority.CORE
    for key in ("boost", "diplomacy", "ranking", "district", "culture_endgame"):
        assert key in reg


def test_header_with_suffix_dropped():
    """「== 城邦 == 优先序(SCIENCE):…」这类带尾注的段头也要丢。"""
    items = advice_mod.from_legacy(
        "city_state_advisor",
        ["== 城邦 == 优先序(SCIENCE):Scientific > Trade", "💡 投使者"],
    )
    assert len(items) == 1 and items[0].action == "投使者"


# --- server 工具的组装辅助 ---


def test_tiles_as_dicts_field_mapping():
    from civ_mcp.server import _tiles_as_dicts

    tiles = [
        NS(
            x=1,
            y=2,
            terrain="TERRAIN_GRASS",
            feature="",
            resource="",
            resource_class="",
            is_hills=False,
            is_river=True,
            is_coastal=False,
            improvement="IMPROVEMENT_MINE",
            district="",
            is_pillaged=False,
        )
    ]
    d = _tiles_as_dicts(tiles)[0]
    assert d["is_river"] and d["improvement"] == "IMPROVEMENT_MINE"


def test_war_game_state_assembly():
    from civ_mcp.server import _war_game_state

    class G:
        _expert_cache = {"techs": {"TECH_MASONRY"}}

        async def get_units(self):
            return [NS(unit_type="UNIT_WARRIOR", x=1, y=1)]

        async def get_threat_scan(self):
            return [
                NS(
                    unit_type="UNIT_ARCHER",
                    x=11,
                    y=10,
                    combat_strength=15,
                    ranged_strength=25,
                ),
                NS(
                    unit_type="UNIT_FAR",
                    x=99,
                    y=99,
                    combat_strength=50,
                    ranged_strength=0,
                ),
            ]

        async def get_cities(self):
            return [NS(x=2, y=2)], []

    state = asyncio.run(_war_game_state(G(), 10, 10, {"name": "伦敦"}))
    assert state["my_techs"] == {"TECH_MASONRY"}
    assert len(state["enemy_units"]) == 1  # 5 格外的被过滤
    assert state["enemy_units"][0]["cs"] == 25  # 取远程/近战较大值


def test_last_units_survives_roundtrip(tmp_path):
    """2026-08-01 实机 bug 第二层:last_units 未被序列化,快照跨回合丢失,
    死亡检测永远停在"首次初始化"。这条锁死持久化。"""
    wm = wm_mod.WorldModel(game_id="g")
    wm.diff_units({"65537": "UNIT_WARRIOR|10|12|1"}, 31)  # 首轮建快照
    p = tmp_path / "w.json"
    wm.save(p)
    back = wm_mod.WorldModel.load(p)
    assert back.last_units == {"65537": "UNIT_WARRIOR|10|12|1"}
    # 跨"进程"重新加载后,次轮差分能真正报出阵亡
    lost = back.diff_units({}, 32)
    assert lost == [("UNIT_WARRIOR", 10, 12)]
    assert "10,12" in back.death_tiles


def test_worldmodel_roundtrip_backward_compatible(tmp_path):
    """旧存档没有 last_units 字段 —— 加载不得报错。"""
    p = tmp_path / "old.json"
    p.write_text(
        '{"game_id":"old","current_turn":5,"camps":[],"plans":[]}', encoding="utf-8"
    )
    wm = wm_mod.WorldModel.load(p)
    assert wm.game_id == "old" and wm.last_units == {}


# ===========================================================================
# 2026-08-01 实机事故回归:换客户端启动 → 世界模型历史被清空
#
# 现象:Hermes 拉起的 MCP 进程无 CIV_MCP_WORKSPACE(写 ~/.civ6-mcp/),
# Claude Code 拉起的有(写工作区)。T33 那回合读到工作区的空文件,把
# 胜利路线/2 条计划/8 条失联/营地首见 T21 全部覆盖成空。
# 存储位置不该取决于"是谁启动的进程"。
# ===========================================================================


def test_candidate_paths_covers_both_locations(monkeypatch, tmp_path):
    monkeypatch.setenv("CIV_MCP_WORKSPACE", str(tmp_path / "ws"))
    paths = wm_mod.candidate_paths("arabia", 1)
    assert len(paths) == 2
    assert str(tmp_path / "ws") in str(paths[0])  # 工作区优先(写入位置)
    assert ".civ6-mcp" in str(paths[1])  # 用户目录兜底


def test_candidate_paths_dedupes_when_no_env(monkeypatch):
    monkeypatch.delenv("CIV_MCP_WORKSPACE", raising=False)
    assert len(wm_mod.candidate_paths("arabia", 1)) == 1


def test_richness_prefers_history_over_turn_number():
    """空模型每回合都会把 current_turn 写成最新值 —— 回合数不能压过历史。"""
    rich = wm_mod.WorldModel()
    rich.observe_camp(48, 20, turn=21)
    rich.victory_type = "SCIENCE"
    rich.current_turn = 32
    empty = wm_mod.WorldModel()
    empty.current_turn = 33  # 更新,但什么历史都没有
    assert rich.richness() > empty.richness()


def test_load_best_picks_the_richer_file(tmp_path, monkeypatch):
    """事故重演:工作区是刚被覆盖的空文件,用户目录是真实历史。"""
    ws_path = tmp_path / "ws" / "世界模型" / "arabia_1.json"
    home_path = tmp_path / "home" / "世界模型" / "arabia_1.json"

    empty = wm_mod.WorldModel()
    empty.current_turn = 33
    empty.save(ws_path)

    real = wm_mod.WorldModel()
    real.observe_camp(48, 20, turn=21)
    real.victory_type = "SCIENCE"
    real.current_turn = 32
    real.record_lost_contact("UNIT_SCOUT", "Barbarian", 50, 13, 28)
    real.save(home_path)

    monkeypatch.setattr(
        wm_mod, "candidate_paths", lambda civ, seed: [ws_path, home_path]
    )
    wm, src = wm_mod.WorldModel.load_best("arabia", 1)
    assert src == home_path, "应选中内容更完整的那份,而不是写入位置那份"
    assert wm.victory_type == "SCIENCE"
    assert wm.camps["48,20"].first_seen_turn == 21  # 首见回合没被重置
    assert len(wm.contacts) == 1


def test_load_best_handles_missing_and_empty(tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "a.json"
    monkeypatch.setattr(wm_mod, "candidate_paths", lambda civ, seed: [missing])
    wm, _src = wm_mod.WorldModel.load_best("arabia", 1)
    assert wm.camps == {} and wm.victory_type == ""  # 干净空模型,不抛异常


# ===========================================================================
# 2026-08-01 实机误报回归:升级被当成阵亡
#
# T34 把 (55,11) 的投石兵升级为弓箭手,升级会换 unit_id,旧 ID 从快照消失
# → 判成阵亡,简报报"UNIT_SLINGER 在 (55,11) 被杀"并把该格写进死亡格档案。
# 假警报比没警报更糟:它让模型绕开一块其实安全的地。
# ===========================================================================


def test_upgrade_is_not_counted_as_death():
    wm = wm_mod.WorldModel()
    wm.diff_units({"655364": "UNIT_SLINGER|55|11|1"}, turn=33)  # 建立快照
    # T34:升级换了 ID,新单位仍在同一格
    lost = wm.diff_units({"720900": "UNIT_ARCHER|55|11|1"}, turn=34)
    assert lost == [], "原地换 ID 是升级,不该记为阵亡"
    assert wm.death_tiles == {}, "死亡格档案不该被误报污染"


def test_real_death_still_detected():
    """真阵亡:那一格不再有我方单位。"""
    wm = wm_mod.WorldModel()
    wm.diff_units({"1": "UNIT_SCOUT|42|29|1", "2": "UNIT_WARRIOR|56|12|1"}, turn=28)
    lost = wm.diff_units({"2": "UNIT_WARRIOR|56|12|1"}, turn=29)
    assert lost == [("UNIT_SCOUT", 42, 29)]
    assert "42,29" in wm.death_tiles


def test_death_detected_even_if_other_unit_elsewhere():
    """别的格有单位不影响判定 —— 只看阵亡格本身。"""
    wm = wm_mod.WorldModel()
    wm.diff_units({"1": "UNIT_SCOUT|10|10|1"}, turn=5)
    lost = wm.diff_units({"9": "UNIT_WARRIOR|40|40|1"}, turn=6)
    assert lost == [("UNIT_SCOUT", 10, 10)]


def test_formation_merge_not_a_death():
    """编队合并同样是原地换 ID。"""
    wm = wm_mod.WorldModel()
    wm.diff_units({"1": "UNIT_WARRIOR|20|20|1", "2": "UNIT_WARRIOR|20|20|1"}, turn=50)
    lost = wm.diff_units({"3": "UNIT_WARRIOR|20|20|1"}, turn=51)
    assert lost == [] and wm.death_tiles == {}
