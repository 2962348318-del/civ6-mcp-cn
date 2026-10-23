"""L1 世界模型 —— 局内持久的"我知道的世界",只增不减。

为什么需要(改造方案 铁律二):
    每次查询是一次【观测】。观测更新世界模型,但世界模型【不因缺少观测而遗忘】。
    现状是"当前可见即全部",所以蛮族一进迷雾就蒸发,T28 才会误判"蛮族全清"。

2026-07-31 实测数据改写了这一层的设计:
    那局 52 回合只探明地图的 2.9%(98/3404 格),13 个蛮族营地【一个都没发现过】。
    所以问题不是"发现了营地却没记住",而是根本没去探。营地档案必须配一条
    前置的探索度预警,否则档案永远是空的。

只记录玩家【已探明】的信息。把迷雾里的营地喂给模型是作弊。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from civ_mcp.hexgrid import hex_distance
from civ_mcp.lua._helpers import SENTINEL


def storage_path(civ: str, seed: int) -> Path:
    """世界模型的落盘位置(写入用)。

    优先放【工作区】(CIV_MCP_WORKSPACE)—— 人能直接打开看、能 git、
    能整份拷给别的 AI 复盘。未设置时回退到用户目录。
    """
    ws = os.environ.get("CIV_MCP_WORKSPACE")
    base = Path(ws) if ws else Path.home() / ".civ6-mcp"
    return base / "世界模型" / f"{civ}_{seed}.json"


def candidate_paths(civ: str, seed: int) -> list[Path]:
    """同一局可能存在的【全部】世界模型位置,按写入优先级排列。

    2026-08-01 实机事故:同一局游戏被两个客户端先后接管 ——
    Hermes 拉起的进程没有 CIV_MCP_WORKSPACE(写用户目录),
    Claude Code 拉起的有(写工作区)。结果 T33 那回合读到空模型,
    整局 L1 历史(胜利路线/2 条计划/8 条失联/营地首见 T21)被覆盖成空。

    存储位置绝不该取决于"是谁启动的进程"。load_best() 因此会看遍
    所有候选位置,取内容最完整的那份 —— 铁律二的直接推论:
    观测(读到空文件)不能抹掉已知事实。
    """
    ws = os.environ.get("CIV_MCP_WORKSPACE")
    bases = []
    if ws:
        bases.append(Path(ws))
    bases.append(Path.home() / ".civ6-mcp")
    seen: set[str] = set()
    out: list[Path] = []
    for b in bases:
        p = b / "世界模型" / f"{civ}_{seed}.json"
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


# 探索度阈值。低于这个比例说明视野严重不足 —— 你不知道威胁从哪来,
# 也不知道该往哪扩张。数值参考:失败那局 T52 时是 2.9%。
EXPLORATION_POOR = 0.08
EXPLORATION_LOW = 0.15


@dataclass
class Camp:
    """已探明的蛮族营地。一旦发现就永久记住,直到确认拆除。"""

    x: int
    y: int
    first_seen_turn: int
    last_confirmed_turn: int
    dist_to_capital: int = -1
    destroyed: bool = False

    @property
    def key(self) -> str:
        return f"{self.x},{self.y}"


@dataclass
class Contact:
    """失联的敌方单位。来自 end_turn 的 LOST CONTACT 事件。"""

    key: str
    unit_type: str
    owner: str
    last_x: int
    last_y: int
    last_turn: int
    hp: str = ""
    fled_dir: str = ""
    moves: int = 2
    reacquired: bool = False

    def guess_radius(self, current_turn: int) -> int:
        """推测半径 = 失联回合数 × 移动力。"""
        return max(0, (current_turn - self.last_turn)) * max(1, self.moves)


@dataclass
class DeathTile:
    """损失过单位的格子。同一格死两次是复盘里的真实情况。"""

    x: int
    y: int
    victims: list[str] = field(default_factory=list)
    turns: list[int] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.x},{self.y}"


@dataclass
class Plan:
    """L2 计划链的一环。

    完成条件必须是【服务端可判定的谓词】,不是模型自评 —— 模型说"做完了"
    不算数,数字说了算。cond == "manual" 是唯一例外(模型用 plan_done 标记),
    用于无法用数字表达的目标。

    治的问题(用户原话):"一个计划完成就忘记继续下一个计划"。
    完成即自动激活后继,后继每回合出现在简报里,模型不需要记得。
    """

    id: int
    goal: str  # 中文目标描述,如 "拆除营地(9,33)"
    cond: str = "manual"  # 条件类型,见 evaluate_plans
    args: str = ""  # 条件参数,如 "9,33" / "4" / "TECH_ARCHERY"
    status: str = "active"  # active / pending(等前置) / done
    after: int = -1  # 前置计划 id,-1 表示无
    created_turn: int = 0
    done_turn: int = -1


@dataclass
class WorldModel:
    """一局游戏的持久世界状态。"""

    game_id: str = ""
    current_turn: int = 0
    camps: dict[str, Camp] = field(default_factory=dict)
    contacts: dict[str, Contact] = field(default_factory=dict)
    death_tiles: dict[str, DeathTile] = field(default_factory=dict)
    # 探索度
    tiles_total: int = 0
    tiles_revealed: int = 0
    # L2 计划链
    plans: list[Plan] = field(default_factory=list)
    next_plan_id: int = 1
    # L3 胜利路线(模型开局锁定,之后偏离会被点名)
    victory_type: str = ""  # SCIENCE / CULTURE / RELIGION / DOMINATION
    victory_reason: str = ""
    victory_locked_turn: int = -1
    # L3 威胁仲裁(每回合重算,红色时压制发展决策)
    threat_level: str = "GREEN"  # RED / YELLOW / GREEN
    threat_reason: str = ""
    # 每回合缓存的帝国概况(供拦截/偏离检查用,避免动作时再发查询)
    city_count: int = 0
    military_count: int = 0
    # 真实军力值(与 get_victory_progress 同源)。2026-08-02 校准局 1e:
    # 此前军力排名/危机/联合宣战全都拿 military_count(单位数)当军力,
    # 把 0.82 倍的差距报成 20 倍。
    military_strength: int = 0
    # 本文明 UU → 被替换基础单位(0c 接线,喂 tech_path 军事主线)
    my_unique_units: dict[str, str] = field(default_factory=dict)
    # 总人口 + 已完成科技(2026-08-03 实机修复)。
    # 人口:L1 分档第七维,此前我方无来源,恒报「人口 0.00×」的假劣势。
    # 科技集合:get_strategy_notes 判断 UU「已解锁 / 还需某科技」要用它,
    # 此前没传,导致 T110 场上已有罗马军团却仍显示「⏳ 需 IRON_WORKING」。
    my_population: int = 0
    researched_techs: list[str] = field(default_factory=list)
    # 本回合可见威胁的坐标与可达半径 [[x, y, reach, 名称], ...],
    # 以及各城坐标 {city_id: [x, y]}。两者都用于把「威胁等级红色」
    # 这个**全局**判断落到**具体城市**上 —— 2026-08-03 实机:罗马距那个
    # 蛮族 12 格,却因为侦察兵离它 3 格而被拦住生产建造者。
    threat_positions: list = field(default_factory=list)
    city_positions: dict = field(default_factory=dict)

    def threat_distance_to(self, city_id: int) -> int | None:
        """该城到最近威胁的距离。任一数据缺失返回 None(调用方按未知处理)。"""
        pos = (self.city_positions or {}).get(str(city_id)) or (
            self.city_positions or {}
        ).get(city_id)
        if not pos or not self.threat_positions:
            return None
        try:
            cx, cy = int(pos[0]), int(pos[1])
        except (TypeError, ValueError, IndexError):
            return None
        best: int | None = None
        for t in self.threat_positions:
            try:
                d = hex_distance(int(t[0]), int(t[1]), cx, cy)
            except (TypeError, ValueError, IndexError):
                continue
            if best is None or d < best:
                best = d
        return best

    # 局面元数据(开局查一次即可,用于难度/地图参数化)
    difficulty: str = ""
    map_type: str = ""
    # 中后期用的产出缓存
    total_production: int = 0
    my_science: int = 0
    has_spaceport: bool = False
    completed_projects: list[str] = field(default_factory=list)
    # 上回合我方单位快照 {完整ID: "类型|x|y"},用于死亡检测。
    # 2026-07-31 实测教训:record_death 定义了却从未接线,导致复盘里
    # "损失单位 0" —— 而那局明明死了侦察兵。事实层错了比没有更糟。
    last_units: dict[str, str] = field(default_factory=dict)

    @property
    def exploration_pct(self) -> float:
        if not self.tiles_total:
            return 0.0
        return self.tiles_revealed / self.tiles_total

    def active_camps(self) -> list[Camp]:
        return [c for c in self.camps.values() if not c.destroyed]

    def stale_contacts(self) -> list[Contact]:
        return [c for c in self.contacts.values() if not c.reacquired]

    # -- 更新 ---------------------------------------------------------------

    def observe_camp(self, x: int, y: int, turn: int, dist: int = -1) -> bool:
        """记录一个已探明的营地。返回 True 表示这是新发现。"""
        key = f"{x},{y}"
        if key in self.camps:
            self.camps[key].last_confirmed_turn = turn
            self.camps[key].destroyed = False
            return False
        self.camps[key] = Camp(x, y, turn, turn, dist)
        return True

    def apply_camp_checks(
        self, checks: list[tuple[int, int, bool, bool]]
    ) -> list[Camp]:
        """按"亲眼确认"原则更新营地存亡。返回本次确认拆除的营地。

        只有该格【当前可见】且【没有营地】才判定拆除;
        看不见的一律维持原状(铁律二:观测缺失 ≠ 事实消失)。
        """
        gone = []
        for x, y, visible, still_camp in checks:
            camp = self.camps.get(f"{x},{y}")
            if camp is None or camp.destroyed:
                continue
            if visible and not still_camp:
                camp.destroyed = True
                gone.append(camp)
        return gone

    def record_lost_contact(
        self,
        unit_type: str,
        owner: str,
        x: int,
        y: int,
        turn: int,
        hp: str = "",
        fled_dir: str = "",
        moves: int = 2,
    ) -> None:
        key = f"{owner}:{unit_type}:{x},{y}:{turn}"
        self.contacts[key] = Contact(
            key, unit_type, owner, x, y, turn, hp, fled_dir, moves
        )

    def record_death(self, x: int, y: int, victim: str, turn: int) -> None:
        key = f"{x},{y}"
        t = self.death_tiles.setdefault(key, DeathTile(x, y))
        t.victims.append(victim)
        t.turns.append(turn)

    def diff_units(
        self,
        current: dict[str, str],
        turn: int,
        exclude: set[tuple[int, int]] | None = None,
    ) -> list[tuple[str, int, int]]:
        """对比上回合快照,找出消失的单位并记为死亡。

        返回 [(单位类型, x, y)]。第一回合(无快照)返回空,避免误报。

        为什么用快照差分而不是捕获死亡事件:游戏没有可靠的死亡通知,
        而"上回合有、这回合没有"是确定性的。代价是无法区分
        "阵亡"和"升级/消耗"(如开拓者建城、建造者耗尽充能)——
        所以只对【军事单位】计死亡,平民消失可能是正常消耗。

        2026-08-01 实机误报:T34 把 (55,11) 的投石兵升级成弓箭手,升级会
        换掉 unit_id,于是旧 ID 消失被判成阵亡,简报报了"UNIT_SLINGER 在
        (55,11) 被杀"并把该格写进死亡格档案 —— 假警报比没警报更糟,
        它会让模型绕开一块其实安全的地。
        判据补一条:**旧单位原地若仍站着我方单位,就是升级/编队而非阵亡**。
        升级后的单位留在原格(升级消耗移动力,走不掉),阵亡后那格则由敌人
        占据或空着 —— 这个区分是可靠的。

        2026-08-05: 新增 exclude 参数 —— 主动解散（delete）的单位由 Lua 侧
        ExposedMembers.MCPDeletedUnits 记录位置，end_turn 传入后跳过，
        不再报"被杀"、不进死亡格档案（本局实机：解散 5 个弩手/加农炮
        全被报成阵亡，误导战术复盘）。
        """
        lost: list[tuple[str, int, int]] = []
        skip = exclude or set()
        if not self.last_units:
            self.last_units = dict(current)
            return lost
        # 本回合我方单位所在的格 —— 用于识别"原地换 ID"= 升级
        occupied: set[tuple[int, int]] = set()
        for info in current.values():
            p = info.split("|")
            if len(p) >= 3:
                try:
                    occupied.add((int(p[1]), int(p[2])))
                except ValueError:
                    continue
        for uid, info in self.last_units.items():
            if uid in current:
                continue
            parts = info.split("|")
            if len(parts) < 4:
                continue
            utype, sx, sy, is_mil = parts[0], parts[1], parts[2], parts[3]
            if is_mil != "1":
                continue  # 平民消失多半是建城/耗尽充能,不算阵亡
            try:
                x, y = int(sx), int(sy)
            except ValueError:
                continue
            if (x, y) in skip:
                continue  # 主动解散（delete），不是阵亡
            if (x, y) in occupied:
                continue  # 原地仍有我方单位 → 升级/编队,不是阵亡
            self.record_death(x, y, utype, turn)
            lost.append((utype, x, y))
        self.last_units = dict(current)
        return lost

    def reacquire_contacts(self, visible: set[str]) -> int:
        """把重新看见的失联单位销档。visible 为 "类型:x,y" 集合。

        实测教训:初版从不销档,29 回合下来积了 8 条全是 reacquired=false,
        Hermes 反馈"T20 之后全是噪声"。看见了就该销,这是铁律二的另一半 ——
        观测缺失不代表消失,但观测到了就该更新。
        """
        n = 0
        for c in self.contacts.values():
            if c.reacquired:
                continue
            if f"{c.unit_type}:{c.last_x},{c.last_y}" in visible:
                c.reacquired = True
                n += 1
        return n

    def reacquire_by_sightings(
        self, sightings: list[tuple[str, int, int]], turn: int
    ) -> int:
        """按【同类型 + 在推测半径内】销档失联条目。

        2026-08-02 校准局 5c:精确匹配 "类型:x,y" 只能销掉原地不动的单位,
        而失联单位几乎都在移动 —— T11 失联于 (36,43) 的侦察兵 T13 在
        (32,42) 重新出现,条目却挂到半径膨胀成 16。凡当前视野里有同类型
        单位落在该条目的推测圈内,即视为同一个单位,销档。
        (同类型多单位时可能误销 —— 代价是少一条推测,可接受;
        不销的代价是虚构一个不存在的威胁,更糟。)
        """
        n = 0
        for c in self.contacts.values():
            if c.reacquired:
                continue
            r = c.guess_radius(turn) + 1
            for utype, x, y in sightings:
                if utype != c.unit_type:
                    continue
                if hex_distance(x, y, c.last_x, c.last_y) <= r:
                    c.reacquired = True
                    n += 1
                    break
        return n

    def prune_contacts(self, current_turn: int, max_age: int = 15) -> None:
        """太久远的失联记录没有情报价值,清掉以免简报越滚越长。"""
        self.contacts = {
            k: c
            for k, c in self.contacts.items()
            if current_turn - c.last_turn <= max_age
        }

    # -- L2 计划链 -----------------------------------------------------------

    def add_plan(
        self,
        goal: str,
        cond: str = "manual",
        args: str = "",
        after: int = -1,
        turn: int = 0,
    ) -> Plan:
        status = "pending" if after >= 0 else "active"
        p = Plan(self.next_plan_id, goal, cond, args, status, after, turn)
        self.next_plan_id += 1
        self.plans.append(p)
        return p

    def evaluate_plans(self, facts: dict, turn: int) -> list[Plan]:
        """用服务端事实判定计划完成,自动激活后继。返回本回合新完成的计划。

        facts 键(全部由 end_turn 的确定性查询提供):
            cities: int            military: int
            exploration: float     destroyed_camps: set[str]  ("x,y")
            techs: set[str]        (已完成科技的 TECH_XXX)
        """
        done_ids = {p.id for p in self.plans if p.status == "done"}
        newly: list[Plan] = []
        for p in self.plans:
            if p.status == "pending" and (p.after in done_ids or p.after < 0):
                p.status = "active"
            # 2026-08-02 校准局 1c 追加:pending 的计划也参与条件判定 ——
            # 目标达成就是达成,与前置顺序无关。此前只判 active,
            # 于是「铺出第二城」在城已建成 7 回合后仍显示等待前置,
            # 与卡死的前置叠加成 65 回合零推进。manual 仍只能人工标记。
            if p.status not in ("active", "pending"):
                continue
            if p.cond == "manual" and p.status == "pending":
                continue
            ok = False
            try:
                if p.cond == "cities_at_least":
                    ok = facts.get("cities", 0) >= int(p.args)
                elif p.cond == "military_at_least":
                    ok = facts.get("military", 0) >= int(p.args)
                elif p.cond == "camp_destroyed":
                    ok = p.args in facts.get("destroyed_camps", set())
                elif p.cond == "exploration_at_least":
                    ok = facts.get("exploration", 0.0) >= float(p.args)
                elif p.cond == "tech_completed":
                    ok = p.args in facts.get("techs", set())
                # "manual" 只能由 plan_done 工具标记
            except (ValueError, TypeError):
                ok = False  # 条件参数损坏 → 永不自动完成,但也不崩
            if ok:
                p.status = "done"
                p.done_turn = turn
                done_ids.add(p.id)
                newly.append(p)
        # 二次扫描:本回合完成的计划可能解锁后继
        for p in self.plans:
            if p.status == "pending" and p.after in done_ids:
                p.status = "active"
        return newly

    def active_plans(self) -> list[Plan]:
        return [p for p in self.plans if p.status == "active"]

    def pending_plans(self) -> list[Plan]:
        return [p for p in self.plans if p.status == "pending"]

    # -- 持久化 -------------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "game_id": self.game_id,
                "current_turn": self.current_turn,
                "tiles_total": self.tiles_total,
                "tiles_revealed": self.tiles_revealed,
                "camps": [asdict(c) for c in self.camps.values()],
                "contacts": [asdict(c) for c in self.contacts.values()],
                "death_tiles": [asdict(t) for t in self.death_tiles.values()],
                "plans": [asdict(p) for p in self.plans],
                "next_plan_id": self.next_plan_id,
                "victory_type": self.victory_type,
                "victory_reason": self.victory_reason,
                "victory_locked_turn": self.victory_locked_turn,
                "threat_level": self.threat_level,
                "threat_reason": self.threat_reason,
                "city_count": self.city_count,
                "military_count": self.military_count,
                "military_strength": self.military_strength,
                "my_unique_units": self.my_unique_units,
                "my_population": self.my_population,
                "researched_techs": self.researched_techs,
                "threat_positions": self.threat_positions,
                "city_positions": self.city_positions,
                "difficulty": self.difficulty,
                "map_type": self.map_type,
                "total_production": self.total_production,
                "my_science": self.my_science,
                "has_spaceport": self.has_spaceport,
                "completed_projects": self.completed_projects,
                # 2026-08-01:此前 last_units 定义了、diff_units 也会更新它,
                # 但序列化里漏了 —— 于是每回合加载回来都是空的,死亡检测
                # 永远停在"首次初始化"分支。实机抓到:建造者阵亡而
                # death_tiles 仍为空。快照必须跨回合持久,差分才有基准。
                "last_units": self.last_units,
            },
            ensure_ascii=False,
            indent=1,
        )

    @classmethod
    def from_json(cls, text: str) -> WorldModel:
        try:
            d = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return cls()
        wm = cls(
            game_id=d.get("game_id", ""),
            current_turn=d.get("current_turn", 0),
            tiles_total=d.get("tiles_total", 0),
            tiles_revealed=d.get("tiles_revealed", 0),
            next_plan_id=d.get("next_plan_id", 1),
            victory_type=d.get("victory_type", ""),
            victory_reason=d.get("victory_reason", ""),
            victory_locked_turn=d.get("victory_locked_turn", -1),
            threat_level=d.get("threat_level", "GREEN"),
            threat_reason=d.get("threat_reason", ""),
            city_count=d.get("city_count", 0),
            military_count=d.get("military_count", 0),
            difficulty=d.get("difficulty", ""),
            map_type=d.get("map_type", ""),
            total_production=d.get("total_production", 0),
            my_science=d.get("my_science", 0),
            has_spaceport=d.get("has_spaceport", False),
            completed_projects=d.get("completed_projects", []),
            military_strength=d.get("military_strength", 0),
            my_unique_units={
                str(k): str(v) for k, v in (d.get("my_unique_units") or {}).items()
            },
            my_population=d.get("my_population", 0),
            researched_techs=[str(t) for t in (d.get("researched_techs") or [])],
            threat_positions=list(d.get("threat_positions") or []),
            city_positions=dict(d.get("city_positions") or {}),
            last_units={str(k): str(v) for k, v in (d.get("last_units") or {}).items()},
        )
        for c in d.get("camps", []):
            wm.camps[f"{c['x']},{c['y']}"] = Camp(**c)
        for c in d.get("contacts", []):
            wm.contacts[c["key"]] = Contact(**c)
        for t in d.get("death_tiles", []):
            wm.death_tiles[f"{t['x']},{t['y']}"] = DeathTile(**t)
        for p in d.get("plans", []):
            wm.plans.append(Plan(**p))
        return wm

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> WorldModel:
        if not path.exists():
            return cls()
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except OSError:
            return cls()

    def richness(self) -> tuple:
        """内容完整度评分,用于在多个候选文件之间挑最有信息量的那份。

        排序键刻意把【不可重建的历史】排在前面:营地档案与计划链是
        只增不减的长期记忆,回合数反而是最容易被空模型顶掉的字段
        (空模型每回合都会把 current_turn 写成最新值)。
        """
        return (
            len(self.camps),
            len(self.plans),
            len(self.contacts),
            len(self.death_tiles),
            1 if self.victory_type else 0,
            self.current_turn,
        )

    @classmethod
    def load_best(cls, civ: str, seed: int) -> tuple[WorldModel, Path]:
        """跨所有候选位置加载,取内容最完整的一份 → (模型, 它的来源路径)。

        修的是 2026-08-01 的实机事故:换个客户端启动(环境变量不同)就把
        整局 L1 历史读成空。详见 candidate_paths 的说明。
        """
        paths = candidate_paths(civ, seed)
        best: WorldModel | None = None
        best_path = paths[0]
        for p in paths:
            if not p.exists():
                continue
            wm = cls.load(p)
            if best is None or wm.richness() > best.richness():
                best, best_path = wm, p
        return (best if best is not None else cls()), best_path


# ---------------------------------------------------------------------------
# 观测:一次 Lua 查询取回营地 + 探索度
# ---------------------------------------------------------------------------


def build_world_scan_query() -> str:
    """扫描已探明的蛮族营地,并统计探索度。

    ⚠️ 只报告 IsRevealed 为真的营地。迷雾里的营地属于玩家不知道的信息,
    喂给模型就是作弊。2026-07-31 实测:失败那局 13 个营地探明 0 个。
    """
    return f"""
local me = Game.GetLocalPlayer()
local pVis = PlayersVisibility[me]
local capX, capY = -1, -1
for _, c in Players[me]:GetCities():Members() do capX, capY = c:GetX(), c:GetY() break end
local total, revealed = 0, 0
for i = 0, Map.GetPlotCount() - 1 do
  local plot = Map.GetPlotByIndex(i)
  if plot ~= nil then
    -- 2026-08-02 校准局 P0 1c:此前分母含海洋(全图格数),而 overview 用
    -- 陆地口径 —— 同一回合一个报 15%、一个报 6.6%,计划链的 8% 门槛用了
    -- 错误口径导致 65 回合零推进。统一为【陆地】口径:营地/邻居/扩张
    -- 这些探索的意义全在陆地上。
    local isLand = not plot:IsWater()
    if isLand then total = total + 1 end
    local x, y = plot:GetX(), plot:GetY()
    local isRev = pVis:IsRevealed(x, y)
    if isRev then
      if isLand then revealed = revealed + 1 end
      local imp = plot:GetImprovementType()
      if imp ~= -1 then
        local row = GameInfo.Improvements[imp]
        if row and row.BarbarianCamp then
          local d = -1
          if capX >= 0 then d = Map.GetPlotDistance(x, y, capX, capY) end
          print("CAMP|" .. x .. "|" .. y .. "|" .. d)
        end
      end
    end
  end
end
print("EXPLORE|" .. revealed .. "|" .. total)
local cities, mil, prod = 0, 0, 0
local spaceport = false
for _, c in Players[me]:GetCities():Members() do
  cities = cities + 1
  local ok, y = pcall(function() return c:GetYield(YieldTypes.PRODUCTION) end)
  if ok and y then prod = prod + y end
  -- 太空港判定。城市的区域集合接口在不同上下文里形态不一致
  -- (实测 GetDistricts() 返回的对象没有 Members()),所以整段 pcall,
  -- 拿不到就当没有 —— 这个字段只用于提示,不承担拦截职责。
  pcall(function()
    local ds = c:GetDistricts()
    if ds == nil then return end
    if ds.Members ~= nil then
      for _, d in ds:Members() do
        local di = GameInfo.Districts[d:GetType()]
        if di and di.DistrictType == "DISTRICT_SPACEPORT" then spaceport = true end
      end
    elseif ds.GetNumDistricts ~= nil then
      for i = 0, ds:GetNumDistricts() - 1 do
        local d = ds:GetDistrictByIndex(i)
        if d ~= nil then
          local di = GameInfo.Districts[d:GetType()]
          if di and di.DistrictType == "DISTRICT_SPACEPORT" then spaceport = true end
        end
      end
    end
  end)
end
for _, u in Players[me]:GetUnits():Members() do
  local ui = GameInfo.Units[u:GetType()]
  if ui and (ui.Combat or 0) > 0 and u:GetX() ~= -9999 then mil = mil + 1 end
  -- 单位快照(2026-08-01 接线):喂 diff_units 做死亡检测,0 新增往返。
  -- ID 用完整 GetID(),不取模 —— 单位串号的教训。
  if ui and u:GetX() ~= -9999 then
    print("UNIT|" .. u:GetID() .. "|" .. ui.UnitType .. "|" .. u:GetX()
          .. "|" .. u:GetY() .. "|" .. (((ui.Combat or 0) > 0) and 1 or 0))
  end
end
print("STATS|" .. cities .. "|" .. mil)
-- 总人口(2026-08-03 实机修复):L1 分档的七个维度里,对手人口来自
-- RivalSnapshot,而我方人口此前没有任何来源 —— 结果分档里恒显示
-- 「人口 0.00×」,是一个凭空捏造的劣势。人口和城市数同一个循环里就能数,
-- 与其它维度同源,零新增往返。
local totalPop = 0
for _, c in Players[me]:GetCities():Members() do
  totalPop = totalPop + c:GetPopulation()
end
print("POP|" .. totalPop)
-- 真实军力值(与胜利快照同源,1e):military_count 是单位数,不是军力
pcall(function()
  print("MILSTR|" .. math.floor(Players[me]:GetStats():GetMilitaryStrength()))
end)
local sci = 0
pcall(function() sci = math.floor(Players[me]:GetTechs():GetScienceYield()) end)
print("YIELD|" .. math.floor(prod) .. "|" .. sci .. "|" .. tostring(spaceport))
local cfg = PlayerConfigurations[me]
local diff = ""
pcall(function()
  local h = cfg:GetHandicapTypeID()
  for r in GameInfo.Difficulties() do
    if r.Hash == h then diff = r.DifficultyType end
  end
end)
print("META|" .. diff)
local techs = Players[me]:GetTechs()
for r in GameInfo.Technologies() do
  if techs:HasTech(r.Index) then print("TECH|" .. r.TechnologyType) end
end
-- 本文明的独特单位映射(2026-08-02 接线,0c):UU → 被替换的基础单位。
-- 引擎侧 CivilizationTraits + UnitReplaces 是权威;喂给 tech_path 让
-- 军事升级主线认得军团这类 UU,不再只推荐基础单位。
pcall(function()
  local myCiv = PlayerConfigurations[me]:GetCivilizationTypeName()
  local traits = {{}}
  for row in GameInfo.CivilizationTraits() do
    if row.CivilizationType == myCiv then traits[row.TraitType] = true end
  end
  for row in GameInfo.UnitReplaces() do
    local uu = GameInfo.Units[row.CivUniqueUnitType]
    if uu and uu.TraitType and traits[uu.TraitType] then
      print("UU|" .. row.CivUniqueUnitType .. "|" .. row.ReplacesUnitType)
    end
  end
end)
-- 已完成市政 + 文化产出(2026-08-01 接线扩展:tech_path 的市政侧和
-- boost_planner 需要;与 TECH 循环同构,同一往返 0 新增成本)
local cul = 0
pcall(function() cul = math.floor(Players[me]:GetCulture():GetCultureYield()) end)
print("CULTURE|" .. cul)
pcall(function()
  local pc = Players[me]:GetCulture()
  for r in GameInfo.Civics() do
    if pc:HasCivic(r.Index) then print("CIVIC|" .. r.CivicType) end
  end
end)
print("{SENTINEL}")
"""


@dataclass
class WorldScan:
    """一次世界扫描的解析结果。"""

    camps: list[tuple[int, int, int]] = field(default_factory=list)
    revealed: int = 0
    total: int = 0
    cities: int = 0
    military: int = 0
    techs: set[str] = field(default_factory=set)
    production: int = 0
    science: int = 0
    has_spaceport: bool = False
    difficulty: str = ""
    civics: set[str] = field(default_factory=set)  # 2026-08-01 接线扩展
    culture: int = 0
    units: dict[str, str] = field(default_factory=dict)  # uid → "type|x|y|is_mil"
    unique_units: dict[str, str] = field(default_factory=dict)  # UU → 被替换基础单位
    military_strength: int = 0  # 真实军力值(GetMilitaryStrength,非单位数)
    population: int = 0  # 总人口(L1 分档的第七个维度)


def parse_world_scan(lines: list[str]) -> WorldScan:
    scan = WorldScan()
    for line in lines:
        if line.startswith("CAMP|"):
            p = line.split("|")
            try:
                scan.camps.append((int(p[1]), int(p[2]), int(p[3])))
            except (ValueError, IndexError):
                continue
        elif line.startswith("EXPLORE|"):
            p = line.split("|")
            try:
                scan.revealed, scan.total = int(p[1]), int(p[2])
            except (ValueError, IndexError):
                pass
        elif line.startswith("STATS|"):
            p = line.split("|")
            try:
                scan.cities, scan.military = int(p[1]), int(p[2])
            except (ValueError, IndexError):
                pass
        elif line.startswith("TECH|"):
            scan.techs.add(line.split("|", 1)[1])
        elif line.startswith("CIVIC|"):
            scan.civics.add(line.split("|", 1)[1])
        elif line.startswith("UNIT|"):
            p = line.split("|")
            if len(p) >= 6:
                scan.units[p[1]] = f"{p[2]}|{p[3]}|{p[4]}|{p[5]}"
        elif line.startswith("MILSTR|"):
            try:
                scan.military_strength = int(float(line.split("|", 1)[1]))
            except (ValueError, IndexError):
                pass
        elif line.startswith("POP|"):
            try:
                scan.population = int(float(line.split("|", 1)[1]))
            except (ValueError, IndexError):
                pass
        elif line.startswith("UU|"):
            p = line.split("|")
            if len(p) >= 3 and p[1] and p[2]:
                scan.unique_units[p[1]] = p[2]
        elif line.startswith("CULTURE|"):
            try:
                scan.culture = int(line.split("|", 1)[1])
            except ValueError:
                pass
        elif line.startswith("YIELD|"):
            p = line.split("|")
            try:
                scan.production, scan.science = int(p[1]), int(p[2])
                scan.has_spaceport = len(p) > 3 and p[3] == "true"
            except (ValueError, IndexError):
                pass
        elif line.startswith("META|"):
            scan.difficulty = line.split("|", 1)[1].strip()
    return scan


def build_camp_check_query(coords: list[tuple[int, int]]) -> str:
    """检查一批已知营地坐标:当前是否可见、是否仍有营地。

    用于拆除判定 —— 只有【亲眼看到那格没营地了】才算拆除,
    看不见的一律维持"未拆除"(铁律二:观测缺失 ≠ 事实消失)。
    """
    checks = []
    for x, y in coords[:20]:
        checks.append(
            f"do local x, y = {x}, {y} "
            f"local vis = pVis:IsVisible(x, y) "
            f"local camp = false "
            f"if vis then local plot = Map.GetPlot(x, y) "
            f"if plot ~= nil then local imp = plot:GetImprovementType() "
            f"if imp ~= -1 then local row = GameInfo.Improvements[imp] "
            f"camp = (row and row.BarbarianCamp) or false end end end "
            f'print("CHK|" .. x .. "|" .. y .. "|" .. tostring(vis) .. "|" .. tostring(camp)) end'
        )
    body = " ".join(checks)
    return (
        "local me = Game.GetLocalPlayer() "
        "local pVis = PlayersVisibility[me] "
        f"{body} "
        f'print("{SENTINEL}")'
    )


def parse_camp_check(lines: list[str]) -> list[tuple[int, int, bool, bool]]:
    """返回 [(x, y, 可见, 仍有营地)]。"""
    out = []
    for line in lines:
        if line.startswith("CHK|"):
            p = line.split("|")
            try:
                out.append((int(p[1]), int(p[2]), p[3] == "true", p[4] == "true"))
            except (ValueError, IndexError):
                continue
    return out


# ---------------------------------------------------------------------------
# L3 威胁仲裁 —— 确定性红黄绿,不交给模型判断(铁律三)
# ---------------------------------------------------------------------------
# 复盘的根本教训:蛮族围城了还在种田。不是模型不知道有威胁(THREAT 事件
# 每回合都在报),是没有一个机制把"现在必须先处理这个"钉在决策入口上。

# 判定阈值。RED 的含义:再不处理就要死单位/丢城了。
THREAT_CLOSE = 3  # 敌军距我方城市/单位 ≤3 格 → 一回合可达攻击位
CAMP_NEAR_CAPITAL = 6  # 营地距首都 ≤6 → 出的兵两三回合就到家门口


def compute_threat_level(wm: WorldModel, threats: list) -> tuple[str, str]:
    """返回 (等级, 中文理由)。threats 为鸭子类型:.distance/.combat_strength/.owner_name。"""

    # 2026-08-04 P1:海军威胁不能与陆地威胁同等对待 —— GALLEY 在海岸
    # 游走曾反复触发 RED 冻结平民生产,但悬崖 LOS 让它实际打不到城。
    # 海军只有「我方有沿海城市 且 距最近城市 <= 攻击可达(近战1格/远程射程+1)」
    # 才算 close;否则最多进 YELLOW 计数。
    def _naval_reach(t) -> int | None:
        """海军能触达最近我方城市的距离;不能触达返回 None。"""
        if not getattr(t, "is_naval", False):
            return None
        if not getattr(t, "has_coastal_city", False):
            return None
        cd = getattr(t, "city_distance", 999)
        rng = getattr(t, "range", 0) or 0
        return cd if cd <= rng + 1 else None

    def _is_close(t) -> bool:
        if getattr(t, "is_naval", False):
            return _naval_reach(t) is not None
        return getattr(t, "distance", 99) <= THREAT_CLOSE

    close = [t for t in threats if _is_close(t)]
    near_camps = [
        c for c in wm.active_camps() if 0 <= c.dist_to_capital <= CAMP_NEAR_CAPITAL
    ]
    if close:
        strongest = max(close, key=lambda t: getattr(t, "combat_strength", 0))
        return "RED", (
            f"{getattr(strongest, 'owner_name', '敌方')}军事单位已进至 "
            f"{getattr(strongest, 'distance', '?')} 格内"
            + (
                f",且营地 ({near_camps[0].x},{near_camps[0].y}) 仍在持续出兵"
                if near_camps
                else ""
            )
        )
    if near_camps and threats:
        c = near_camps[0]
        return "RED", (
            f"营地 ({c.x},{c.y}) 距首都 {c.dist_to_capital} 格且视野内有敌军 —— "
            f"不拆营地,兵会一直来"
        )
    if near_camps or threats or wm.active_camps():
        if near_camps:
            c = near_camps[0]
            return "YELLOW", f"营地 ({c.x},{c.y}) 距首都 {c.dist_to_capital} 格"
        if threats:
            return "YELLOW", f"视野内有 {len(threats)} 个敌方军事单位"
        c = wm.active_camps()[0]
        return "YELLOW", f"已知营地 {len(wm.active_camps())} 个(最近 ({c.x},{c.y}))"
    return "GREEN", ""


def render_enemy_projection(threats: list) -> str:
    """把"假设我是对面"的推演渲染成简报行。

    用户 2026-07-31 的想法:不知道对面会干什么,就假设自己是对面。
    对蛮族这种确定性规则机,这不是猜测而是计算 —— 只算能力上界(移动力+射程),
    不猜意图,所以结论可靠。

    输出的是【对面下回合能打到多远】,而不是【它现在在哪】。后者模型已经
    从 THREAT 事件看到了,但"距离 4 格"这个数字会让它误以为安全 ——
    移动力 4 的骑手在 4 格外,下回合就在你脸上。
    """
    if not threats:
        return ""
    rows = []
    for t in threats:
        moves = getattr(t, "base_moves", 0) or 2
        rng = getattr(t, "range", 0) or 0
        # 2026-08-04 P1:海军推演用「距最近城市」而非混合距离 —— 海军
        # 打不到我方内陆单位/城市,只有沿海城市是它的目标。近战海军
        # (GALLEY)移动力对陆地城市无效,有效可达 = 1(贴脸)。
        is_naval = getattr(t, "is_naval", False)
        if is_naval:
            dist = getattr(t, "city_distance", 999)
            reach = max(1, rng)  # 海军攻城:近战贴脸1格,远程=射程
            label = "海上"
        else:
            dist = getattr(t, "distance", 99)
            reach = max(1, moves) + rng
            label = ""
        name = getattr(t, "unit_type", "?").replace("UNIT_", "")
        owner = getattr(t, "owner_name", "敌方")
        if is_naval and not getattr(t, "has_coastal_city", False):
            continue  # 我方无沿海城市 —— 海军打不到任何东西,不进推演
        if dist <= reach:
            mark = "⚔ 下回合可打到我方"
        elif dist <= reach + moves:
            mark = "→ 两回合内可到"
        else:
            continue
        rows.append(
            f"   {mark}:{owner} {name}{('[' + label + ']') if label else ''} 在 {dist} 格外"
            + (f",机动 {moves}" if not is_naval else "")
            + (f"+射程 {rng}" if rng else "")
            + f" = 可达 {reach} 格"
        )
    if not rows:
        return ""
    return "🎯 对手推演(按敌军自身机动力算,非猜测):\n" + "\n".join(rows[:5])


def red_directive(wm: WorldModel) -> str:
    """红色等级下的唯一目标指令。确定性生成,写进简报最顶部。"""
    camps = sorted(
        (c for c in wm.active_camps() if c.dist_to_capital >= 0),
        key=lambda c: c.dist_to_capital,
    )
    if camps:
        c = camps[0]
        return (
            f"【本回合唯一目标】处理 ({c.x},{c.y}) 蛮族营地方向的威胁。"
            f"在解除红色警报前:不要生产开拓者/建造者(会被拦截,可 force),"
            f"不要远征探图,军事单位不要离开首都圈。"
        )
    return (
        "【本回合唯一目标】消灭或击退视野内的敌方军事单位。"
        "在解除红色警报前:不要生产开拓者/建造者(会被拦截,可 force),"
        "平民不要出城。"
    )


# ---------------------------------------------------------------------------
# 简报
# ---------------------------------------------------------------------------


def render_briefing(wm: WorldModel, turn: int, threats: list | None = None) -> str:
    """把世界模型渲染成每回合注入的简报段。空则返回空串。"""
    lines: list[str] = []

    # --- L3 威胁等级与唯一目标。永远第一行 ---
    icon = {"RED": "🔴", "YELLOW": "🟡", "GREEN": "🟢"}.get(wm.threat_level, "🟢")
    head = f"{icon} 威胁等级 {wm.threat_level}"
    if wm.threat_reason:
        head += f" — {wm.threat_reason}"
    if wm.victory_type:
        head += f" ‖ 胜利路线:{wm.victory_type}(T{wm.victory_locked_turn} 锁定)"
    lines.append(head)
    if wm.threat_level == "RED":
        lines.append(red_directive(wm))

    # --- 对手推演。紧跟威胁等级,因为它解释了这个等级从哪来 ---
    projection = render_enemy_projection(threats or [])
    if projection:
        lines.append(projection)

    # --- 探索度。放最前面是因为实测证明它是最靠前的失败环节 ---
    if wm.tiles_total:
        pct = wm.exploration_pct
        if pct < EXPLORATION_POOR:
            lines.append(
                f"🔴 地图只探明 {pct:.1%}({wm.tiles_revealed}/{wm.tiles_total} 格)。"
                f"视野这么窄,你既不知道威胁从哪来,也不知道往哪扩张 —— "
                f"蛮族营地不去找就永远发现不了,而营地不拆,出兵就不会停。"
                f"派单位出去探图,这比多造一个建造者优先级更高。"
            )
        elif pct < EXPLORATION_LOW:
            lines.append(
                f"⚠️ 地图探明 {pct:.1%},视野偏窄。"
                f"考虑派闲置单位继续探索,尤其是尚未发现蛮族营地时。"
            )

    # --- 营地。只增不减,直到确认拆除 ---
    active = wm.active_camps()
    if active:
        for c in sorted(
            active, key=lambda c: c.dist_to_capital if c.dist_to_capital >= 0 else 999
        ):
            age = turn - c.first_seen_turn
            d = (
                f"距首都 {c.dist_to_capital} 格"
                if c.dist_to_capital >= 0
                else "距离未知"
            )
            lines.append(
                f"🏕 蛮族营地 ({c.x},{c.y}) {d} — 发现于 T{c.first_seen_turn}"
                f"({age} 回合前),**未拆除**。营地不拆,蛮族不会停。"
            )
    elif wm.tiles_total and wm.exploration_pct >= EXPLORATION_POOR:
        lines.append("🏕 已探明区域内无蛮族营地。")

    # --- 失联敌军 ---
    # 2026-08-02 校准局 5c:半径上限 12 —— 超过后"可能在半径 22 内"已无
    # 任何情报价值,却在和营地坐标抢显示行。超限条目直接不显示
    # (prune_contacts 会按时限清档,这里只管别让它占预算)。
    # 上限取 12:高机动单位失联 3 回合(4×3=12)仍是新鲜情报,该显示;
    # 本局实测的噪音样本全部在 14 以上。
    _RADIUS_CAP = 12
    for c in sorted(wm.stale_contacts(), key=lambda c: -c.last_turn)[:4]:
        gap = turn - c.last_turn
        if gap <= 0:
            continue
        r = c.guess_radius(turn)
        if r > _RADIUS_CAP:
            continue
        name = c.unit_type.replace("UNIT_", "")
        d = f",向{c.fled_dir}进雾" if c.fled_dir else ""
        lines.append(
            f"👁 失联 {c.owner} {name} {c.hp} — T{c.last_turn} 最后见于 "
            f"({c.last_x},{c.last_y}){d},已 {gap} 回合,可能在半径 {r} 内。"
        )

    # --- 死亡格 ---
    if wm.death_tiles:
        parts = []
        for t in sorted(wm.death_tiles.values(), key=lambda t: -len(t.victims))[:4]:
            vs = "、".join(v.replace("UNIT_", "") for v in t.victims[:3])
            parts.append(f"({t.x},{t.y}) {vs}")
        lines.append(f"☠️ 已损失单位的格子:{' ‖ '.join(parts)} — 勿再派平民进入。")

    # --- L2 计划链。永远显示"当前在做什么、下一步是什么" ---
    active = wm.active_plans()
    pending = wm.pending_plans()
    # 刚完成的要单独播报 —— 这是"完成即接续"这件事被模型看见的唯一途径。
    # 注意不能挂在 (active or pending) 分支里:最后一个计划完成时两者都为空,
    # 那恰恰是最需要提示"该挂新目标"的时刻。
    recent_done = [
        p for p in wm.plans if p.status == "done" and p.done_turn >= turn - 1
    ]
    if active or pending or recent_done:
        lines.append("📋 计划链:")
        for p in recent_done[:3]:
            tail = " 后续计划已自动激活。" if pending or active else ""
            lines.append(f"   ✔ #{p.id} {p.goal} — 已完成!{tail}")
        for p in active[:4]:
            cond = "" if p.cond == "manual" else f"(完成条件:{p.cond} {p.args})"
            lines.append(f"   ▶ #{p.id} {p.goal} {cond}")
        for p in pending[:2]:
            lines.append(f"   ⏸ #{p.id} {p.goal}(等待 #{p.after} 完成)")

    if not active and not pending:
        lines.append(
            "📋 没有进行中的计划。用 plan_add 挂下一阶段目标 —— "
            "写下来的计划才不会在几个回合后被忘掉。"
        )

    # 一局刚开始、什么都还没有时不占版面(只有威胁等级 + 挂计划提示两行)
    if (
        len(lines) <= 2
        and wm.threat_level == "GREEN"
        and not wm.threat_reason
        and not wm.victory_type
        and not wm.tiles_total
    ):
        return ""
    return "== 世界情报 ==\n" + "\n".join(lines)


# ===========================================================================
# 任务 M:铺城位置深度评估(追加,上方已有代码未动一行)
#
# get_settle_advisor 只看产出。人类高手看的是六个额外维度:战略资源、
# 扼守、忠诚度、防御地形、奢侈覆盖、卡邻居。这里全部是确定性计算。
#
# 忠诚度公式(计划书给定,标注为机制近似):
#     忠诚变化/回合 ≈ 10(基础) + Σ己方城市 人口×(1-距离/9)
#                              - Σ敌方城市 人口×(1-距离/9)
#     距离 ≥ 9 的城市压力为 0。新城满忠诚 100,净值为负时 ≈ 100/|净值|
#     回合后叛变。
#
# 距离用 hex 距离(odd-r,与游戏 Map.GetPlotDistance 一致,方案 v5 P0-a);
# 扼守判定是纯几何近似,文案里如实标注。
# ===========================================================================

LOYALTY_RANGE = 9  # 忠诚压力的作用半径(计划书公式)
LOYALTY_BASE = 10.0  # 基础忠诚增量(计划书公式)
_WORKABLE_RADIUS = 3  # 城市可工作圈


def _settle_dist(x1: int, y1: int, x2: int, y2: int) -> int:
    return hex_distance(x1, y1, x2, y2)


def loyalty_pressure(
    x: int,
    y: int,
    own_cities: list[dict],
    enemy_cities: list[dict],
) -> float:
    """在 (x,y) 铺城的每回合忠诚净变化(计划书公式,机制近似)。"""

    def _side(cities: list[dict]) -> float:
        total = 0.0
        for c in cities or []:
            try:
                d = _settle_dist(x, y, int(c["x"]), int(c["y"]))
                pop = float(c.get("population") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if d < LOYALTY_RANGE:
                total += pop * (1 - d / LOYALTY_RANGE)
        return total

    return LOYALTY_BASE + _side(own_cities) - _side(enemy_cities)


def evaluate_settle_location(
    x: int,
    y: int,
    tiles: list[dict],
    own_cities: list[dict] | None = None,
    enemy_cities: list[dict] | None = None,
    owned_luxuries: tuple[str, ...] | list[str] = (),
    max_items: int = 6,
) -> list[str]:
    """铺城位置的六维深度评估(产出之外的战略维度)。

    tiles:候选位周边地块(TileInfo 对齐的 dict,建议给到半径 3-5)。
    返回 list[str];无可评内容返回空列表,不抛异常。
    """
    try:
        return _settle_eval_inner(
            int(x),
            int(y),
            tiles or [],
            own_cities or [],
            enemy_cities or [],
            set(owned_luxuries or ()),
            max_items,
        )
    except Exception:
        return []


def _settle_eval_inner(
    x, y, tiles, own_cities, enemy_cities, owned_luxuries, max_items
) -> list[str]:
    near: list[dict] = []
    ring2: list[dict] = []
    for t in tiles:
        if not isinstance(t, dict):
            continue
        try:
            d = _settle_dist(x, y, int(t.get("x", 0)), int(t.get("y", 0)))
        except (TypeError, ValueError):
            continue
        if d <= _WORKABLE_RADIUS:
            near.append(t)
        if d == 2:
            ring2.append(t)

    dims: list[str] = []

    # ① 忠诚度(有敌城信息才算得出压力,没有则按无压力)
    net = loyalty_pressure(x, y, own_cities, enemy_cities)
    if net < 0:
        dims.append(
            f"🔴 忠诚度:净 {net:+.1f}/回合——按公式约 {abs(100 / net):.0f} 回合后"
            f"叛变。没有总督/驻军等硬手段就别铺(公式为计划书近似,实机可验)。"
        )
    elif net < 5 and enemy_cities:
        dims.append(
            f"⚠️ 忠诚度:净 {net:+.1f}/回合,贴着及格线——敌方再铺一城或涨人口"
            f"就会转负,预留总督位。"
        )

    # ② 战略资源
    strategics = sorted(
        {
            str(t.get("resource"))
            for t in near
            if t.get("resource")
            and str(t.get("resource_class") or "").lower() == "strategic"
        }
    )
    if strategics:
        dims.append(
            f"· 战略资源:{('、'.join(strategics[:3]))} 在可工作圈内——落城即圈进,"
            f"不用等买地。"
        )

    # ③ 奢侈覆盖(只算我们还没有的种类)
    new_lux = sorted(
        {
            str(t.get("resource"))
            for t in near
            if t.get("resource")
            and str(t.get("resource_class") or "").lower() == "luxury"
            and str(t.get("resource")) not in owned_luxuries
        }
    )
    if new_lux:
        dims.append(
            f"· 奢侈覆盖:{('、'.join(new_lux[:3]))} 是你还没有的种类——"
            f"每种给 4 城 +1 宜居度,这座城自带扩张配额。"
        )

    # ④ 防御地形
    def_mountains = sum(
        1
        for t in near
        if "_MOUNTAIN" in str(t.get("terrain") or "")
        and _settle_dist(x, y, int(t.get("x", 0)), int(t.get("y", 0))) <= 1
    )
    has_river = any(
        t.get("is_river")
        and _settle_dist(x, y, int(t.get("x", 0)), int(t.get("y", 0))) <= 1
        for t in near
    )
    if def_mountains >= 2 or (def_mountains >= 1 and has_river):
        bits = []
        if def_mountains:
            bits.append(f"{def_mountains} 面贴山")
        if has_river:
            bits.append("依河")
        dims.append(
            f"· 防御地形:{('、'.join(bits))}——进攻方少 {def_mountains} 个"
            f"可站位,渡河再吃减益,天然易守。"
        )

    # ⑤ 扼守价值(几何近似:二环不可通行/水面占比高 = 走廊收窄)
    if ring2:
        blocked = sum(
            1
            for t in ring2
            if "_MOUNTAIN" in str(t.get("terrain") or "")
            or str(t.get("terrain") or "") in ("TERRAIN_OCEAN", "TERRAIN_COAST")
        )
        ratio = blocked / len(ring2)
        if ratio >= 0.5:
            dims.append(
                f"· 扼守:二环 {ratio * 100:.0f}% 是山/水——这里是条走廊,"
                f"落城即关门(几何近似,以实际通行路线为准)。"
            )

    # ⑥ 卡邻居扩张(前哨判定)
    def _nearest(cities: list[dict]) -> int | None:
        ds = []
        for c in cities:
            try:
                ds.append(_settle_dist(x, y, int(c["x"]), int(c["y"])))
            except (KeyError, TypeError, ValueError):
                continue
        return min(ds) if ds else None

    d_own, d_enemy = _nearest(own_cities), _nearest(enemy_cities)
    if d_enemy is not None and d_own is not None and d_enemy < d_own:
        line = (
            f"· 前哨:离敌城 {d_enemy} 格 < 离己城 {d_own} 格——"
            f"卡住的是他的扩张位,他的开拓者要绕路了。"
        )
        if net < 0:
            line += "但忠诚撑不住(见上),卡位变送城。"
        dims.append(line)

    if not dims:
        return []
    head = f"== 铺城深评({x},{y}) =="
    if len(dims) > max_items:
        omitted = len(dims) - max_items
        dims = dims[:max_items]
        dims.append(f"   (另有 {omitted} 条维度已省略)")
    return [head, *dims]


# ===========================================================================
# 任务 Y:忠诚度翻转计算器(追加,上方已有代码未动一行)
#
# 任务 M 的 loyalty_pressure 回答的是"我在这儿铺城守不守得住";这里反过来:
# **敌人的城我能不能不费一兵一卒拿过来**。同一个公式,反向使用。
#
# 敌方城市的忠诚净值 = 10(基础)
#                    + Σ敌方城市人口×(1-距离/9)     ← 支撑它的
#                    - Σ我方城市人口×(1-距离/9)     ← 我施加的
# 净值为负 → 每回合掉,约 100/|净值| 回合后叛变加入我方。
#
# 加速手段(计划书列举,数值均未导出,标注为机制项):
#   · Amani 总督「使者领主」派到附近城邦 → 对周边施压
#   · 我方城市跑「面包与马戏」项目 → 增加忠诚压力
#   · 间谍「消解总督」→ 打掉对方的总督加成
#   · 在 9 格内再铺一座城 → 直接加人口压力(最确定的一项)
# ===========================================================================


def assess_loyalty_flip(
    target_city: dict,
    my_cities: list[dict] | None = None,
    enemy_cities: list[dict] | None = None,
    game_state: dict | None = None,
    max_items: int = 6,
) -> list[str]:
    """能不能靠忠诚度把敌方城市翻过来。不可翻转时也返回诊断,不抛异常。

    target_city : {"name","x","y","population","owner"(可选)}
    my_cities   : [{"x","y","population"}...]   我方施压源
    enemy_cities: [{"x","y","population"}...]   支撑它的敌方城市(含它自己)
    game_state  : {"has_amani":bool,"spy_available":bool,
                   "can_settle_nearby":bool}
    """
    try:
        return _flip_inner(
            target_city if isinstance(target_city, dict) else {},
            [c for c in (my_cities or []) if isinstance(c, dict)],
            [c for c in (enemy_cities or []) if isinstance(c, dict)],
            game_state if isinstance(game_state, dict) else {},
            max_items,
        )
    except Exception:
        return []


def _flip_inner(target, mine, enemy, gs, max_items) -> list[str]:
    try:
        tx, ty = int(target["x"]), int(target["y"])
    except (KeyError, TypeError, ValueError):
        return []
    name = str(target.get("name") or f"({tx},{ty})")
    try:
        pop = int(target.get("population") or 0)
    except (TypeError, ValueError):
        pop = 0

    # 目标城自身也是支撑它的力量;调用方若没带上它,这里补进去
    supports = list(enemy)
    if pop and not any(
        int(c.get("x", -999)) == tx and int(c.get("y", -999)) == ty
        for c in supports
        if isinstance(c, dict)
    ):
        supports.append({"x": tx, "y": ty, "population": pop})

    # 复用任务 M 的公式,方向对调:敌方是"己方压力",我方是"敌方压力"
    net = loyalty_pressure(tx, ty, supports, mine)

    out: list[str] = [f"== 忠诚翻转评估:{name} =="]
    my_press = sum(
        float(c.get("population") or 0)
        * (1 - _settle_dist(tx, ty, int(c["x"]), int(c["y"])) / LOYALTY_RANGE)
        for c in mine
        if isinstance(c, dict)
        and c.get("x") is not None
        and _settle_dist(tx, ty, int(c["x"]), int(c["y"])) < LOYALTY_RANGE
    )

    if net < 0:
        turns = max(1, int(100 / abs(net)))
        out.append(
            f"🔮 它每回合掉 {abs(net):.1f} 忠诚(你施加 {my_press:.1f} 压力)"
            f"—— 约 {turns} 回合后叛变加入你。不费一兵夺一城。"
        )
        out.append("· 保持现状即可:别打它,打下来反而重置忠诚。守住你的施压城市。")
        if turns > 25:
            out.append(f"· {turns} 回合偏久 —— 下面的加速手段能把它压到十几回合。")
    else:
        need = net + 0.1
        out.append(
            f"当前不可翻转:它的净忠诚 {net:+.1f}/回合(你已施加 {my_press:.1f} "
            f"压力,还差 {need:.1f} 才能压到负数)。"
        )

    # 加速/补差手段 —— 越确定的排越前
    if gs.get("can_settle_nearby"):
        out.append(
            "· ① 在它 9 格内再铺一座城:直接加人口压力,是这几项里"
            "最确定、也最持久的一项(公式里就是它)。"
        )
    if gs.get("has_amani"):
        out.append(
            "· ② 把 Amani(使者领主)派到附近城邦 —— 对周边城市施加忠诚压力"
            "(数值未导出,以游戏内为准)。"
        )
    else:
        out.append(
            "· ② Amani 总督(使者领主晋升)能对周边施压 —— 你还没有,值得为这一手任命。"
        )
    out.append("· ③ 我方邻近城市跑「面包与马戏」项目:项目期间额外施压。")
    if gs.get("spy_available"):
        out.append("· ④ 间谍做 NEUTRALIZE_GOVERNOR:打掉它的总督加成,对方少一份支撑。")

    out.append("   (忠诚公式为计划书近似;总督/项目/间谍的具体数值未导出)")
    if len(out) > max_items + 1:
        omitted = len(out) - max_items - 1
        out = out[: max_items + 1]
        out.append(f"   (另有 {omitted} 条已省略)")
    return out
