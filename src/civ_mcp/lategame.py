"""中后期决策 —— 方案里承认的最大短板。

复盘那局 52 回合就结束了,所以中后期完全没有实证。这一层的设计原则因此更保守:
只做【能用数字算出来的】判断,不做经验性建议。

覆盖四块:
    1. 胜利路线里程碑与产能瓶颈 —— 用真实项目成本算,不是拍脑袋
    2. 区域长期布局 —— 相邻加成是确定性规则,可以算最优
    3. 奇观竞速风险 —— 结合对手议程判断该不该抢
    4. 后期产能瓶颈 —— 最容易被忽略的隐性输法

数据来源:docs/gamedata/ 的 Projects / Districts / Adjacency_YieldChanges /
Buildings(IsWonder)。全部从游戏库导出,与本机规则集一致。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 胜利路线的硬成本 —— 来自 docs/gamedata/Projects.md
# ---------------------------------------------------------------------------
# 科学胜利必须跑完这条链,总产能需求是【确定的数字】,可以据此倒推还差多少。


@dataclass(frozen=True)
class Milestone:
    key: str
    name: str
    cost: int
    prereq_tech: str
    prereq_district: str = ""
    # 发射层级。引擎强制【层级之间】的顺序(不完成登月就开不了火星),
    # 但同层的三件火星项目互不构成前置。用于从"此刻在造第几层"反推
    # 前面几层必然已完成 —— 见 infer_completed_projects。
    tier: int = 0


SCIENCE_CHAIN: list[Milestone] = [
    Milestone(
        "PROJECT_LAUNCH_EARTH_SATELLITE",
        "发射地球卫星",
        900,
        "TECH_ROCKETRY",
        "DISTRICT_SPACEPORT",
        tier=1,
    ),
    Milestone(
        "PROJECT_LAUNCH_MOON_LANDING",
        "启动登月计划",
        1500,
        "TECH_SATELLITES",
        "DISTRICT_SPACEPORT",
        tier=2,
    ),
    Milestone(
        "PROJECT_LAUNCH_MARS_REACTOR",
        "发射火星反应堆",
        1800,
        "TECH_NUCLEAR_FUSION",
        "DISTRICT_SPACEPORT",
        tier=3,
    ),
    Milestone(
        "PROJECT_LAUNCH_MARS_HABITATION",
        "发射火星住所",
        1800,
        "TECH_ROBOTICS",
        "DISTRICT_SPACEPORT",
        tier=3,
    ),
    Milestone(
        "PROJECT_LAUNCH_MARS_HYDROPONICS",
        "发射火星溶液栽培",
        1800,
        "TECH_NANOTECHNOLOGY",
        "DISTRICT_SPACEPORT",
        tier=3,
    ),
]

SCIENCE_TOTAL_COST = sum(m.cost for m in SCIENCE_CHAIN)  # 7800


def infer_completed_projects(
    building_now: str = "",
    known: Iterable[str] = (),
) -> set[str]:
    """从「太空港城此刻在造哪个项目」反推已完成的项目。

    2026-08-12 缺陷110:WorldModel.completed_projects 有存盘、有读盘、有
    三个消费者,唯独【没有写入者】—— 恒为空列表。于是简报一直说"0/5 个
    项目已完成,还剩 7800 产能",而同一回合 get_victory_progress 说 2/5。
    7800 正好是五件的总和,也就是"一件没做"的数字。

    引擎强制层级之间的顺序:在造第 N 层,前面各层就必然全部完成。同层的
    三件火星项目互不构成前置,所以只能推到层,推不到具体是哪一件 ——
    这也是为什么结果要和已知集合取并集而不是覆盖:这个函数只会让集合
    单调增长,绝不减少。
    """
    done = {str(k) for k in known if k}
    key = (building_now or "").strip()
    if not key:
        return done
    current = next((m for m in SCIENCE_CHAIN if m.key == key), None)
    if current is None or current.tier <= 0:
        return done
    for m in SCIENCE_CHAIN:
        if 0 < m.tier < current.tier:
            done.add(m.key)
    return done


# 另外三条路线没有"项目链",但同样有确定的阶段序列。
# 每条给:胜利判据 / 阶段里程碑 / 最常见的输法。
# 这四条必须对等 —— 用户明确要求"不同的胜利条件怎么胜利都要会"。


@dataclass(frozen=True)
class VictoryPath:
    cn: str
    condition: str  # 精确判据
    stages: list[str]  # 阶段里程碑
    key_metric: str  # 每回合该盯的那个数
    common_loss: str  # 最常见的输法


VICTORY_PATHS: dict[str, VictoryPath] = {
    "SCIENCE": VictoryPath(
        "科学胜利",
        "依次完成 5 个太空船项目(卫星→登月→火星反应堆/住所/溶液栽培),"
        "全部在太空港内建造。",
        [
            "① 铺城 + 学院区打底,学院优先贴山脉(每座山 +1 科技)",
            "② 图书馆/大学/研究实验室补满,大科学家优先招募",
            "③ 关键科技:火箭技术 → 卫星 → 核聚变/机器人学/纳米技术",
            "④ 建太空港(DISTRICT_SPACEPORT),没它整条链跑不了",
            "⑤ 堆产能跑项目 —— 这一步卡的是产能不是科技",
        ],
        "科技/回合 与 总产能",
        "科技一路领先却输在产能:5 个项目共 7800 产能,"
        "30 产能/回合要跑 260 回合。后期必须转产能。",
    ),
    "CULTURE": VictoryPath(
        "文化胜利",
        "你的【外国游客数】超过任一对手的【国内游客数】。"
        "注意:比的是旅游业绩不是文化产出。",
        [
            "① 剧院广场 + 圆形剧场/艺术博物馆/考古博物馆",
            "② 伟大作品与文物填满槽位 —— 空槽不产旅游业绩",
            "③ 关键奇观:大金字塔/兵马俑之类能加伟人点的,以及后期直接产旅游的",
            "④ 国家公园、海滨度假区(需要保护区/自然奇观)",
            "⑤ 关键市政:飞行(+100% 旅游)、社交媒体",
        ],
        "旅游业绩/回合 与 对手的国内游客数",
        "只堆文化不堆旅游:文化产出高但没有伟大作品和奇观,游客数上不去,永远差最后一截。",
    ),
    "RELIGION": VictoryPath(
        "宗教胜利",
        "所有其它文明【超过半数的城市】以你的宗教为主流宗教。",
        [
            "① 抢万神殿 → 圣地区 → 神庙,信仰产出打底",
            "② 大预言家创立宗教 —— 抢不到就这条路直接断了,优先级最高",
            "③ 信仰选择:传教士/使徒相关的信条优先",
            "④ 传教士铺自己城,使徒出去传外国城",
            "⑤ 对方有护教者时要打神学战,使徒需要辩论晋升",
        ],
        "信仰/回合 与 各国已皈依城市比例",
        "大预言家被抢:创不了教这条路就没了。"
        "以及只传自己城不传外国城——判据看的是【其它文明】的城市。",
    ),
    "DOMINATION": VictoryPath(
        "统治胜利",
        "占领所有其它文明的【原始首都】(不是所有城市)。",
        [
            "① 早期锁定一个近邻作为第一目标,不要四面开战",
            "② 攻城单位代差是关键:撞车/投石机 → 弩炮 → 攻城塔",
            "③ 城墙出现后没有攻城单位硬啃 = 白送兵",
            "④ 打下首都后消化(忠诚度/宜居度),再打下一个",
            "⑤ 后勤:补给线、医疗、军事工程师修路",
        ],
        "军力对比 与 已占首都数",
        "四面开战被围殴,或者没有攻城单位去啃城墙。"
        "统治胜利拼的是代差和后勤,不是总兵力。",
    ),
}

# 兼容旧引用
VICTORY_NOTES: dict[str, str] = {k: v.condition for k, v in VICTORY_PATHS.items()}

# ---------------------------------------------------------------------------
# 区域相邻加成 —— 来自 Adjacency_YieldChanges.md,确定性规则
# ---------------------------------------------------------------------------
DISTRICT_ADJACENCY_HINTS: dict[str, str] = {
    "DISTRICT_CAMPUS": "学院:每座相邻山脉 +1 科技;每 2 格雨林 +1;每 2 个相邻区域 +1。"
    "山脉群旁边是学院的最佳位置。",
    "DISTRICT_HOLY_SITE": "圣地:每座相邻山脉 +1 信仰;相邻自然奇观 +2;每 2 格森林 +1;"
    "每 2 个相邻区域 +1。",
    "DISTRICT_COMMERCIAL_HUB": "商业中心:相邻河流 +2 金币;相邻港口 +2。沿河是最佳位置。",
    "DISTRICT_HARBOR": "港口:相邻城市中心 +1;给相邻商业中心 +2 金币。",
    "DISTRICT_INDUSTRIAL_ZONE": "工业区:相邻矿山/采石场加成。放在资源密集区。",
    "DISTRICT_THEATER": "剧院广场:相邻奇观加成。挨着已建奇观放。",
    "DISTRICT_ENCAMPMENT": "军营:不能贴城市中心。放在城市外围朝威胁方向,可当前哨。",
}


@dataclass
class LateGameContext:
    """中后期判断所需的事实,全部由服务端查询提供。"""

    turn: int = 0
    era: str = ""
    victory_type: str = ""
    # 产能
    total_production: int = 0  # 全帝国每回合产能
    # 太空港城(或产能最高城)的单城产能。太空项目是单城建造 ——
    # 用全帝国产能当 ETA 分母会把 280 回合说成 50 回合(2026-08-09 T276)。
    # 0 = 调用方没提供,退回旧口径(帝国总产能)并在文案里标明不可靠。
    project_city_production: int = 0
    city_count: int = 0
    # 科学胜利进度
    has_spaceport: bool = False
    completed_projects: set[str] = field(default_factory=set)
    techs: set[str] = field(default_factory=set)
    # 竞争
    rival_max_cities: int = 0
    rival_max_science: int = 0
    my_science: int = 0
    wonder_obsessed_rivals: list[str] = field(default_factory=list)
    # 区域
    districts_built: dict[str, int] = field(default_factory=dict)


@dataclass
class LateGameAdvice:
    severity: str  # "warn" / "hint"
    text: str


def _remaining_science_cost(ctx: LateGameContext) -> int:
    return sum(m.cost for m in SCIENCE_CHAIN if m.key not in ctx.completed_projects)


def my_eta_turns(ctx: LateGameContext) -> int | None:
    """我方当前路线还要几回合完成。算不出返回 None(不猜)。

    只有科学胜利能硬算 —— 它的分母是确定的产能总量(SCIENCE_CHAIN),
    与 check_lategame 里那段用的是同一个源,不重复实现。
    文化/外交/宗教的剩余量不是一个确定的产能数(取决于对手的国内游客、
    城邦归属、皈依速度),给不出诚实的 ETA 就不给。
    """
    if ctx.victory_type != "SCIENCE":
        return None
    remaining = _remaining_science_cost(ctx)
    if remaining <= 0:
        return 0
    # 单城产能优先:项目在太空港城逐个排队,全帝国产能摊不进去。
    per_turn = ctx.project_city_production or ctx.total_production
    if per_turn <= 0:
        return None
    return int(round(remaining / per_turn))


def check_lategame(ctx: LateGameContext) -> list[LateGameAdvice]:
    """中后期确定性检查。只在真的进入中后期时产出内容。"""
    out: list[LateGameAdvice] = []

    late_eras = {
        "ERA_INDUSTRIAL",
        "ERA_MODERN",
        "ERA_ATOMIC",
        "ERA_INFORMATION",
        "ERA_FUTURE",
    }
    mid_eras = {"ERA_MEDIEVAL", "ERA_RENAISSANCE"}
    is_late = ctx.era in late_eras
    is_mid = ctx.era in mid_eras or is_late

    # ---------------- 科学胜利:产能瓶颈是硬算的 ----------------
    if ctx.victory_type == "SCIENCE" and is_mid:
        remaining = _remaining_science_cost(ctx)
        per_turn = ctx.project_city_production or ctx.total_production
        if remaining > 0 and per_turn > 0:
            turns_needed = remaining / max(1, per_turn)
            done = len(ctx.completed_projects & {m.key for m in SCIENCE_CHAIN})
            # 2026-08-09 T276:分母必须是【太空港城】的单城产能 —— 项目在
            # 一座城里逐个排队,帝国总产能摊不进去。旧算式把 280 回合说成
            # 50 回合,整段冲刺规划跟着错。
            denom_label = (
                f"按太空港城产能 {per_turn}/回合(单城建造,不是帝国总产能"
                f" {ctx.total_production})"
                if ctx.project_city_production
                else f"按帝国产能 {ctx.total_production}/回合(⚠️ 高估:项目是"
                f"单城建造,拿不到单城产能时此数只是下界)"
            )
            out.append(
                LateGameAdvice(
                    "warn" if turns_needed > 120 else "hint",
                    f"科学胜利链还剩 {remaining} 产能({done}/{len(SCIENCE_CHAIN)} 个项目已完成)。"
                    f"{denom_label},还需约 "
                    f"{turns_needed:.0f} 回合 —— "
                    + (
                        "**这个速度赢不了,必须先堆产能**(工业区、矿山、发电厂、"
                        "伟人注入),光堆科技没用:项目卡的是产能不是科技。"
                        if turns_needed > 120
                        else "进度合理,保持产能投入。"
                    ),
                )
            )
        if not ctx.has_spaceport and is_late:
            out.append(
                LateGameAdvice(
                    "warn",
                    "锁定科学胜利但还没有太空港(DISTRICT_SPACEPORT) —— "
                    "所有太空船项目都必须在太空港里造,没有它整条链跑不了。",
                )
            )
        # 下一个里程碑缺什么
        for m in SCIENCE_CHAIN:
            if m.key in ctx.completed_projects:
                continue
            if m.prereq_tech and m.prereq_tech not in ctx.techs:
                out.append(
                    LateGameAdvice(
                        "hint",
                        f"下一个里程碑「{m.name}」({m.cost} 产能)需要 "
                        f"{m.prereq_tech},尚未研究。",
                    )
                )
            break

    # ---------------- 非科学路线:给判据 + 当前阶段该做什么 ----------------
    if ctx.victory_type in VICTORY_PATHS and ctx.victory_type != "SCIENCE" and is_mid:
        p = VICTORY_PATHS[ctx.victory_type]
        out.append(
            LateGameAdvice(
                "hint", f"{p.cn}判据:{p.condition} 该盯的数:{p.key_metric}。"
            )
        )
        # 按时代给对应阶段。中世纪/文艺复兴看前段,工业以后看后段。
        idx = 1 if ctx.era in mid_eras else 3
        stage = p.stages[min(idx, len(p.stages) - 1)]
        out.append(LateGameAdvice("hint", f"当前阶段重点:{stage}"))
        out.append(LateGameAdvice("hint", f"⚠️ 这条路最常见的输法:{p.common_loss}"))

    # ---------------- 后期产能瓶颈(通用) ----------------
    if is_late and ctx.city_count > 0:
        per_city = ctx.total_production / ctx.city_count
        if per_city < 15:
            out.append(
                LateGameAdvice(
                    "warn",
                    f"平均每城仅 {per_city:.0f} 产能 —— 后期任何项目都要几十回合。"
                    f"优先补工业区+工厂、开发矿山、接入战略资源;"
                    f"产能不足是后期最常见的隐性输法。",
                )
            )

    # ---------------- 扩张落后 ----------------
    if is_mid and ctx.rival_max_cities > 0 and ctx.city_count > 0:
        if ctx.rival_max_cities >= ctx.city_count * 2:
            out.append(
                LateGameAdvice(
                    "warn",
                    f"最强对手有 {ctx.rival_max_cities} 城,你只有 {ctx.city_count} 城。"
                    f"城市数是所有产出的乘数 —— 差距 2 倍以上时,"
                    f"任何单点优化都补不回来,要么抢地要么抢城。",
                )
            )

    # ---------------- 科技竞速 ----------------
    if (
        ctx.victory_type == "SCIENCE"
        and ctx.rival_max_science > 0
        and ctx.my_science > 0
    ):
        if ctx.rival_max_science > ctx.my_science * 1.3:
            out.append(
                LateGameAdvice(
                    "warn",
                    f"对手科技 {ctx.rival_max_science}/回合 vs 你 {ctx.my_science} —— "
                    f"你走科学胜利却在科技上落后 30% 以上,这条路线正在失效。",
                )
            )

    # ---------------- 奇观竞速风险 ----------------
    if ctx.wonder_obsessed_rivals and is_mid:
        names = "、".join(ctx.wonder_obsessed_rivals[:2])
        out.append(
            LateGameAdvice(
                "hint",
                f"{names} 的议程是抢奇观 —— 和它抢同一个奇观风险很高:"
                f"被抢走的产能会全额退成金币,但回合数是白扔的。"
                f"要抢就用产能高的城市,或者干脆让给它。",
            )
        )

    out.sort(key=lambda a: 0 if a.severity == "warn" else 1)
    return out


def district_hint(district_type: str) -> str:
    """给某个区域的放置要点。用于 set_city_production 的区域分支。"""
    return DISTRICT_ADJACENCY_HINTS.get(district_type, "")


def victory_playbook(victory_type: str) -> str:
    """某条胜利路线的完整打法。锁定路线时返回给模型,让它一次看全。"""
    p = VICTORY_PATHS.get((victory_type or "").upper())
    if not p:
        return ""
    lines = [
        f"【{p.cn}】",
        f"判据:{p.condition}",
        f"该盯的数:{p.key_metric}",
        "阶段:",
    ]
    lines.extend(f"  {s}" for s in p.stages)
    lines.append(f"⚠️ 最常见的输法:{p.common_loss}")
    return "\n".join(lines)


def render_lategame(advice: list[LateGameAdvice], max_items: int = 3) -> str:
    if not advice:
        return ""
    lines = []
    for a in advice[:max_items]:
        icon = "⚠️" if a.severity == "warn" else "💡"
        lines.append(f"{icon} {a.text}")
    more = len(advice) - max_items
    if more > 0:
        lines.append(f"   (还有 {more} 条)")
    return "== 中后期 ==\n" + "\n".join(lines)


# ===========================================================================
# 以下为后续追加的扩展(任务 Q / AA / AF)。
# 上面的函数签名与行为一概未动 —— 只做追加。
# ===========================================================================

_GAMEDATA = gamedata_path()


def _rows(table: str) -> list[dict[str, str]]:
    """读一张导出的规则表。任何失败都返回空列表 —— 这几个函数不抛异常。"""
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_ruleset() -> str:
    try:
        with (_GAMEDATA / "_all.json").open(encoding="utf-8") as fh:
            return json.load(fh).get("ruleset", "")
    except Exception:
        return ""


RULESET = _load_ruleset()


# ---------------------------------------------------------------------------
# 任务 Q:伟人博弈(招募 / 买断 / 跳过)
# ---------------------------------------------------------------------------
"""
伟人是【零和】的:同一个伟人全世界只有一个,你不招就是别人招。所以判断不是
"这个伟人好不好",而是"我花这笔钱截下他,值不值,以及落到对手手里有多疼"。

数据来源与边界(必须说清楚,否则这段会输出编造的效果):

    ✅ `GreatPersonIndividuals.json`(177 行)有:
         GreatPersonIndividualType / Name / GreatPersonClassType / EraType
       ⇒ 伟人的【名字、类别、时代】可以校验,不用编。
    ✅ `GreatPersonClasses.json`(9 行)给出 9 个类别的中文名。

    ❌ **每个伟人具体做什么,不在导出数据里。** GreatPersonIndividuals 只有
       上面四列;Modifiers 表里虽然有 GREATPERSON_* 修正器,但【没有导出
       个体 → 修正器的关联表】(GreatPersonIndividualActionModifiers 未导出),
       所以无法从游戏库反查"卡尔·萨根到底给多少产能"。
       ⇒ 效果一律使用调用方从 `get_great_people` 实时返回里带来的
         `ability` 字段原文。查不到就不描述效果,绝不猜。

    类别 → 胜利路线的契合度是编辑判断(标注为 [经验]),但它只影响排序,
    不构成任何数值承诺。
"""


def _load_gp_individuals() -> dict[str, dict[str, str]]:
    return {
        r["GreatPersonIndividualType"]: r
        for r in _rows("GreatPersonIndividuals")
        if r.get("GreatPersonIndividualType")
    }


def _load_gp_classes() -> dict[str, str]:
    return {
        r["GreatPersonClassType"]: r.get("Name") or r["GreatPersonClassType"]
        for r in _rows("GreatPersonClasses")
        if r.get("GreatPersonClassType")
    }


GP_INDIVIDUALS: dict[str, dict[str, str]] = _load_gp_individuals()
GP_CLASSES: dict[str, str] = _load_gp_classes()

# 中文名 → 个体标识,方便调用方直接传游戏里显示的名字。
GP_BY_NAME: dict[str, str] = {
    row.get("Name", ""): gid for gid, row in GP_INDIVIDUALS.items() if row.get("Name")
}

# [经验] 类别对各胜利路线的契合度。0-3 分,只用于排序。
_GP_CLASS_FIT: dict[str, dict[str, int]] = {
    "GREAT_PERSON_CLASS_SCIENTIST": {
        "SCIENCE": 3,
        "CULTURE": 1,
        "RELIGION": 0,
        "DOMINATION": 1,
    },
    "GREAT_PERSON_CLASS_ENGINEER": {
        "SCIENCE": 3,
        "CULTURE": 2,
        "RELIGION": 1,
        "DOMINATION": 2,
    },
    "GREAT_PERSON_CLASS_WRITER": {
        "SCIENCE": 0,
        "CULTURE": 3,
        "RELIGION": 0,
        "DOMINATION": 0,
    },
    "GREAT_PERSON_CLASS_ARTIST": {
        "SCIENCE": 0,
        "CULTURE": 3,
        "RELIGION": 0,
        "DOMINATION": 0,
    },
    "GREAT_PERSON_CLASS_MUSICIAN": {
        "SCIENCE": 0,
        "CULTURE": 3,
        "RELIGION": 0,
        "DOMINATION": 0,
    },
    "GREAT_PERSON_CLASS_PROPHET": {
        "SCIENCE": 0,
        "CULTURE": 0,
        "RELIGION": 3,
        "DOMINATION": 0,
    },
    "GREAT_PERSON_CLASS_MERCHANT": {
        "SCIENCE": 1,
        "CULTURE": 1,
        "RELIGION": 1,
        "DOMINATION": 1,
    },
    "GREAT_PERSON_CLASS_GENERAL": {
        "SCIENCE": 0,
        "CULTURE": 0,
        "RELIGION": 0,
        "DOMINATION": 3,
    },
    "GREAT_PERSON_CLASS_ADMIRAL": {
        "SCIENCE": 0,
        "CULTURE": 0,
        "RELIGION": 0,
        "DOMINATION": 2,
    },
}

# 招募建议档位
GP_RECRUIT = "招募"
GP_PATRONIZE = "买断"
GP_SKIP = "跳过"


@dataclass(frozen=True)
class GreatPersonOffer:
    """一位当前可争夺的伟人。字段与 lua/models.py 的 GreatPersonInfo 对齐。

    individual_type 可传 GREAT_PERSON_INDIVIDUAL_XXX 或游戏里显示的中文名;
    两者都会去 GreatPersonIndividuals.json 里校验。校验不过的不评分。
    """

    individual_type: str = ""
    individual_name: str = ""
    class_type: str = ""  # 传了就用,没传就从导出表补
    cost: int = 0  # 招募所需伟人点数
    player_points: int = 0  # 我方已积累的点数
    gold_cost: int = 0  # 金币买断价(0 = 不可买断)
    faith_cost: int = 0  # 信仰买断价
    can_recruit: bool = False  # 点数已够
    claimant: str = "Unclaimed"  # 已被谁预订
    ability: str = ""  # 实时返回里的效果原文;空串 = 不描述效果
    rival_points_leader: str = ""  # 点数领先的对手名(不知道就留空)


@dataclass
class GreatPersonContext:
    """伟人博弈所需的事实。"""

    victory_type: str = ""
    gold: int = 0
    faith: int = 0
    offers: list[GreatPersonOffer] = field(default_factory=list)


def gp_lookup(key: str) -> dict[str, str] | None:
    """按标识或中文名查伟人。查不到返回 None —— 不猜。"""
    if not key:
        return None
    row = GP_INDIVIDUALS.get(key)
    if row:
        return row
    gid = GP_BY_NAME.get(key)
    return GP_INDIVIDUALS.get(gid) if gid else None


def gp_class_cn(class_type: str) -> str:
    return GP_CLASSES.get(class_type, class_type)


def gp_fit_score(class_type: str, victory_type: str) -> int:
    """类别对路线的契合度 0-3。未知类别返回 0。"""
    table = _GP_CLASS_FIT.get(class_type)
    if not table:
        return 0
    return table.get((victory_type or "").upper(), 1)


def evaluate_great_person(ctx: GreatPersonContext, max_items: int = 3) -> list[str]:
    """任务 Q —— 伟人博弈。为每位可争夺的伟人给 契合度 + 建议 + 理由。

    返回 list[str];没有可评的伟人时返回空列表,不抛异常。
    """
    if not ctx.offers or not GP_INDIVIDUALS:
        return []

    scored: list[tuple[int, str]] = []
    for offer in ctx.offers:
        row = gp_lookup(offer.individual_type or offer.individual_name)
        if row is None:
            # 名字对不上导出表 —— 可能是资料片/模组内容。不评分,也不编。
            continue
        name = row.get("Name") or offer.individual_name
        class_type = offer.class_type or row.get("GreatPersonClassType", "")
        fit = gp_fit_score(class_type, ctx.victory_type)
        cn_class = gp_class_cn(class_type)

        # --- 决定建议 ---
        if fit >= 3 and offer.can_recruit:
            verdict, why = GP_RECRUIT, "点数已够,直接招募,不必花钱"
        elif fit >= 3 and offer.gold_cost > 0 and ctx.gold >= offer.gold_cost:
            verdict, why = (
                GP_PATRONIZE,
                f"和路线高度契合,而金币够({ctx.gold} ≥ {offer.gold_cost})—— "
                f"伟人是零和的,你不截下他就是对手的",
            )
        elif fit >= 3 and offer.faith_cost > 0 and ctx.faith >= offer.faith_cost:
            verdict, why = (
                GP_PATRONIZE,
                f"和路线高度契合,信仰够({ctx.faith} ≥ {offer.faith_cost})",
            )
        elif fit >= 3:
            gaps = []
            if offer.gold_cost > 0:
                gaps.append(f"金币差 {offer.gold_cost - ctx.gold}")
            if offer.faith_cost > 0:
                gaps.append(f"信仰差 {offer.faith_cost - ctx.faith}")
            shortfall = f"({','.join(gaps)})" if gaps else ""
            verdict, why = (
                GP_RECRUIT,
                f"契合度最高但现在买不起{shortfall} —— 攒点数或攒钱,别让它溜走",
            )
        elif fit <= 1:
            verdict, why = (
                GP_SKIP,
                f"{cn_class}对 {ctx.victory_type or '当前路线'} 帮助有限,"
                f"点数留给下一位更契合的",
            )
        else:
            verdict, why = GP_SKIP, "契合度一般,除非闲钱很多否则不值得买断"

        line = (
            f"· [{verdict}] {name}({cn_class},{row.get('EraType', '')})契合度 {fit}/3"
        )
        if offer.cost > 0:
            line += f",需 {offer.cost} 点(已有 {offer.player_points})"
        line += f" —— {why}。"

        if offer.ability:
            line += f" 效果:{offer.ability}"
        else:
            line += " (效果不在导出数据里,用 get_gp_advisor 看游戏内说明)"

        if offer.claimant and offer.claimant not in ("Unclaimed", "", "无"):
            line += f" ⚠️ 已被 {offer.claimant} 预订。"
        elif offer.rival_points_leader:
            line += f" ⚠️ {offer.rival_points_leader} 的点数领先你,再拖就没了。"

        scored.append((fit, line))

    if not scored:
        return []
    scored.sort(key=lambda p: -p[0])
    out = ["== 伟人博弈 =="] + [text for _, text in scored[:max_items]]
    if len(scored) > max_items:
        out.append(f"   (另有 {len(scored) - max_items} 位伟人未列出)")
    out.append(
        "   伟人是零和的:同一位全世界只有一个。跳过的判断要问的不是"
        "「他好不好」,而是「落到对手手里有多疼」。"
    )
    return out


# ---------------------------------------------------------------------------
# 任务 AA:科学胜利收尾(太空竞赛细节)
# ---------------------------------------------------------------------------
"""
`check_lategame` 只算了"还差多少产能"。但真正跑太空竞赛时,决定回合数的是
四件事:能不能并行、伟人截不截、铝够不够、太空港被不被破坏。

数据核对结果(这一节被数据纠正了两处,记在这里):

    ✅ `Projects.json` 五个太空项目 MaxPlayerInstances 全部 = 1
       ⇒ **同一个项目不能重复建**,但不同项目可以在不同太空港同时跑。
       并行的判据因此是完全可算的:看有几个项目的前置科技已到手。
    ✅ 前置科技也是导出值:
         地球卫星 TECH_ROCKETRY / 登月 TECH_SATELLITES /
         火星反应堆 TECH_NUCLEAR_FUSION / 火星住所 TECH_ROBOTICS /
         火星溶液栽培 TECH_NANOTECHNOLOGY
       ⇒ 三个火星项目的前置互相独立,科技到位后天然可以三线并行。
    ✅ `Resources.json`:RESOURCE_ALUMINUM 的 PrereqTech = TECH_RADIO(无线电)。
       ⇒ "没研究无线电就在地图上看不见铝"是可确证的事实。

    ❌ **皇家学会(Royal Society)在本规则集里不存在。** 计划书要求检查它,
       但 `Buildings.json` 里没有任何 ROYAL/SOCIETY 的行 —— 它是
       Gathering Storm 的政府广场建筑。本机 ruleset = RULESET_STANDARD。
       ⇒ 不提供"建造者加速太空项目"的建议,因为这条路在本局根本不存在。
    ❌ **项目的战略资源需求不在导出表里。** Projects 表只有
       ProjectType/Name/Cost/PrereqTech/PrereqCivic/MaxPlayerInstances/
       PrereqDistrict 七列,没有资源列。
       ⇒ 不承诺"火星反应堆需要 N 个铝",只提示铝的可见性与库存。
    ❌ **伟人个体 → 效果的关联表未导出**(同任务 Q)。卡尔·萨根 / 谢尔盖·
       科罗廖夫 / 沃纳·冯·布劳恩 三个名字已对 GreatPersonIndividuals.json
       校验过确实存在,但他们各自加多少产能查不到,所以只提名字不报数字。
"""

# 从 Buildings.json 反查:本规则集有没有皇家学会。有就是 True,没有就是 False。
ROYAL_SOCIETY_BUILDING = "BUILDING_ROYAL_SOCIETY"
ROYAL_SOCIETY_AVAILABLE: bool = any(
    r.get("BuildingType") == ROYAL_SOCIETY_BUILDING for r in _rows("Buildings")
)


def _spaceport_cost() -> int:
    """太空港造价 —— 从 Districts.json 读,不写死。查不到返回 0。"""
    for row in _rows("Districts"):
        if row.get("DistrictType") == "DISTRICT_SPACEPORT":
            try:
                return int(row.get("Cost") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


DISTRICT_SPACEPORT_COST: int = _spaceport_cost()

# 铝由哪个科技揭示 —— 从 Resources.json 反查,不写死。
_ALUMINUM = "RESOURCE_ALUMINUM"


def _aluminum_reveal_tech() -> tuple[str, str]:
    """(揭示铝的科技, 铝的中文名)。查不到返回 ("", "")。"""
    for row in _rows("Resources"):
        if row.get("ResourceType") == _ALUMINUM:
            return row.get("PrereqTech") or "", row.get("Name") or "铝"
    return "", ""


ALUMINUM_REVEAL_TECH, ALUMINUM_CN = _aluminum_reveal_tech()

# 太空竞赛相关伟人 —— 名字全部对 GreatPersonIndividuals.json 校验过。
# 效果不写,因为个体→修正器的关联表没有导出。
_SPACE_RACE_GP = (
    "GREAT_PERSON_INDIVIDUAL_CARL_SAGAN",
    "GREAT_PERSON_INDIVIDUAL_SERGEI_KOROLEV",
    "GREAT_PERSON_INDIVIDUAL_WERNHER_VON_BRAUN",
)
SPACE_RACE_GREAT_PEOPLE: tuple[str, ...] = tuple(
    (GP_INDIVIDUALS.get(g) or {}).get("Name", "")
    for g in _SPACE_RACE_GP
    if g in GP_INDIVIDUALS
)


@dataclass
class ScienceEndgameContext:
    """太空竞赛收尾判断所需的事实,全部来自实时查询。"""

    turn: int = 0
    techs: set[str] = field(default_factory=set)
    completed_projects: set[str] = field(default_factory=set)
    # 太空港
    spaceport_cities: tuple[str, ...] = ()  # 已建成太空港的城市名
    spaceports_running_project: int = 0  # 其中正在跑项目的数量
    counterspy_spaceport_cities: tuple[str, ...] = ()  # 有反间谍驻守的太空港城市
    # 资源
    aluminum_stock: int = 0
    # 伟人
    available_great_people: tuple[str, ...] = ()  # 当前可招募的伟人名(中文名)
    # 产能(用于把并行收益折算成回合)
    total_production: int = 0
    # 建造者(用于皇家学会那条检查)
    idle_builders: int = 0


def _runnable_projects(ctx: ScienceEndgameContext) -> list[Milestone]:
    """前置科技已到手、且还没完成的项目。MaxPlayerInstances=1,完成即不可重复。"""
    return [
        m
        for m in SCIENCE_CHAIN
        if m.key not in ctx.completed_projects
        and (not m.prereq_tech or m.prereq_tech in ctx.techs)
    ]


def science_endgame(ctx: ScienceEndgameContext, max_items: int = 5) -> list[str]:
    """任务 AA —— 科学胜利收尾检查。

    返回 list[str];没到收尾阶段或无可操作项时返回空列表,不抛异常。
    """
    remaining = [m for m in SCIENCE_CHAIN if m.key not in ctx.completed_projects]
    if not remaining:
        return []
    if not ctx.spaceport_cities and not ctx.techs:
        return []

    out: list[str] = []
    runnable = _runnable_projects(ctx)

    # --- 1. 多太空港并行 ---
    ports = len(ctx.spaceport_cities)
    if len(runnable) >= 2 and ports >= 2:
        idle_ports = max(0, ports - ctx.spaceports_running_project)
        parallel = min(idle_ports, len(runnable) - ctx.spaceports_running_project)
        if parallel > 0:
            names = "、".join(m.name for m in runnable[:ports])
            line = (
                f"⚡ 你有 {ports} 座太空港,{len(runnable)} 个项目的前置科技已到手"
                f"({names})。这些是不同的项目(每个 MaxPlayerInstances=1,"
                f"同一个不能重复建),可以在不同太空港【同时】跑。"
                f"当前只有 {ctx.spaceports_running_project} 座在跑 —— "
                f"还能再开 {parallel} 条线。"
            )
            if ctx.total_production > 0:
                saved = sum(m.cost for m in runnable[1 : 1 + parallel])
                line += (
                    f" 并行可省下约 {saved / ctx.total_production:.0f} 回合的串行等待。"
                )
            out.append(line)
    elif len(runnable) >= 2 and ports == 1:
        out.append(
            f"💡 有 {len(runnable)} 个项目的前置科技已到手,但只有 1 座太空港 —— "
            f"太空港({DISTRICT_SPACEPORT_COST} 产能)本身很贵,但第二座能让"
            f"两个项目并行,后期通常比多堆一座学院更划算。"
        )

    # --- 2. 铝:先确认看不看得见,再确认够不够 ---
    # 只在太空竞赛真的开始后才提(有太空港,或火箭研究已到手)——
    # 否则远早于火星阶段就开始念铝,纯粹是噪音。
    in_space_race = bool(ctx.spaceport_cities) or "TECH_ROCKETRY" in ctx.techs
    mars = [m for m in remaining if "MARS" in m.key] if in_space_race else []
    if mars:
        if ALUMINUM_REVEAL_TECH and ALUMINUM_REVEAL_TECH not in ctx.techs:
            out.append(
                f"⚠️ 还没研究 {ALUMINUM_REVEAL_TECH} —— {ALUMINUM_CN}在地图上"
                f"根本看不见。火星阶段前必须先点它,否则连有没有矿都不知道。"
            )
        elif ctx.aluminum_stock <= 0:
            out.append(
                f"⚠️ {ALUMINUM_CN}库存为 0,而还剩 {len(mars)} 个火星项目。"
                f"(项目的资源需求列不在导出表里,以游戏内提示为准 —— "
                f"但库存 0 无论如何都该先解决:开矿或交易换取。)"
            )

    # --- 3. 太空港反间谍 ---
    undefended = [
        c for c in ctx.spaceport_cities if c not in ctx.counterspy_spaceport_cities
    ]
    if undefended:
        out.append(
            f"⚠️ 太空港所在城市 {('、'.join(undefended[:2]))} 没有反间谍驻守 —— "
            f"对手破坏一次太空港,你要花几十回合修回来。"
            f"走科学胜利时这是最值钱、也最容易被盯上的目标。"
        )

    # --- 4. 伟人截胡 ---
    if SPACE_RACE_GREAT_PEOPLE:
        hits = [g for g in ctx.available_great_people if g in SPACE_RACE_GREAT_PEOPLE]
        if hits:
            out.append(
                f"💡 太空竞赛关键伟人现在可争夺:{('、'.join(hits))} —— "
                f"用信仰/金币买断通常比继续攒点数快。"
                f"(各自的具体加成不在导出数据里,用 get_great_people 看游戏内说明。)"
            )

    # --- 5. 皇家学会:本规则集没有,说清楚免得白找 ---
    if ctx.idle_builders > 0 and not ROYAL_SOCIETY_AVAILABLE:
        out.append(
            f"💡 有 {ctx.idle_builders} 个空闲建造者,但本规则集({RULESET})"
            f"【没有皇家学会】—— 建造者牺牲次数加速太空项目是 Gathering Storm "
            f"的机制,本局不存在。建造者该去开矿堆产能,别留着等这个。"
        )

    if not out:
        return []
    if len(out) > max_items:
        omitted = len(out) - max_items
        out = out[:max_items]
        out.append(f"   (另有 {omitted} 条太空竞赛提醒已省略)")
    return ["== 太空竞赛收尾 ==", *out]


# ---------------------------------------------------------------------------
# 任务 AF:外交胜利 DVP 追踪
# ---------------------------------------------------------------------------
"""
⚠️ **先说结论:外交胜利在本机的规则集里不存在。**

实测 `docs/gamedata/Victories.json` 只有 6 行,全部列在这里:

    VICTORY_SCORE(分数) / VICTORY_DEFAULT / VICTORY_CONQUEST(统治)
    VICTORY_CULTURE(文化) / VICTORY_RELIGIOUS(宗教) / VICTORY_TECHNOLOGY(科技)

**没有 VICTORY_DIPLOMATIC。** 进一步在全部 38 张导出表里搜
`DIPLOMATIC_VICTORY|FAVOR|WORLD_CONGRESS|CARBON|EMISSION|SEASTEAD|
STATUE_OF_LIBERTY` —— 只命中两条与议程有关的 `Favored` 参数,和外交胜利无关。

也就是说计划书任务 AF 点名的四个 DVP 来源,在 RULESET_STANDARD 下【全部不存在】:

    · 自由女神像       —— Buildings.json 里没有这个奇观(Rise & Fall 内容)
    · 海会场科技       —— Technologies.json 68 个节点里没有
    · 世界议会 DVP 决议 —— 没有任何 WorldCongress 相关表
    · 碳排放救济竞赛   —— 没有任何 Carbon/Emission 相关行

⇒ 所以本函数的设计是【数据自门控】的:
   `DIPLOMATIC_VICTORY_AVAILABLE` 直接从 Victories.json 算出来。
   本规则集下它是 False,函数对空输入一律返回 [],一个字都不输出,
   绝不凭空生成一套本局根本无法执行的建议。

   但函数体本身是按真实规则写完整的:如果哪天换到 Gathering Storm 规则集
   重新导出一次 gamedata,`DIPLOMATIC_VICTORY_AVAILABLE` 会自动变 True,
   算术部分不用改一行就能用。同时,只要调用方真的传进来了非零的 DVP 数据
   (这种数据只可能来自有外交胜利的规则集),函数也会照常计算 ——
   这样既不会在本局刷屏,也不会在换规则集后静默失效。
"""

DIPLOMATIC_VICTORY_TYPE = "VICTORY_DIPLOMATIC"


def _load_victory_types() -> set[str]:
    return {r["VictoryType"] for r in _rows("Victories") if r.get("VictoryType")}


AVAILABLE_VICTORY_TYPES: set[str] = _load_victory_types()
DIPLOMATIC_VICTORY_AVAILABLE: bool = DIPLOMATIC_VICTORY_TYPE in AVAILABLE_VICTORY_TYPES

# 达成外交胜利所需的 DVP。不在导出表里(本规则集根本没有这个系统),
# 标注为 [经验] 默认值,并且允许调用方覆盖。
DEFAULT_DVP_TARGET = 20


def dvp_availability_note() -> str:
    """给集成方看的一句话:本规则集到底有没有外交胜利。"""
    if DIPLOMATIC_VICTORY_AVAILABLE:
        return f"{RULESET} 支持外交胜利,DVP 追踪有效。"
    return (
        f"{RULESET} 的 Victories 表里没有 {DIPLOMATIC_VICTORY_TYPE} —— "
        f"外交胜利、外交favor、世界议会、碳排放竞赛在本规则集下都不存在,"
        f"track_dvp_progress 会保持静默。"
    )


@dataclass
class DVPContext:
    """DVP 追踪所需的事实。全部由实时查询提供;本规则集下应当全为 0/空。"""

    turn: int = 0
    current_dvp: int = 0
    target_dvp: int = DEFAULT_DVP_TARGET
    # 已确定的来源
    wonder_dvp: int = 0  # 已建成的 DVP 奇观贡献
    tech_dvp: int = 0  # 已完成的 DVP 科技贡献
    congress_dvp: int = 0  # 已赢下的 WC 决议贡献
    competition_dvp: int = 0  # 竞赛(如碳排放救济)贡献
    # 在途
    wonder_turns_left: int = -1  # DVP 奇观还差几回合建成(-1 = 没在造)
    wonder_name: str = ""
    wonder_dvp_value: int = 0
    turns_between_congress: int = 0  # 每届 WC 间隔回合
    dvp_per_congress: int = 0  # 每届预计能拿到的 DVP
    # 风险
    dvp_stripping_resolution: bool = False  # 本届 WC 有"剥夺我方 DVP"的决议
    my_votes: int = 0
    rival_max_dvp: int = 0
    rival_max_dvp_name: str = ""


def track_dvp_progress(ctx: DVPContext, max_items: int = 4) -> list[str]:
    """任务 AF —— 外交胜利 DVP 追踪。

    返回 list[str];本规则集没有外交胜利且没有任何 DVP 数据时返回空列表,
    不抛异常。
    """
    has_data = any(
        (
            ctx.current_dvp,
            ctx.wonder_dvp,
            ctx.tech_dvp,
            ctx.congress_dvp,
            ctx.competition_dvp,
            ctx.dvp_per_congress,
            ctx.rival_max_dvp,
            ctx.dvp_stripping_resolution,
            ctx.wonder_turns_left >= 0,
        )
    )
    if not DIPLOMATIC_VICTORY_AVAILABLE and not has_data:
        return []

    target = ctx.target_dvp if ctx.target_dvp > 0 else DEFAULT_DVP_TARGET
    out: list[str] = []

    # --- 1. 进度与来源拆解 ---
    sources = [
        ("奇观", ctx.wonder_dvp),
        ("科技", ctx.tech_dvp),
        ("世界议会决议", ctx.congress_dvp),
        ("竞赛", ctx.competition_dvp),
    ]
    breakdown = "、".join(f"{name} {n}" for name, n in sources if n)
    gap = max(0, target - ctx.current_dvp)
    head = f"📊 外交胜利进度:{ctx.current_dvp}/{target} DVP"
    if breakdown:
        head += f"(来源:{breakdown})"
    head += f",还差 {gap}。" if gap else ",已达标。"
    out.append(head)

    # --- 2. 最早完成回合的倒推 ---
    if gap > 0:
        eta_parts: list[str] = []
        pending = gap
        if ctx.wonder_turns_left >= 0 and ctx.wonder_dvp_value > 0:
            pending -= ctx.wonder_dvp_value
            eta_parts.append(
                f"{ctx.wonder_name or '在建的 DVP 奇观'}还有 "
                f"{ctx.wonder_turns_left} 回合建成(+{ctx.wonder_dvp_value})"
            )
        if pending > 0 and ctx.dvp_per_congress > 0 and ctx.turns_between_congress > 0:
            congresses = -(-pending // ctx.dvp_per_congress)  # 向上取整
            turns = congresses * ctx.turns_between_congress
            eta = ctx.turn + max(turns, ctx.wonder_turns_left)
            eta_parts.append(
                f"其余 {pending} DVP 需赢下 {congresses} 届世界议会决议"
                f"(每届间隔 {ctx.turns_between_congress} 回合)"
            )
            out.append(f"⏳ 预计最早完成:T{eta} —— " + ";".join(eta_parts) + "。")
        elif eta_parts:
            out.append(
                f"⏳ {';'.join(eta_parts)};剩余 {max(0, pending)} DVP "
                f"暂无确定来源,需要靠世界议会决议或竞赛补。"
            )
        else:
            out.append(
                f"⏳ 还差 {gap} DVP 但当前没有任何在途来源 —— "
                f"外交胜利拿不到被动 DVP,不主动争决议就永远停在这个数。"
            )

    # --- 3. 被扣 DVP 的风险 ---
    if ctx.dvp_stripping_resolution:
        out.append(
            f"🔴 本届世界议会有【剥夺你 DVP】的决议 —— "
            f"你有 {ctx.my_votes} 票。DVP 被扣掉是纯粹的倒退,"
            f"优先把外交点砸在压掉这条决议上,比拿新 DVP 更划算。"
        )

    # --- 4. 对手进度 ---
    if ctx.rival_max_dvp >= ctx.current_dvp and ctx.rival_max_dvp > 0:
        lead = ctx.rival_max_dvp - ctx.current_dvp
        who = ctx.rival_max_dvp_name or "最强对手"
        out.append(
            f"⚠️ {who} 有 {ctx.rival_max_dvp} DVP"
            + (f",领先你 {lead}。" if lead else ",与你持平。")
            + "外交胜利是竞速,DVP 差距只能靠抢决议追回来。"
        )

    if len(out) > max_items:
        omitted = len(out) - max_items
        out = out[:max_items]
        out.append(f"   (另有 {omitted} 条 DVP 提醒已省略)")
    return out


# ---------------------------------------------------------------------------
# 任务 Z:胜利阻断(Stop the Runner)
# ---------------------------------------------------------------------------
"""
后期最重要、也最反直觉的一条判断:**什么时候放下自己的胜利路线去阻止别人**。

你可能在稳稳地走科学胜利,但西班牙已经让 6/7 的文明信了他的教 —— 你再跑
20 回合太空项目,他先赢,你做的一切归零。阻断的优先级高于自己的主线,
因为"我赢"和"他不赢"里,后者是前者的前提。

判定阈值(下面这四条是**编辑判断**,不是导出值 —— 它们回答的是"还剩多少
窗口",而游戏里没有"剩余窗口"这张表):
    宗教:对手的宗教已在 ≥(其它文明数-1) 个文明成为多数 → 🔴(只差最后一个)
    科学:对手已完成 ≥3/5 太空项目 → 🔴(火星三件套可并行,后段崩得很快)
    文化:对手的外国游客 ≥ 你的国内游客 ×0.8 → 🟡(还有缓冲但已进射程)
    统治:对手已占 ≥3 个原始首都 → 🟡(雪球已经开始滚)

胜利判据本身来自 VICTORY_PATHS(已在本文件上方,来自导出的 Victories 表);
太空链长度来自 SCIENCE_CHAIN(来自 Projects.json)—— 阈值分母不写死。

阻断方案是**具体动作**,不是"去阻止他":每条路线怎么打断,写在
_BLOCK_PLAYBOOK 里。这部分是战术知识,标注为经验。
"""

# 阈值(编辑判断,见上文说明)
CULTURE_ALARM_RATIO = 0.8
DOMINATION_ALARM_CAPITALS = 3
SCIENCE_ALARM_PROJECTS = 3

_BLOCK_PLAYBOOK: dict[str, tuple[str, ...]] = {
    "RELIGION": (
        "对他宣战 —— 战争状态下你的军事单位可以直接踏死他的传教士/使徒"
        "(宗教单位没有战斗力,这是最便宜的打断)",
        "在自己城市里用审判官清异教,把他已转化的城翻回来",
        "他还没转化的那几个文明,派你自己的传教士去占坑 —— 多数宗教只能有一个,先到先得",
    ),
    "SCIENCE": (
        "派间谍去他的宇航中心做 SABOTAGE_PRODUCTION —— "
        "太空项目卡的是产能,破坏一次够他修几十回合",
        "间谍不够就宣战:骑兵突袭宇航中心所在城,掠夺区域直接瘫痪产出",
        "抢铝:火星阶段吃铝,断了他的铝矿等于断链(先确认他的铝从哪来)",
    ),
    "CULTURE": (
        "关闭对他的开放边界 —— 开放边界给对方 +25% 旅游,这是白送的",
        "撤掉通往他的贸易路线 —— 同样是 +25% 旅游的白送项",
        "宣战抢他的巨作城:巨作被夺走,旅游业绩立刻掉",
    ),
    "DOMINATION": (
        "给他的下一个目标送援军/结盟 —— 让他啃硬骨头,雪球就停了",
        "趁他主力在外,反打他的本土城(统治文明的后院通常很空)",
        "联合其他受威胁的文明一起宣他 —— 多线作战是雪球的天敌",
    ),
}


@dataclass
class RivalVictoryContext:
    """胜利阻断判断所需的事实,全部来自实时查询。

    total_civs        : 局内存活的主要文明总数(含你)
    religion_majority : {文明名: 该文明的宗教已在几个文明成为多数}
    space_projects    : {文明名: 已完成的太空项目数}
    tourism           : {文明名: 该文明的外国游客数}
    my_domestic_tourists : 你的国内游客数(文化胜利的分母)
    original_capitals : {文明名: 已占领的原始首都数(含自己的)}
    at_war_with       : 已经和你交战的文明名
    """

    turn: int = 0
    my_victory_type: str = ""
    total_civs: int = 0
    religion_majority: dict = field(default_factory=dict)
    space_projects: dict = field(default_factory=dict)
    tourism: dict = field(default_factory=dict)
    my_domestic_tourists: int = 0
    original_capitals: dict = field(default_factory=dict)
    at_war_with: tuple = ()


def _block_lines(victory: str) -> list[str]:
    return [
        f"  ① {p}" if i == 0 else f"  {'②③④'[i - 1]} {p}"
        for i, p in enumerate(_BLOCK_PLAYBOOK.get(victory, ())[:3])
    ]


def check_rival_victory_threat(
    ctx: RivalVictoryContext, max_items: int = 2
) -> list[str]:
    """任务 Z —— 对手逼近胜利时的阻断方案。没人快赢时返回空列表,不抛异常。"""
    try:
        return _rival_threat_inner(ctx, max_items)
    except Exception:
        return []


def _rival_threat_inner(ctx: RivalVictoryContext, max_items: int) -> list[str]:
    threats: list[tuple[int, str, str, list[str]]] = []  # (紧急度, 文明, 抬头, 明细)
    others = max(0, int(ctx.total_civs or 0) - 1)

    # —— 宗教:只差最后一两个文明时最危险 ——
    for civ, converted in (ctx.religion_majority or {}).items():
        try:
            converted = int(converted)
        except (TypeError, ValueError):
            continue
        if others <= 0 or converted <= 0:
            continue
        remaining = others - converted
        if remaining <= 1:
            threats.append(
                (
                    0,
                    civ,
                    f"🔴 {civ} 距宗教胜利只差 {max(remaining, 0)} 个文明"
                    f"({converted}/{others} 已被转化)—— 这是最快的胜利路线,"
                    f"一旦补完立刻结束。",
                    _block_lines("RELIGION"),
                )
            )
        elif remaining <= 2:
            threats.append(
                (
                    1,
                    civ,
                    f"🟡 {civ} 的宗教已覆盖 {converted}/{others} 个文明,"
                    f"还差 {remaining} 个 —— 现在动手成本最低,再等就来不及。",
                    _block_lines("RELIGION"),
                )
            )

    # —— 科学:分母来自 SCIENCE_CHAIN,不写死 ——
    total_projects = len(SCIENCE_CHAIN)
    for civ, done in (ctx.space_projects or {}).items():
        try:
            done = int(done)
        except (TypeError, ValueError):
            continue
        if done >= SCIENCE_ALARM_PROJECTS:
            left = total_projects - done
            threats.append(
                (
                    0 if left <= 2 else 1,
                    civ,
                    f"{'🔴' if left <= 2 else '🟡'} {civ} 已完成 {done}/{total_projects} "
                    f"个太空项目,还剩 {left} 个 —— 火星三件套可以多太空港并行,"
                    f"后段比你想的快。",
                    _block_lines("SCIENCE"),
                )
            )

    # —— 文化:比的是他的外国游客 vs 你的国内游客 ——
    mine = int(ctx.my_domestic_tourists or 0)
    if mine > 0:
        for civ, foreign in (ctx.tourism or {}).items():
            try:
                foreign = int(foreign)
            except (TypeError, ValueError):
                continue
            if foreign >= mine * CULTURE_ALARM_RATIO:
                ratio = foreign / mine
                threats.append(
                    (
                        0 if ratio >= 1.0 else 1,
                        civ,
                        f"{'🔴' if ratio >= 1.0 else '🟡'} {civ} 的外国游客 {foreign} "
                        f"vs 你的国内游客 {mine}(比值 {ratio:.2f})—— "
                        f"文化胜利的判据就是这个比值超过 1。",
                        _block_lines("CULTURE"),
                    )
                )

    # —— 统治:占了几个原始首都 ——
    for civ, caps in (ctx.original_capitals or {}).items():
        try:
            caps = int(caps)
        except (TypeError, ValueError):
            continue
        if caps >= DOMINATION_ALARM_CAPITALS:
            threats.append(
                (
                    1,
                    civ,
                    f"🟡 {civ} 已占 {caps} 个原始首都 —— 统治胜利的雪球已经在滚,"
                    f"每多一个首都他的产能就更强一档。",
                    _block_lines("DOMINATION"),
                )
            )

    if not threats:
        return []
    threats.sort(key=lambda t: t[0])
    out = ["== 🔴 胜利阻断(优先级高于你自己的胜利路线) =="]
    for urgency, civ, head, plan in threats[:max_items]:
        out.append(head)
        out.extend(plan)
        if civ in (ctx.at_war_with or ()):
            out.append(f"  (你已在和 {civ} 交战 —— 军事手段随时可用,别只顾攻城)")
    if len(threats) > max_items:
        out.append(f"   (另有 {len(threats) - max_items} 个对手在逼近胜利)")
    if ctx.my_victory_type:
        out.append(
            f"   权衡:你在走 {ctx.my_victory_type}。阻断会拖慢自己的进度,"
            f"但「他赢」是「你赢」的否决项 —— 先确保没人先冲线,再谈自己的节奏。"
        )
    out.append("   (阈值为编辑判断,不是导出值;阻断手段为战术经验)")
    return out
