"""开局四维评估器(任务 AM)—— 整个计划里最重要的模块。

其他模块输出碎片建议("走科技""造投石")。这个模块输出**原理级别的连贯
叙事**:文明 × 地图 × 邻居 × 难度 → 一段读完就"理解了局面"的文字。
它是「知识库(strategy_space)+ 推理引擎」里推理引擎的第一版:知识库给
原理,本模块把原理对着本局的实际地形、资源、邻居做**兑现率评估**。

═══════════════════════════════════════════════════════════════════════════
## game_state 输入契约(dict,所有键都可缺省 —— 缺什么就降级什么)

    civ / leader      : str   标识或中文名("CIVILIZATION_ROME"/"罗马"/"图拉真")
    difficulty        : str   DIFFICULTY_*(缺省不输出难度规则)
    map_type          : str   PANGAEA/CONTINENTS/ARCHIPELAGO/...(缺省走 DEFAULT 计划)
    turn              : int
    capital           : {"x":int,"y":int} 或 (x,y)
    my_military       : int   我方军力值
    map_total_tiles   : int   全图格数(算探索度用,可缺省)
    explored_tiles    : list[dict] 每格与 lua/models.TileInfo 对齐:
                        x,y,terrain,feature,resource,is_hills,is_river,
                        is_coastal,resource_class("luxury"/"strategic"/"bonus")
    neighbors         : list[dict]:
                        civ_name,leader_name,distance(可缺省,有 x,y 则自算),
                        military_strength,agendas(["AGENDA_X"] 或 [{"name":...}])

坐标系(AGENTS.md):X 增大 = 东,**Y 增大 = 南**。方向判定依此,勿反。

## 输出:不超过 35 行的连贯叙事,四段自然衔接(无编号标题)
    ① 你在这个局面里是谁(能力利用率,>80% 或 <50% 时明说)
    ② 地图告诉了你什么(几何 → 战略洞察的翻译)
    ③ 邻居是谁,意味着什么(议程 → 他为什么打你 + 怎么为你所用)
    ④ 推荐路线(推荐/备选/不推荐,各带论证与置信度)

## 三份 mock 评估报告(格式规范 —— 先于代码写成,代码向它们看齐)

=== mock 1:罗马 × 盘古 × 蒙特祖玛(11 格) × 预测有铁 ===================
【开局评估】罗马·图拉真 ‖ T1 ‖ PANGAEA ‖ 王子
你是窗口型文明:军团(40,寡头下44)对同期一切近战是代差,铺城自带路网,
浴场半价。盘古大陆让这套全部兑现——陆地邻居就是军团的靶子。能力利用率
约 90%,这局走你最擅长的路。唯一要盯的:对手进入中世纪后窗口关闭,
一切围绕 T55 前后的战争窗口安排。
地图正在替你做防御规划:首都西面的连片山脉是天然城墙——西面不需驻军,
兵力全押东面开阔地。最近海岸约 12 格,15 回合内没人能从海上来,造船跳过。
首都沿河,住房与商业中心相邻都有着落。马(60,9)已可见,是早期骑兵的门票;
东北向丘陵群无林覆盖,是铁的典型生成地形——大概率有铁(这是预测不是事实,
青铜加工完成后立刻确认)。
蒙特祖玛在约 11 格外,军力 28 对你 20,议程「特拉托阿尼」:他看重奢侈品,
你占着他想要的就会积怨,会为资源开战(战争倾向:高)。反过来用:主动卖他
一份奢侈换金和和约——合约期内他不宣战,这 30 回合正好盖住你的军团成型期。
你的应对(策略库):先发制人,他就是军团窗口的第一目标。
推荐:统治胜利(约 85%)。论证链:军团代差 + 近邻军力可测且议程可安抚 +
盘古无海峡阻隔。前 30 回合:采矿→青铜加工(顺手杀 3 个蛮族拿尤里卡)→
炼铁术,市政直奔政治哲学换寡头;生产侦察→投石→开拓者,T30 前落第二城;
先卖奢侈稳住蒙特祖玛,T55 前 3-4 个军团直接开他首都。
风险:若实机证实军团需要铁而铁点被他先占,窗口不开。
预案(约 60%):转防守反击或文化——免费建筑的文化底子直接接盘。
备选(约 60%):军团窗口错过 → 浴场宽铺转科技,内贸路线喂产能。
不推荐:宗教(约 30%)——已核能力清单里没有一条信仰相关,这条路你是白板,
大预言家抢不到则整条路线报废。

=== mock 2:俄罗斯 × 群岛 × 无邻居 × 冻土带 ============================
【开局评估】俄罗斯·彼得 ‖ T1 ‖ ARCHIPELAGO ‖ 国王
你是引擎型文明:拉夫拉 27 锤半价、冻土 +1 信 +1 锤(归属推断)、建城多圈
地。群岛对多数文明是路线否决,对你不是——拉夫拉不挑地形,宗教引擎照转。
能力利用率约 85%(冻土盘吃满;哥萨克在群岛难有用武之地,扣分全在军事端)。
首都圈内冻土 11 格:别人的垃圾地是你的正常地块,往北铺城无人跟你抢。
西北向有山脉贴脸,拉夫拉的信仰相邻已有着落。已探明格里海岸近在咫尺——
探明本岛边界前不知道还有几个铺城位,造船探图的优先级高于一切军事。
未见任何奢侈资源,宜居度要靠万神殿或买入解决。
尚未遇见其他文明。群岛的接触来得晚——这是宗教文明的天然保护期,但大预言家
的竞速对手数量未知,不能因为没见人就放慢圣地节奏。探索方向:沿海岸线两翼
推进,先确认本岛容量,再放船出海。
推荐:宗教胜利(约 80%)。论证链:半价拉夫拉的先手权 + 群岛保护期 + 冻土
信仰盘。前 30 回合:占星术最优先,T30 前首城拉夫拉落地(贴山脉),信仰
攒够立万神殿;第二城往北冻土带铺,继续拉夫拉;军事只留一弓防蛮族登陆。
风险:群岛传教靠船,航海科技落下会让后期传播成本翻倍。
备选(约 60%):大预言家被抢 → 拉夫拉不退款,信仰买自然学家/摇滚乐队,
同一引擎换个出口走文化。
不推荐:统治(约 35%)——哥萨克过不了海,你没有任何海军加成,群岛统治
等于用短板打别人的水面主场。

=== mock 3:德国 × 盘古 × 姆本巴(13 格) × 无铁地形(德国不需要铁) =====
【开局评估】德国·弗雷德里克·巴巴罗萨 ‖ T1 ‖ PANGAEA ‖ 君主
你是结构型文明:每城 +1 区域槽(归属推断),汉萨 27 锤半价且吃商业中心
+2/资源 +1 相邻。盘古腹地的资源簇正对你的胃口——能力利用率约 90%,这局
走你最擅长的路:用锤子把科技胜利砸出来。AI 的难度加成在产出侧,你的优势
在结构侧,不怕稀释。
首都 4 格内资源 6 处,汉萨菱形(汉萨+商业中心+资源簇)在二环就能成型。
附近无铁特征地形(丘陵多有林),需探明后确认——但你的军事线不吃铁,这条
信息是防邻居用的。少山:学院相邻一般,坐实"科技靠产能不靠相邻"的路线。
姆本巴在约 13 格外,军力与你相当,议程「热忱信徒」:他看重你接受他的宗教,
城市信别的教会积怨,会派传教士压你(战争倾向:中)。反过来用:他不创教
只接收——宗教赛道上他是接收方不是对手,当温和邻居与贸易伙伴用。
13 格的距离意味着南向铺城空间还有余量,先到先得。
推荐:科技胜利(约 85%)。论证链:区域槽+半价汉萨的结构优势 + 腹地资源簇
+ 南邻温和。前 30 回合:侦察探明城邦与资源簇,铺城以"汉萨菱形能否成型"
为第一标准,T30 前 3 城;科技采矿→畜牧→直奔学徒制;近身若有低价值城邦,
开局军力(对城邦 +7)顺手吃一个,议程免罚。
风险:盘古腹地必有未现身的邻居——军事槽保持插卡,汉萨产能就是暴兵底气。
备选(约 60%):科技被强邻反超且打断失败 → 汉萨引擎直接改产军队,
德国转军事的成本全图最低。
不推荐:宗教(约 30%)——已核能力没有一条信仰相关,而真正的宗教狂早已
起跑,从零追没有意义。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass, field
from pathlib import Path

from civ_mcp import strategy_space
from civ_mcp.hexgrid import hex_distance
from civ_mcp.lategame import VICTORY_PATHS
from civ_mcp.opponents import AGENDA_PROFILES
from civ_mcp.params import DIFFICULTY_CN

_GAMEDATA = gamedata_path()

MAX_LINES = 35

# 置信度档 → 展示用百分数(与 advice.Confidence 的区间一致,取整数点位)
_CONF_PCT = {"确定": "约 95%", "推荐": "约 85%", "可选": "约 65%", "冒险": "约 40%"}
_CONF_UTIL = {"推荐": 85, "可选": 60, "冒险": 40}

# 胜利类型 → 该文明"有没有这条路的本钱"的关键词(在 identity+principles 里搜)
_VICTORY_KEYWORDS = {
    "RELIGION": ("信仰", "圣地", "宗教", "拉夫拉", "传教"),
    "SCIENCE": ("科技", "学院", "汉萨", "产能", "太空"),
    "CULTURE": ("文化", "剧院", "纪念碑", "旅游", "巨作"),
    "DOMINATION": ("军团", "战力", "军事", "哥萨克", "战争", "碾压"),
}

# 议程 → "怎么为你所用"。这是战略解读(与 opponents.AGENDA_PROFILES 的
# 画像同一性质:人写的推演,不是游戏数据),逐条对应 20 个历史议程。
_AGENDA_LEVERAGE: dict[str, str] = {
    "AGENDA_TLATOANI": "主动卖他一份奢侈换金和和约——合约期内他不宣战,正好盖住你的成型期",
    "AGENDA_WONDER_OBSESSED": "别跟他抢奇观;他的产能都埋在奇观里,军队必然空虚——要么让,要么连城收走",
    "AGENDA_ENTHUSIASTIC_DISCIPLE": "他不创教只接收——走宗教他是免费市场,不走宗教他就是温和邻居",
    "AGENDA_WESTERNIZER": "科技文化两项别垫底他就无怨;他也是高级贸易路线的好目的地",
    "AGENDA_IRON_CROWN": "别当城邦宗主往他枪口上凑;他打城邦时你正好用使者接盘",
    "AGENDA_OPTIMUS_PRINCEPS": "多铺城他反而敬你——扩张本身就是对他的外交",
    "AGENDA_PEACEKEEPER": "不主动宣战他就是安全后方,边境兵力可以全部调走",
    "AGENDA_BLACK_QUEEN": "她靠间谍吃饭——关键区域留反间谍,别让她白拿你的科技",
    "AGENDA_QUEEN_OF_NILE": "军力压过她她就亲近你——一支路过的大军能换来一个贸易伙伴",
    "AGENDA_ALLY_OF_ENKIDU": "主动结盟,他的联合作战加成会替你打架",
    "AGENDA_WITH_YOUR_SHIELD_OR_ON_IT": "军力别垫底,否则你就是猎物;对她先发制人优于等挨打",
    "AGENDA_LAST_VIKING_KING": "海军弱在他眼里是原罪——沿海城至少备一船,或干脆不沿海",
    "AGENDA_BUSHIDO": "要打就全力打——他鄙视军力虚弱,也鄙视虚张声势",
    "AGENDA_GREAT_PERSON_OBSESSED": "伟人竞速被他加压——关键伟人提前留钱买断,别跟他拼点数",
    "AGENDA_DELIAN_LEAGUE": "抢他罩着的城邦他翻脸——对城邦用使者别用刀",
    "AGENDA_COUNTER_REFORMER": "教别传到他家门口——宗教边界就是和平线",
    "AGENDA_AYYUBID_DYNASTY": "他的信徒城别碰,你的教也别硬压——各传各的相安无事",
    "AGENDA_BACKSTAB_AVERSE": "永不背刺她——记仇全局;反过来她是最可靠的盟友人选",
    "AGENDA_BIG_STICK_POLICY": "他是本大陆警察——大陆内别开战,跨陆行动他不管",
    "AGENDA_SUN_NEVER_SETS": "她盯着所有大陆——你的殖民扩张会踩她议程,跨陆铺城前掂量",
}


# ---------------------------------------------------------------------------
# gamedata 辅助
# ---------------------------------------------------------------------------

_res_cache: dict[str, tuple[str, str]] | None = None


def _resource_info(rtype: str) -> tuple[str, str]:
    """RESOURCE_X → (中文名, 类别小写)。查不到返回 (原名, "")。"""
    global _res_cache
    if _res_cache is None:
        _res_cache = {}
        try:
            with (_GAMEDATA / "Resources.json").open(encoding="utf-8") as fh:
                for row in json.load(fh).get("rows", []):
                    cls = (
                        (row.get("ResourceClassType") or "")
                        .removeprefix("RESOURCECLASS_")
                        .lower()
                    )
                    _res_cache[row.get("ResourceType", "")] = (
                        row.get("Name") or row.get("ResourceType", ""),
                        cls,
                    )
        except Exception:
            pass
    return _res_cache.get(rtype, (rtype, ""))


# ---------------------------------------------------------------------------
# 输入视图(容错解析)
# ---------------------------------------------------------------------------


@dataclass
class _Neighbor:
    civ_name: str = "未知文明"
    leader_name: str = ""
    distance: int | None = None
    military: int | None = None
    agendas: tuple[str, ...] = ()


@dataclass
class _View:
    """game_state 的容错视图。所有字段缺省安全。"""

    strat: strategy_space.CivStrategy | None = None
    civ_key: str = ""
    turn: int = 1
    difficulty: str = ""
    map_type: str = ""
    capital: tuple[int, int] | None = None
    my_military: int = 0
    tiles: list[dict] = field(default_factory=list)
    neighbors: list[_Neighbor] = field(default_factory=list)
    map_total: int = 0


def _find_strategy(key: str) -> strategy_space.CivStrategy | None:
    if not key:
        return None
    k = key.strip()
    for s in strategy_space.load_all().values():
        if k.upper() in (s.civ, s.leader) or k in (s.civ_cn, s.leader_cn):
            return s
    return None


def _parse_view(game_state: dict) -> _View:
    gs = game_state if isinstance(game_state, dict) else {}
    v = _View()
    v.civ_key = str(gs.get("civ") or gs.get("leader") or "")
    v.strat = _find_strategy(v.civ_key) or _find_strategy(str(gs.get("leader") or ""))
    try:
        v.turn = int(gs.get("turn") or 1)
    except (TypeError, ValueError):
        v.turn = 1
    v.difficulty = str(gs.get("difficulty") or "")
    v.map_type = str(gs.get("map_type") or "").upper()
    cap = gs.get("capital")
    try:
        if isinstance(cap, dict):
            v.capital = (int(cap["x"]), int(cap["y"]))
        elif isinstance(cap, (tuple, list)) and len(cap) >= 2:
            v.capital = (int(cap[0]), int(cap[1]))
    except (KeyError, TypeError, ValueError):
        v.capital = None
    try:
        v.my_military = int(gs.get("my_military") or 0)
    except (TypeError, ValueError):
        v.my_military = 0
    try:
        v.map_total = int(gs.get("map_total_tiles") or 0)
    except (TypeError, ValueError):
        v.map_total = 0
    v.tiles = [t for t in (gs.get("explored_tiles") or []) if isinstance(t, dict)]

    for n in gs.get("neighbors") or []:
        if not isinstance(n, dict):
            continue
        agendas = []
        for a in n.get("agendas") or []:
            name = a.get("name") if isinstance(a, dict) else a
            if isinstance(name, str) and name:
                agendas.append(name)
        dist = n.get("distance")
        if dist is None and v.capital and n.get("x") is not None:
            try:
                dist = hex_distance(
                    int(n["x"]), int(n["y"]), v.capital[0], v.capital[1]
                )
            except (TypeError, ValueError, KeyError):
                dist = None
        try:
            dist = int(dist) if dist is not None else None
        except (TypeError, ValueError):
            dist = None
        mil = n.get("military_strength")
        try:
            mil = int(mil) if mil is not None else None
        except (TypeError, ValueError):
            mil = None
        v.neighbors.append(
            _Neighbor(
                civ_name=str(n.get("civ_name") or n.get("name") or "未知文明"),
                leader_name=str(n.get("leader_name") or ""),
                distance=dist,
                military=mil,
                agendas=tuple(agendas),
            )
        )
    return v


# ---------------------------------------------------------------------------
# 地理分析(几何 → 事实;翻译成洞察在渲染层)
# ---------------------------------------------------------------------------


@dataclass
class _Geo:
    mountain_dir: str = ""  # 山脉群的主方向(≥3 格才算屏障)
    mountain_count: int = 0
    coast_dist: int | None = None  # 首都到最近海格的距离;None = 未见海
    river_capital: bool = False
    tundra_count: int = 0
    luxuries: list[tuple[str, int, int]] = field(default_factory=list)
    strategics: list[tuple[str, int, int]] = field(default_factory=list)
    resource_near: int = 0  # 首都 4 格内资源数
    iron_visible: tuple[int, int] | None = None
    iron_predicted: bool = False  # 丘陵无林 ≥2 格(预测,非事实)
    explored: int = 0


def _direction(dx: int, dy: int) -> str:
    """八向。坐标系:X+ = 东,Y+ = 南(AGENTS.md,勿反)。"""
    ew = "东" if dx > 0 else "西" if dx < 0 else ""
    ns = "南" if dy > 0 else "北" if dy < 0 else ""
    if abs(dx) > 2 * abs(dy):
        return ew
    if abs(dy) > 2 * abs(dx):
        return ns
    return (ew + ns) or ""


def _analyze_geography(view: _View) -> _Geo:
    g = _Geo()
    g.explored = len(view.tiles)
    cx, cy = view.capital or (0, 0)
    has_cap = view.capital is not None
    mdx = mdy = 0
    bare_hills = 0
    for t in view.tiles:
        terrain = str(t.get("terrain") or "")
        feature = str(t.get("feature") or "")
        try:
            x, y = int(t.get("x", 0)), int(t.get("y", 0))
        except (TypeError, ValueError):
            continue
        dist = hex_distance(x, y, cx, cy) if has_cap else 99
        if "_MOUNTAIN" in terrain:
            g.mountain_count += 1
            mdx += x - cx
            mdy += y - cy
        if "TUNDRA" in terrain:
            g.tundra_count += 1
        is_water = t.get("is_coastal") or terrain in ("TERRAIN_COAST", "TERRAIN_OCEAN")
        if is_water and has_cap:
            g.coast_dist = dist if g.coast_dist is None else min(g.coast_dist, dist)
        if t.get("is_river") and dist <= 1:
            g.river_capital = True
        res = str(t.get("resource") or "")
        if res:
            if dist <= 4:
                g.resource_near += 1
            cls = str(t.get("resource_class") or "").lower()
            if not cls:
                cls = _resource_info(res)[1]
            if cls == "luxury":
                g.luxuries.append((_resource_info(res)[0], x, y))
            elif cls == "strategic":
                g.strategics.append((_resource_info(res)[0], x, y))
            if res == "RESOURCE_IRON":
                g.iron_visible = (x, y)
        if "_HILLS" in terrain and not feature and not res and dist <= 6:
            bare_hills += 1
    if g.mountain_count >= 3 and has_cap:
        g.mountain_dir = _direction(
            round(mdx / g.mountain_count), round(mdy / g.mountain_count)
        )
    g.iron_predicted = g.iron_visible is None and bare_hills >= 2
    return g


def _geography_lines(g: _Geo, view: _View) -> list[str]:
    """几何事实 → 战略洞察的翻译。这一段是本模块存在的理由。"""
    out: list[str] = []
    if g.explored == 0:
        return ["地图:尚无已探明地块数据——先探图,地形结论全部悬置。"]
    if g.mountain_dir and g.mountain_count >= 3:
        out.append(
            f"首都{g.mountain_dir}面的连片山脉({g.mountain_count} 格)是天然城墙"
            f"——那一面不需驻军,兵力押在开阔面;山旁也是学院/圣地的相邻位。"
        )
    if g.coast_dist is None and g.explored >= 30:
        out.append(
            "已探明范围内没见到海——按内陆局准备,造船线整条跳过,直到探明海岸再议。"
        )
    elif g.coast_dist is not None and g.coast_dist >= 8:
        out.append(
            f"最近海岸约 {g.coast_dist} 格:短期没人能从海上来,海军押后,"
            f"省下的产能全进陆上。"
        )
    elif g.coast_dist is not None and g.coast_dist <= 2:
        out.append("首都临海:港口与出海探图都是现实选项,蛮族海路袭扰也要防。")
    if g.river_capital:
        out.append("首都沿河:住房无忧,商业中心的 +2 相邻已有着落。")
    if g.tundra_count >= 5:
        out.append(
            f"周边冻土 {g.tundra_count} 格——对多数文明是减分项,铺城前想清楚谁来种这些地。"
        )
    if g.luxuries:
        names = "、".join(f"{n}({x},{y})" for n, x, y in g.luxuries[:3])
        out.append(f"可见奢侈:{names}——宜居度的本钱,也是对外交易的筹码。")
    else:
        out.append("未见任何奢侈资源:宜居度得靠万神殿/娱乐/买入解决,扩张节奏受此约束。")
    strat_names = "、".join(f"{n}({x},{y})" for n, x, y in g.strategics[:2])
    if strat_names:
        out.append(f"可见战略资源:{strat_names}——先到先得,邻居也看得见它。")
    if g.iron_visible:
        out.append(
            f"铁已可见({g.iron_visible[0]},{g.iron_visible[1]}),军事升级线的硬门票在手。"
        )
    elif g.iron_predicted:
        out.append(
            "附近有无林丘陵群——铁的典型生成地形,大概率有铁"
            "(这是预测不是事实,青铜加工完成后立刻确认)。"
        )
    else:
        out.append("附近无铁特征地形(丘陵少或有林),铁的着落需探明后确认。")
    if view.map_total > 0:
        pct = g.explored / view.map_total * 100
        if pct < 3:
            out.append(
                f"探索度仅 {pct:.1f}%——上面每一条都可能被下一格推翻,探图本身就是情报投资。"
            )
    return out


# ---------------------------------------------------------------------------
# ① 文明能力利用率
# ---------------------------------------------------------------------------


def _analyze_civ_fit(view: _View, geo: _Geo) -> tuple[list[str], int]:
    s = view.strat
    if s is None:
        return [], 0
    plan = s.map_plans.get(view.map_type) or s.map_plans.get("DEFAULT")
    util = _CONF_UTIL.get(plan.confidence, 60) if plan else 60
    notes: list[str] = []
    # 文明特定的地形修正(小幅、可解释;数值是估计,措辞里不装精确)
    if s.civ == "CIVILIZATION_RUSSIA":
        if geo.tundra_count >= 5:
            util += 10
            notes.append("冻土盘吃满,+1 信 +1 锤对你是白送的地")
        elif geo.explored >= 30 and geo.tundra_count == 0:
            util -= 15
            notes.append("周边无冻土——Mother Russia 的地形增益这局吃不到")
        if view.map_type == "ARCHIPELAGO":
            util -= 10
            notes.append("哥萨克的本土 +5 在群岛难有用武之地,扣分全在军事端")
    elif s.civ == "CIVILIZATION_ROME":
        if geo.iron_visible or geo.iron_predicted:
            util += 5
        if view.map_type == "ARCHIPELAGO":
            notes.append("军团过不了海,修路能力也被海隔断")
    elif s.civ == "CIVILIZATION_GERMANY":
        if geo.resource_near >= 4:
            util += 5
            notes.append("首都圈资源簇厚,汉萨相邻直接拉满")
    util = max(20, min(95, util))

    first = f"你是{s.identity}"
    verdict = (
        "这局走你最擅长的路。"
        if util >= 80
        else "核心能力兑现打了折,路线要按本局重排——别按教科书打。"
        if util < 50
        else "能力大体可用,但别指望教科书式的完整兑现。"
    )
    line2 = (
        f"能力利用率约 {util}%"
        + (f"({';'.join(notes)})" if notes else "")
        + f"——{verdict}"
    )
    return [first, line2], util


# ---------------------------------------------------------------------------
# ③ 邻居
# ---------------------------------------------------------------------------


def _analyze_neighbors(view: _View) -> list[str]:
    if not view.neighbors:
        return [
            "尚未遇见其他文明。探索方向:向首都周围 6 格半径内未探明区域推进"
            "——邻居的位置比邻居的身份更早决定你的开局。"
        ]
    out: list[str] = []
    s = view.strat
    for n in view.neighbors[:2]:
        dist = f"约 {n.distance} 格外" if n.distance is not None else "距离未知"
        mil = ""
        if n.military is not None and view.my_military > 0:
            mil = f",军力 {n.military} 对你 {view.my_military}"
        head = f"{n.leader_name or n.civ_name}({n.civ_name})在{dist}{mil}"
        agenda = next((a for a in n.agendas if a in AGENDA_PROFILES), "")
        if agenda:
            p = AGENDA_PROFILES[agenda]
            head += (
                f":他看重{p.cares},{p.angers}会积怨 → {p.predicts}"
                f"(战争倾向:{p.war_risk})。"
            )
            lev = _AGENDA_LEVERAGE.get(agenda)
            if lev:
                out.append(head)
                out.append(f"反过来用:{lev}。")
            else:
                out.append(head)
        else:
            out.append(head + ":议程未知——按中性对待,先探他的边境布防再定调。")
        if s:
            play = next(
                (
                    np.play
                    for np in s.neighbor_plays
                    if agenda and agenda in np.matches_agendas
                ),
                "",
            )
            if play:
                out.append(f"你的应对(策略库):{play}")
    extra = len(view.neighbors) - 2
    if extra > 0:
        rest = "、".join(n.civ_name for n in view.neighbors[2:5])
        out.append(f"(另有 {extra} 个已知文明:{rest}——按遇见顺序补充评估)")
    return out


# ---------------------------------------------------------------------------
# ④ 路线推荐
# ---------------------------------------------------------------------------


def _victory_cn(token: str) -> str:
    p = VICTORY_PATHS.get((token or "").upper())
    return p.cn if p else token


def _has_capital_for(s: strategy_space.CivStrategy, victory: str) -> bool:
    """这个文明的已核材料里有没有该路线的本钱(关键词搜索)。"""
    blob = s.identity + " ".join(p.fact + p.derivation for p in s.principles)
    return any(k in blob for k in _VICTORY_KEYWORDS.get(victory, ()))


def _recommend_routes(view: _View, geo: _Geo) -> list[tuple[int, str]]:
    s = view.strat
    if s is None:
        return []
    out: list[tuple[int, str]] = []
    plan = s.map_plans.get(view.map_type) or s.map_plans.get("DEFAULT")
    if plan is None:
        return []
    pct = _CONF_PCT.get(plan.confidence, "约 65%")

    # —— 主推荐:论证链绑定本局事实 ——
    chain = [plan.reasoning.rstrip("。")]
    if plan.victory == "DOMINATION" and (geo.iron_visible or geo.iron_predicted):
        chain.append("铁的着落有谱")
    if (
        view.neighbors
        and view.neighbors[0].distance is not None
        and view.neighbors[0].distance <= 15
    ):
        chain.append(f"首个目标/威胁在 {view.neighbors[0].distance} 格内,可测可控")
    out.append(
        (0, f"推荐:{_victory_cn(plan.victory)}({pct})。论证:{';'.join(chain)}。")
    )
    if plan.opening:
        steps = ";".join(step.rstrip("。") for step in plan.opening[:3])
        out.append((0, f"前 30 回合:{steps}。"))
    if s.fallbacks:
        # 2026-08-02 校准局 P1 #1:trigger 是策略库预案的【触发条件】,
        # 不是已观测事实。此前直接 "风险:{trigger}" 把「若发生」透传成
        # 「已发生」—— T1 什么都没探明就输出"实机证实军团确实需要铁"。
        fb0 = s.fallbacks[0]
        trig = fb0.trigger.lstrip("若如果")
        out.append((0, f"风险(预案触发条件,T1 尚未观测,勿当事实):若{trig}。"))

    # —— 难度规则(命中才有)——
    if view.difficulty:
        from civ_mcp.params import DIFFICULTY_ORDER

        def _idx(d: str) -> int:
            try:
                return DIFFICULTY_ORDER.index(d.upper())
            except ValueError:
                return -1

        lvl = _idx(view.difficulty)
        hits = [r for r in s.difficulty_rules if lvl >= _idx(r.min_difficulty) >= 0]
        if hits:
            # 取门槛最高的那条:神级玩家要看的是神级规则,不是君主规则
            rule = max(hits, key=lambda r: _idx(r.min_difficulty))
            out.append((1, f"难度规则〔{rule.confidence}〕:{rule.rule}"))

    # —— 备选:fallback 原文,不硬解析。首条的 trigger 已在风险行点过,
    #    只展开 plan,避免同一句话连念两遍 ——
    if s.fallbacks:
        out.append((1, f"预案(约 60%):{s.fallbacks[0].plan}"))
    for fb in s.fallbacks[1:2]:
        out.append((1, f"备选(约 60%):{fb.trigger} → {fb.plan}"))

    # —— 不推荐:两轮扫描。先找"能力清单里完全缺席"的白板路线(30%),
    #    没有白板才点一条"有本钱但非主线"(45%)。理由从缺席里推,不编 ——
    candidates = [
        vt
        for vt in ("RELIGION", "DOMINATION", "CULTURE", "SCIENCE")
        if vt != plan.victory
    ]
    blank = next((vt for vt in candidates if not _has_capital_for(s, vt)), None)
    if blank:
        out.append(
            (
                2,
                f"不推荐:{_victory_cn(blank)}(约 30%)——已核能力清单里"
                f"没有一条与它相关,这条路你是白板。",
            )
        )
    elif candidates:
        out.append(
            (
                3,
                f"不推荐:{_victory_cn(candidates[0])}(约 45%)——有本钱"
                f"但排不上优先级,主线失败时再评估。",
            )
        )
    return out


# ---------------------------------------------------------------------------
# 行数控制:超了从低优先级压,不随机删
# ---------------------------------------------------------------------------


def _trim_to_35_lines(lines: list[tuple[int, str]], limit: int = MAX_LINES) -> str:
    """(优先级, 行) 列表 → 文本。超限时从优先级数字最大的行开始,从后往前删;
    0 级(标题/利用率/主推荐)永不删。"""
    work = list(lines)

    def count() -> int:
        return len(work)

    for prio in (3, 2, 1):
        while count() > limit:
            idx = next(
                (i for i in range(len(work) - 1, -1, -1) if work[i][0] == prio), None
            )
            if idx is None:
                break
            work.pop(idx)
    if count() > limit:
        work = work[:limit]  # 兜底,理论到不了
    return "\n".join(text for _, text in work)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def assess_opening(game_state: dict) -> str:
    """T1 开局评估:四段连贯叙事,≤35 行。任何错误降级,不抛异常。"""
    try:
        return _assess_opening_inner(game_state)
    except Exception:
        try:
            s = _parse_view(game_state).strat
            return f"你是{s.identity}" if s else ""
        except Exception:
            return ""


def _assess_opening_inner(game_state: dict) -> str:
    view = _parse_view(game_state)
    if view.strat is None:
        known = "、".join(strategy_space.list_civs())
        return (
            (
                f"策略库未收录「{view.civ_key}」(已收录:{known})。"
                f"按通用原则打:先探图,先确认邻居,再锁路线。"
            )
            if view.civ_key
            else ""
        )

    geo = _analyze_geography(view)
    fit_lines, _util = _analyze_civ_fit(view, geo)
    geo_lines = _geography_lines(geo, view)
    nb_lines = _analyze_neighbors(view)
    route_lines = _recommend_routes(view, geo)

    diff_cn = DIFFICULTY_CN.get(view.difficulty.upper(), "")
    head = (
        f"【开局评估】{view.strat.civ_cn}·{view.strat.leader_cn}"
        f" ‖ T{view.turn} ‖ {view.map_type or '地图未知'}"
        + (f" ‖ {diff_cn}" if diff_cn else "")
    )

    lines: list[tuple[int, str]] = [(0, head)]
    lines += [(0, fit_lines[0]), (0, fit_lines[1])] if len(fit_lines) >= 2 else []
    for i, ln in enumerate(geo_lines):
        lines.append((1 if i < 3 else 3, ln))
    for i, ln in enumerate(nb_lines):
        lines.append((1 if i < 4 else 3, ln))
    lines += route_lines
    return _trim_to_35_lines(lines)


def assess_midgame(game_state: dict, current_victory: str) -> str:
    """每 30 回合的路线复审:当前路线 vs 开局评估,是否切换。≤12 行。"""
    try:
        return _assess_midgame_inner(game_state, current_victory)
    except Exception:
        return ""


def _assess_midgame_inner(game_state: dict, current_victory: str) -> str:
    view = _parse_view(game_state)
    s = view.strat
    if s is None:
        return ""
    plan = s.map_plans.get(view.map_type) or s.map_plans.get("DEFAULT")
    if plan is None:
        return ""
    geo = _analyze_geography(view)
    _, util = _analyze_civ_fit(view, geo)
    cur = (current_victory or "").upper()
    rec = plan.victory.upper()

    out = [f"【路线复审】T{view.turn} ‖ {s.civ_cn}"]
    if cur == rec:
        metric = VICTORY_PATHS[rec].key_metric if rec in VICTORY_PATHS else ""
        out.append(
            f"你在走{_victory_cn(cur)},与开局评估一致——继续。"
            + (f"该盯的数:{metric}。" if metric else "")
        )
    else:
        out.append(
            f"你在走{_victory_cn(cur)},但开局评估对本图的推荐是"
            f"{_victory_cn(rec)}({_CONF_PCT.get(plan.confidence, '约 65%')})"
            f"——{plan.reasoning}"
        )
        out.append("是否切换,别凭感觉,按判据(满足任一才考虑切):")
        for fb in s.fallbacks[:2]:
            out.append(f"· {fb.trigger} → {fb.plan}")
    if util < 50:
        out.append(
            f"另:能力利用率降到约 {util}%——文明优势兑现不足,"
            f"当前路线是在裸奔,下一个 30 回合内必须给出改观。"
        )
    out.append(f"下一次复审:T{view.turn + 30}。")
    return "\n".join(out[:12])
