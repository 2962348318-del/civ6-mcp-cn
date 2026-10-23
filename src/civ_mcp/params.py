"""难度 / 地图参数表 —— 方案 §8 一直是空表,这里填上。

⚠️ 先说清楚数据来源和可信度,因为这一层和其它模块不同:

    游戏数据库里【没有】难度加成表。实测(2026-07-31)确认 RULESET_STANDARD
    下 DifficultyBonuses / HandicapInfos / MapSizes 三张表都不存在 ——
    难度加成是通过 Modifier 系统挂载的,反查成本很高且易错。

    所以下面的数值分两类,标注清楚:
      [导出]  来自游戏库,确定无误
      [经验]  社区共识 + 需要实战校准,**不要当成精确值**

    经验值的用途是给模型一个【量级参照】("T50 该有 4 城而不是 1 城"),
    而不是精确指标。宁可给粗略的正确方向,也不要给精确的错误数字。
"""

from __future__ import annotations

from dataclasses import dataclass

# [导出] docs/gamedata/Difficulties.md — 8 档,顺序即强度
DIFFICULTY_ORDER = [
    "DIFFICULTY_SETTLER",  # 开拓者
    "DIFFICULTY_CHIEFTAIN",  # 首领
    "DIFFICULTY_WARLORD",  # 军阀  ← 失败复盘那局
    "DIFFICULTY_PRINCE",  # 王子  ← 基准难度,AI 无加成
    "DIFFICULTY_KING",  # 国王
    "DIFFICULTY_EMPEROR",  # 君主
    "DIFFICULTY_IMMORTAL",  # 不朽
    "DIFFICULTY_DEITY",  # 神
]

DIFFICULTY_CN = {
    "DIFFICULTY_SETTLER": "开拓者",
    "DIFFICULTY_CHIEFTAIN": "首领",
    "DIFFICULTY_WARLORD": "军阀",
    "DIFFICULTY_PRINCE": "王子",
    "DIFFICULTY_KING": "国王",
    "DIFFICULTY_EMPEROR": "君主",
    "DIFFICULTY_IMMORTAL": "不朽",
    "DIFFICULTY_DEITY": "神",
}


@dataclass(frozen=True)
class DifficultyParams:
    """某个难度下的行为阈值。数值均为 [经验] 值,需实战校准。"""

    # 到 T50 时应有的城市数下限。低于这个数说明扩张已经失败。
    cities_by_t50: int
    # 到 T100 时应有的城市数下限
    cities_by_t100: int
    # 首个军事单位最晚出现的回合。越高难度蛮族越凶,越要早出兵。
    first_military_by: int
    # 每城最少驻军
    garrison_per_city: int
    # 蛮族威胁系数(1.0 = 王子基准)。影响威胁等级判定的宽严。
    barb_factor: float


# [经验] 这张表是量级参照,不是精确指标。
_DIFFICULTY_PARAMS: dict[str, DifficultyParams] = {
    "DIFFICULTY_SETTLER": DifficultyParams(3, 6, 20, 1, 0.5),
    "DIFFICULTY_CHIEFTAIN": DifficultyParams(3, 6, 18, 1, 0.7),
    "DIFFICULTY_WARLORD": DifficultyParams(4, 7, 15, 1, 0.85),
    "DIFFICULTY_PRINCE": DifficultyParams(4, 8, 15, 1, 1.0),
    "DIFFICULTY_KING": DifficultyParams(4, 8, 12, 1, 1.15),
    "DIFFICULTY_EMPEROR": DifficultyParams(5, 9, 12, 2, 1.3),
    "DIFFICULTY_IMMORTAL": DifficultyParams(5, 10, 10, 2, 1.5),
    "DIFFICULTY_DEITY": DifficultyParams(6, 11, 8, 2, 1.8),
}

DEFAULT_DIFFICULTY = "DIFFICULTY_PRINCE"


def difficulty_params(difficulty: str | None) -> DifficultyParams:
    """取难度参数。未知难度回退到王子(基准档)。"""
    if not difficulty:
        return _DIFFICULTY_PARAMS[DEFAULT_DIFFICULTY]
    return _DIFFICULTY_PARAMS.get(
        difficulty.upper(), _DIFFICULTY_PARAMS[DEFAULT_DIFFICULTY]
    )


# ---------------------------------------------------------------------------
# 地图类型参数
# ---------------------------------------------------------------------------
# [经验] 地图脚本名在 GameConfiguration 里读不到(实测 GetValue 返回 nil),
# 所以这张表目前只能由模型显式告知,或从陆海比例推断。


@dataclass(frozen=True)
class MapParams:
    naval_priority: str  # "高" / "中" / "低"
    note: str


_MAP_PARAMS: dict[str, MapParams] = {
    "PANGAEA": MapParams(
        "低",
        "泛大陆:所有文明同一块陆地,早期就会遇到邻居,边境摩擦多。"
        "海军几乎无用,陆军和城防优先。",
    ),
    "CONTINENTS": MapParams(
        "中",
        "大陆:同大陆邻居是主要威胁,跨洋对手中后期才接触。"
        "需要一定海军做侦察和跨洲殖民。",
    ),
    "ARCHIPELAGO": MapParams(
        "高",
        "群岛:陆地破碎,海军是主力。港口和造船科技优先级极高,纯陆军打法会寸步难行。",
    ),
    "ISLAND_PLATES": MapParams(
        "高",
        "岛屿板块:比群岛更破碎。早期几乎不会遇到敌国,可以安心发展;但扩张必须靠海路。",
    ),
    "INLAND_SEA": MapParams(
        "中", "内海:中央有海,沿岸城市能靠港口和商业中心吃到高加成。"
    ),
    "FRACTAL": MapParams("中", "分裂大陆:形态随机,先探图再定策略。"),
}

DEFAULT_MAP = "CONTINENTS"


def map_params(map_type: str | None) -> MapParams:
    if not map_type:
        return _MAP_PARAMS[DEFAULT_MAP]
    return _MAP_PARAMS.get(map_type.upper(), _MAP_PARAMS[DEFAULT_MAP])


# ---------------------------------------------------------------------------
# 基于参数的确定性检查
# ---------------------------------------------------------------------------


def check_pace(
    turn: int,
    cities: int,
    military: int,
    difficulty: str | None = None,
    rival_city_median: int | None = None,
) -> list[str]:
    """校验发展节奏。返回中文警告列表。

    这一条治的是复盘里"T52 还只有 1 座城"却没有任何机制提醒的问题。

    ⚠️ 2026-08-03 改动:城市数一项**优先用实测对手中位数**,
    只有拿不到实测数据时才回落到下面的经验表。

    原因是 2026-08-02 校准局把按难度名硬编码的阈值证伪了:
    实测 T56 才 3 城、T100 只 5 城,却是全场军力第一并攻下一座首都;
    而 T111 实测对手中位数只有 4 城,经验表却仍在喊"国王难度应有 8 座"。
    两条结论同时出现在一份简报里,只会让读的人不知道该信哪个。
    难度还可能是随机的 —— 按难度名下的结论没有预测力。
    """
    p = difficulty_params(difficulty)
    cn = DIFFICULTY_CN.get((difficulty or "").upper(), "王子")
    out: list[str] = []

    if turn >= p.first_military_by and military == 0:
        out.append(
            f"T{turn} 仍无军事单位({cn}难度应在 T{p.first_military_by} 前出第一个)"
            f" —— 无兵等于把平民和城市白送。"
        )

    if rival_city_median is not None:
        # 实测口径:只有落后于对手中位数才算落后,而且要说清楚这是实测
        if cities < rival_city_median:
            out.append(
                f"T{turn} 你 {cities} 座城,实测对手中位数 {rival_city_median} 座"
                f" —— 城市数是所有产出的乘数。(注意:城市数不单独作为成败判据,"
                f"它只是局势测量七个维度里的一个)"
            )
    elif turn >= 50 and cities < p.cities_by_t50:
        out.append(
            f"T{turn} 只有 {cities} 座城,{cn}难度这个阶段的经验值是 "
            f"{p.cities_by_t50} 座〔经验值,非实测〕 —— 城市数是所有产出的乘数,"
            f"落后了很难靠单点优化补回来。"
        )
    elif rival_city_median is None and turn >= 100 and cities < p.cities_by_t100:
        out.append(
            f"T{turn} 只有 {cities} 座城,{cn}难度这个阶段的经验值是 "
            f"{p.cities_by_t100} 座〔经验值,非实测〕。"
        )

    if cities > 0 and military < cities * p.garrison_per_city:
        out.append(
            f"军事单位 {military} 个 / 城市 {cities} 座 —— "
            f"{cn}难度建议每城至少 {p.garrison_per_city} 个驻军。"
        )
    return out
