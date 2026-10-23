"""L3 策略引擎(下半)—— v3 的十棵通用决策树,全部加上门控。

═══════════════════════════════════════════════════════════════════════════
 设计文档(本 docstring 即交付物)
═══════════════════════════════════════════════════════════════════════════

## 0. 与 doctrine.py 的分工

    doctrine.py   决策树 11/12/13 —— 神级专项(生存 / 议程 / 不对称交战)
    playbook.py   决策树 1-10     —— 通用主线(开局/蛮族/邻居/科技/战争/
                                     扩城/区域/伟人/中期/危机)

## 1. v3 → v4 的唯一结构性改动:门控

v3 的形式是「条件 → 动作」。校准局证伪了其中三条,原因都一样:
**条件成立不等于该做**——还要看你付不付得起。v4 的形式是

    条件 AND L1档位允许 AND 资源余力足够 → 动作

拿不到 L1 档位时,受档位门控的分支**静默**,不按"大概是势均力敌"猜。

## 2. 三条被校准局实证证伪的,这里是改写后的版本

### 2.1 决策树 2「蛮族侦察兵 ≤2 格 → 立刻调所有可动单位追杀(最高优先级)」

T21-T22 照做,赔掉全国唯一的勇士。当时 1 城 4 产能,补员要 10 回合,
开局差点直接崩盘。**追杀一个侦察兵的收益上限是省掉一次营地报信,
风险下限是输掉整局。**

改写:追击需同时满足 `我方军事单位 ≥ 2` 与 `补员回合数 < 5`。
否则只**驱离**——把单位移动到威胁与平民之间,不发起攻击。
(驱离同样能挡住它抓平民,而且不会死。)

### 2.2 决策树 1/6「有营地威胁 → 先清营再出开拓者」

T10 照此思路让投石兵插队,推迟扩张 4 回合;而本局全图 6 个营地,
照做则永远铺不出第二座城。**营地是流量,城市是存量**——
用一次性产能去换一个会重新刷新的目标,在低产能期是净亏。

改写:按 `营地数 vs 我方清剿能力` 分档。产能低且营地多时,
扩张优先于清剿;清剿只处理**已经在出兵路径上**的那一个。

### 2.3 决策树 6/9「King: T50 应有 4 城, T100 应有 8 城」

实测 T56 才 3 城、T100 只 5 城,却是全场军力第一并攻下一座首都。
**按难度名硬编码的城市数阈值没有预测力**(难度还可能是随机的)。

改写:和**实测的对手城市数中位数**比。落后于中位数才算落后,
而且城市数永远不单独作为成败判据——它只是 L1 七个维度里的一个。

## 3. 不做的事

  · 不重复已接线模块已经在出的建议(tech_path/boost_planner/district_planner/
    crisis_manager/victory_block/great_person 都已在 _gather_expert_advice 里)。
    本模块只补它们**答不了的决策层问题**:什么时候做、代价是什么、不做会怎样。
  · 不给没有数据支撑的数字。"AI 战术差能吃 1.5-2 倍兵力"这类量级
    在方案 v4 §5 已标注为无实测支撑,这里一个字都不写。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from civ_mcp import civ_kit
from civ_mcp.advice import Advice, Confidence, Priority
from civ_mcp.doctrine import DoctrineInput
from civ_mcp.power_balance import DIM_CN, PowerTier

# —— 门控阈值。全部是【资源余力】判据,没有一个按难度名分档 ——
MIN_UNITS_TO_HUNT = 2  # 追击蛮族所需的最少军事单位数(§2.1)
MAX_REPLACE_TURNS = 5  # 补员超过这么多回合就不许拿单位去冒险
SETTLER_MIN_POP = 2  # 出开拓者的首都人口下限
SETTLER_MIN_PROD = 8  # 出开拓者的产能下限
SETTLER_BUY_GOLD = 270  # 直接买开拓者的金币线
OPENING_LAST_TURN = 30  # 决策树 1 的作用区间
MIDGAME_FROM = 50  # 决策树 9 的起点
REVIEW_EVERY = 20  # 路线复审间隔
CAMP_CLEAR_RADIUS = 6  # 「已经在出兵路径上」的营地距离
LOW_PRODUCTION = 10  # 低产能期:清剿的机会成本高于收益

_SIEGE_CLASS = "PROMOTION_CLASS_SIEGE"
_RANGED_CLASS = "PROMOTION_CLASS_RANGED"


# ---------------------------------------------------------------------------
# 共用小工具
# ---------------------------------------------------------------------------


def _units(inp: DoctrineInput, military_only: bool = True) -> list:
    out = []
    for u in inp.my_units or []:
        cs = int(getattr(u, "combat_strength", 0) or 0)
        rs = int(getattr(u, "ranged_strength", 0) or 0)
        if military_only and cs <= 0 and rs <= 0:
            continue
        out.append(u)
    return out


def _promotion_class(unit) -> str:
    return civ_kit.unit_row(getattr(unit, "unit_type", "") or "").get(
        "PromotionClass", ""
    )


def _capital(inp: DoctrineInput):
    """把首都近似为人口最高的城 —— CityInfo 没有 is_capital 字段。"""
    cities = inp.my_cities or []
    return (
        max(cities, key=lambda c: int(getattr(c, "population", 0) or 0))
        if cities
        else None
    )


def _replacement_turns(inp: DoctrineInput, unit_cost: int = 65) -> float:
    """补一个基础军事单位要几回合。产能未知时返回一个很大的数(保守)。"""
    prod = int(inp.production or 0)
    if prod <= 0:
        for c in inp.my_cities or []:
            prod = max(prod, int(float(getattr(c, "production", 0) or 0)))
    return unit_cost / prod if prod > 0 else 99.0


def _tier_allows(inp: DoctrineInput, minimum: PowerTier) -> bool:
    """档位门控。没有档位数据 → 不允许(静默),不猜。"""
    return inp.power is not None and inp.power.tier >= minimum


def _tier_known(inp: DoctrineInput) -> bool:
    return inp.power is not None


def _rival_city_median(inp: DoctrineInput) -> int | None:
    """实测对手城市数中位数 —— 取代按难度名硬编码的阈值(§2.3)。"""
    if inp.power is None or not inp.power.rivals:
        return None
    counts = sorted(s.cities for s in inp.power.rivals)
    mid = len(counts) // 2
    return counts[mid] if len(counts) % 2 else ((counts[mid - 1] + counts[mid]) // 2)


# ===========================================================================
# 决策树 1:开局前 30 回合
# ===========================================================================


def opening_queue(inp: DoctrineInput) -> list[Advice]:
    """T1-T30 的生产链。只回答「现在最该产什么、为什么是现在」。

    不重复 opening_assessor 的选址评估,也不重复 tech_path 的科技排序 ——
    那两件已经有模块在做。这里只补它们之间的空隙:**顺序与时机**。
    """
    out: list[Advice] = []
    if inp.turn <= 0 or inp.turn > OPENING_LAST_TURN:
        return out

    mil = _units(inp)
    cap = _capital(inp)

    # 无军事单位 —— 这是开局唯一真正会致命的空缺
    if not mil and inp.turn >= 8:
        out.append(
            Advice(
                module="playbook.opening",
                priority=Priority.CORE,
                action="把当前生产切成一个军事单位(有投石兵/弓箭手就出远程)",
                reason=f"T{inp.turn} 还没有任何军事单位",
                consequence=(
                    "蛮族营地会周期性出兵,而无护卫的建造者/开拓者是它的首选目标;"
                    "第一个平民被抓走的代价约等于 5-7 回合产能"
                ),
                confidence=Confidence.RECOMMENDED,
                alternative="城内已有远程且有城墙时可以再拖两回合,但不要拖到出开拓者之后",
                dedup_key="playbook:opening:first_military",
            )
        )

    # 扩城时机 —— 条件是资源余力,不是回合数
    # 红警下不催造平民:game_state.set_city_production 会直接拒绝
    # (Error: RED_ALERT_CIVILIAN)。催一条工具层保证会失败的动作,
    # 只会把 force=True 训练成肌肉记忆(2026-08-06 实机矛盾)。
    red = (inp.threat_level or "").upper() == "RED"
    if cap is not None and len(inp.my_cities or []) < 2 and not red:
        pop = int(getattr(cap, "population", 0) or 0)
        prod = float(getattr(cap, "production", 0) or 0)
        if pop >= SETTLER_MIN_POP and prod >= SETTLER_MIN_PROD:
            out.append(
                Advice(
                    module="playbook.opening",
                    priority=Priority.CORE,
                    action="首都转产开拓者 —— 人口与产能都已过线",
                    reason=f"{getattr(cap, 'name', '首都')} pop{pop} 产能{prod:.0f}"
                    f"(线是 pop≥{SETTLER_MIN_POP} 且产能≥{SETTLER_MIN_PROD})",
                    consequence="第二座城的复利从它建成那一刻开始算,晚一回合就少一回合",
                    confidence=Confidence.RECOMMENDED,
                    dedup_key="playbook:opening:settler",
                )
            )
        elif inp.gold >= SETTLER_BUY_GOLD:
            out.append(
                Advice(
                    module="playbook.opening",
                    priority=Priority.OPTIMIZE,
                    action=f"直接花金币买开拓者(你有 {inp.gold:.0f} 金)",
                    reason="首都产能还没到线,但金币够 —— 买单位是把金转成产能的最快方式",
                    consequence="早期金币躺着不产生任何收益",
                    confidence=Confidence.OPTIONAL,
                    alternative="若这笔金准备用于买城墙或抢关键地块,先算哪个更急",
                    dedup_key="playbook:opening:buy_settler",
                )
            )

    # 探图 —— 校准局实测这是最靠前的失败环节
    if inp.exploration_pct and inp.exploration_pct < 0.08:
        out.append(
            Advice(
                module="playbook.opening",
                priority=Priority.CORE,
                action="把闲置单位全部派出去探图",
                reason=f"仅探明 {inp.exploration_pct:.0%}",
                consequence="看不见的营地不会停止出兵,看不见的落点也没法评估",
                confidence=Confidence.CERTAIN,
                dedup_key="playbook:opening:explore",
            )
        )
    return out


# ===========================================================================
# 决策树 2:蛮族应对(v3 版本已被实证证伪,这里是改写版)
# ===========================================================================


def barbarian_response(inp: DoctrineInput) -> list[Advice]:
    """蛮族。核心改动:**追击要付得起,付不起就只驱离**(§2.1)。"""
    out: list[Advice] = []
    barb = [
        t
        for t in inp.threats or []
        if (getattr(t, "owner_name", "") or "").lower().startswith("barbarian")
        or getattr(t, "owner_id", 0) == 63
    ]
    mil = _units(inp)
    replace = _replacement_turns(inp)

    near = [t for t in barb if int(getattr(t, "distance", 99) or 99) <= 2]
    if near:
        affordable = len(mil) >= MIN_UNITS_TO_HUNT and replace < MAX_REPLACE_TURNS
        if affordable:
            out.append(
                Advice(
                    module="playbook.barb",
                    priority=Priority.CORE,
                    action=f"派一个(不是全部)军事单位追掉 {len(near)} 格内的蛮族侦察兵",
                    reason="侦察兵跑回营地报信后,该营地会持续针对你",
                    consequence="报信之后每隔几回合就要处理一次出兵",
                    confidence=Confidence.RECOMMENDED,
                    alternative="城内留至少一个单位驻防 —— 追击不等于清空后方",
                    dedup_key="playbook:barb:hunt",
                    detail=(
                        f"付得起的依据:你有 {len(mil)} 个军事单位,"
                        f"补员约 {replace:.1f} 回合",
                    ),
                )
            )
        else:
            out.append(
                Advice(
                    module="playbook.barb",
                    priority=Priority.CORE,
                    action="**不要追击**,改为驱离:把军事单位移到蛮族与你的平民之间,不发起攻击",
                    reason=(
                        f"你只有 {len(mil)} 个军事单位、补员约 {replace:.1f} 回合 —— "
                        f"追击的收益上限是省掉一次报信,风险下限是输掉整局"
                    ),
                    consequence=(
                        "校准局 T21-T22 就是照 v3 的「立刻追杀」打的:赔掉全国唯一的勇士,"
                        "当时 1 城 4 产能,补员 10 回合,开局差点崩盘"
                    ),
                    confidence=Confidence.RECOMMENDED,
                    alternative="有城墙的城可以放它进射程,让城市开火 —— 城市攻击零风险",
                    dedup_key="playbook:barb:screen",
                )
            )

    # 营地:流量 vs 存量(§2.2)
    camps = list(inp.camps or [])
    if camps:
        low_prod = (inp.production or 0) < LOW_PRODUCTION
        close = [c for c in camps if _camp_dist(c) <= CAMP_CLEAR_RADIUS]
        if low_prod and len(camps) >= 2 and len(inp.my_cities or []) < 3:
            out.append(
                Advice(
                    module="playbook.barb",
                    priority=Priority.OPTIMIZE,
                    action=(
                        f"暂不清剿(已发现 {len(camps)} 个营地),扩张优先;"
                        f"只处理确实已经在出兵打你的那一个"
                    ),
                    reason=f"当前产能 {inp.production},城市 {len(inp.my_cities or [])} 座",
                    consequence=(
                        "营地会刷新,城市不会。低产能期把一次性产能投给会重生的目标,"
                        "换来的是永远铺不出第二座城 —— 校准局全图 6 个营地,"
                        "照 v3 的「先清营再出开拓者」会一座都铺不出来"
                    ),
                    confidence=Confidence.OPTIONAL,
                    alternative="若某个营地已连续出兵掠夺你的改良,那一个必须清",
                    dedup_key="playbook:barb:defer_camps",
                )
            )
        elif close and len(mil) >= MIN_UNITS_TO_HUNT:
            out.append(
                Advice(
                    module="playbook.barb",
                    priority=Priority.OPTIMIZE,
                    action=f"清掉 {CAMP_CLEAR_RADIUS} 格内的营地(有 {len(close)} 个)",
                    reason="营地随时代升级 —— 远古营地出勇士,同一个营地到中世纪出披甲战士",
                    consequence="拖越久清剿成本越高,而它的出兵不会停",
                    confidence=Confidence.RECOMMENDED,
                    dedup_key="playbook:barb:clear",
                )
            )
    return out


# 营地放血多久算"必须立计划"(回合)。首见后拖过这条线还没动手,
# 说明缺的不是判断而是计划 —— 2026-08-06 法国局:T12 见营地、T64 仍未拆,
# 中间 50 回合被出兵 15+ 次、赔掉 5 个单位,而系统只会重复"营地不拆出兵不停"。
CAMP_BLEED_TURNS = 15
# 拆营的最低编成:营地本身要【近战单位踩进那一格】才能摧毁,远程只能杀守军。
# 再加一个单位吸引/补刀,低于这个数去拆就是送人头。
CAMP_ASSAULT_MELEE = 1
CAMP_ASSAULT_TOTAL = 3


def camp_campaign(inp: DoctrineInput) -> list[Advice]:
    """营地清剿【计划】—— 把诊断升级成带数字和期限的行动表。

    barbarian_response 只覆盖两头:兵够了叫你去拆、早期多营地叫你别管。
    中间那段("营地在放血,但我兵力不够")它一句话都没有 —— 而那恰恰是
    最需要计划的时候:2026-08-06 法国局 T12 发现营地,到 T64 还在被放血,
    模型自述"一直想拆但始终差兵力,系统也没推先补兵再拆营的主线"。

    本树只在【营地已持续放血】时出手,输出四件可执行的事:
      1. 拆营需要什么编成(近战必须有,因为摧毁营地要踩进那一格)
      2. 现在差多少
      3. 按当前产能补齐要几回合 → 给出可动手的回合数
      4. 攒兵期间的防守姿态(这局赔的 5 个单位全是攒兵期冒进丢的)
    """
    out: list[Advice] = []
    camps = list(inp.camps or [])
    turn = int(inp.turn or 0)
    if not camps or turn <= 0:
        return out

    # 只看已经放血够久的营地:首见回合拿得到才算,拿不到的不猜
    bleeding = []
    for c in camps:
        seen = getattr(c, "first_seen_turn", None)
        if seen is None or int(seen) <= 0:
            continue
        if turn - int(seen) >= CAMP_BLEED_TURNS:
            bleeding.append((c, turn - int(seen)))
    if not bleeding:
        return out
    camp, age = max(bleeding, key=lambda p: p[1])

    mil = _units(inp)
    from civ_mcp import war_planner

    melee = [
        u
        for u in mil
        if war_planner.can_capture_city(str(getattr(u, "unit_type", "") or ""))
    ]
    need_total = max(0, CAMP_ASSAULT_TOTAL - len(mil))
    need_melee = max(0, CAMP_ASSAULT_MELEE - len(melee))
    short = max(need_total, need_melee)

    cx = getattr(camp, "x", "?")
    cy = getattr(camp, "y", "?")
    dist = _camp_dist(camp)
    dist_txt = f"距首都 {dist} 格" if dist < 99 else "距离未知"

    if short <= 0:
        # 编成够了 —— 这时候缺的不是兵而是决心,给一条硬期限
        out.append(
            Advice(
                module="playbook.camp",
                priority=Priority.CORE,
                action=(
                    f"本回合就去拆营地({cx},{cy}) —— 编成已经够了"
                    f"({len(mil)} 个军事单位、{len(melee)} 个近战)"
                ),
                reason=(
                    f"这个营地已经放血 {age} 回合。编成够了还不动手,"
                    f"等于每回合白付一次出兵骚扰的代价"
                ),
                consequence=(
                    "营地随时代升级:远古出勇士,拖到中世纪出披甲战士 —— "
                    "拖得越久,同一个营地要付的清剿成本越高"
                ),
                confidence=Confidence.RECOMMENDED,
                alternative=(
                    "路上有敌方单位就先集火清掉再进;"
                    "摧毁营地必须让近战单位【踩进营地那一格】,远程只能杀守军"
                ),
                dedup_key="playbook:camp:strike_now",
                detail=(
                    f"营地({cx},{cy}) {dist_txt},T{turn - age} 首见",
                    "拆完立刻回防:蛮族清空前后是掠夺改良的高发期",
                ),
            )
        )
        return out

    # 兵力不够 —— 给攒兵时间表,这是原来完全缺失的那一段
    replace = _replacement_turns(inp)
    eta = int(round(replace * short)) if replace < 90 else None
    eta_txt = (
        f"按当前产能 {inp.production}/回合,补齐约需 {eta} 回合 → 最早 T{turn + eta} 动手"
        if eta is not None
        else "当前产能未知或过低,先把首都产能提上来,否则永远攒不出这支队"
    )
    out.append(
        Advice(
            module="playbook.camp",
            priority=Priority.CORE,
            action=(
                f"立一个拆营时间表:还差 {short} 个军事单位"
                f"(需 {CAMP_ASSAULT_TOTAL} 个、其中 ≥{CAMP_ASSAULT_MELEE} 个近战;"
                f"现有 {len(mil)} 个、{len(melee)} 个近战)。{eta_txt}"
            ),
            reason=(
                f"营地({cx},{cy})已经放血 {age} 回合 —— 缺的不是判断,是计划。"
                f"「想拆但差兵力」拖着不定期限,就会一路拖到被碾压"
            ),
            consequence=(
                "2026-08-06 法国局实证:T12 发现营地、T64 仍未拆,"
                "中间 50 回合被出兵 15+ 次、赔掉 5 个单位,军力反被拉开到 0.4×"
            ),
            confidence=Confidence.RECOMMENDED,
            alternative=(
                "攒不出来就别硬攒:承认这块地暂时守不住,把平民全部撤进城,"
                "改良交给蛮族掠夺 —— 也比每回合赔单位强"
            ),
            dedup_key="playbook:camp:plan",
            detail=(
                f"营地({cx},{cy}) {dist_txt},T{turn - age} 首见",
                "攒兵期间:军事单位一律回城/驻防,不要野外过夜、不要追击 —— "
                "这局赔掉的 5 个单位全是攒兵期冒进丢的",
            ),
        )
    )
    return out


def _camp_dist(camp) -> int:
    """营地到首都的距离。

    两种形态都认:世界扫描的 `(x, y, dist_to_capital)` 三元组,
    以及 `world_model.Camp` 对象。距离缺失(-1)按很远处理 —— 宁可不催清剿,
    也不要因为一个坐标不明的营地就把产能从扩张上抽走(§2.2)。
    """
    if isinstance(camp, (tuple, list)):
        d = int(camp[2]) if len(camp) > 2 else -1
    else:
        d = int(getattr(camp, "dist_to_capital", -1) or -1)
    return 99 if d < 0 else d


# ===========================================================================
# 决策树 3:邻居策略
# ===========================================================================


def neighbor_policy(inp: DoctrineInput) -> list[Advice]:
    """把每个已知邻居归档并给出对应打法。

    归档依据是**能算的**:军力比 + 是否交战 + 关系态度。
    「距离」用可见城市的存在近似(见到他的城 = 够近)—— 没有真实距离数据时
    不编一个格数出来。
    """
    out: list[Advice] = []
    my_mil = int(inp.my_military or 0)
    for civ in inp.civs or []:
        if not getattr(civ, "has_met", True) or getattr(civ, "is_at_war", False):
            continue
        their = int(getattr(civ, "military_strength", 0) or 0)
        name = getattr(civ, "civ_name", None) or "邻居"
        state = (getattr(civ, "diplomatic_state", "") or "").upper()
        # 「接壤」用可见城市近似 —— CivInfo 没有真实距离字段,
        # 没有数据时不编一个格数出来。
        adjacent = bool(getattr(civ, "visible_cities", None))
        if not my_mil or not their:
            continue
        ratio = their / my_mil
        if adjacent and ratio >= 1.3 and "FRIEND" not in state and "ALLI" not in state:
            out.append(
                Advice(
                    module="playbook.neighbor",
                    priority=Priority.CORE,
                    action=f"把 {name} 当高危邻居处理:送使团 → 满足他的议程 → 边境城造墙",
                    reason=f"他的军力 {their} 是你的 {ratio:.1f} 倍,而且你们已经接壤",
                    consequence="军力悬殊本身就是 AI 的开战判据之一,不需要别的理由",
                    confidence=Confidence.RECOMMENDED,
                    alternative=(
                        "打不过也追不平时,退一步:主动开放边境 + 把他要的奢侈卖给他,"
                        "把开战时点往后推"
                    ),
                    subject=f"player:{getattr(civ, 'player_id', -1)}",
                    dedup_key=f"playbook:neighbor:hostile:{getattr(civ, 'player_id', -1)}",
                )
            )
        elif adjacent and ratio <= 0.7:
            out.append(
                Advice(
                    module="playbook.neighbor",
                    priority=Priority.OPTIMIZE,
                    action=f"{name} 军力只有你的 {ratio:.1f} 倍 —— 要么当贸易伙伴养,要么就是开战对象",
                    reason="接壤 + 弱势,这两条同时成立时他是你唯一低成本的扩张方向",
                    consequence="",
                    confidence=Confidence.OPTIONAL,
                    alternative="打不打取决于 L1 档位与 UU 窗口;不打就宣友并把贸易路线全派他",
                    subject=f"player:{getattr(civ, 'player_id', -1)}",
                    dedup_key=f"playbook:neighbor:weak:{getattr(civ, 'player_id', -1)}",
                )
            )
    return out


# ===========================================================================
# 决策树 4:科技 / 市政选择的「为什么是现在」
# ===========================================================================


def research_rationale(inp: DoctrineInput) -> list[Advice]:
    """不排科技顺序(tech_path 在做),只补它不说的两件事:
    尤里卡的机会成本,以及对手科技数与你的差。"""
    out: list[Advice] = []
    if inp.boosted:
        out.append(
            Advice(
                module="playbook.research",
                priority=Priority.OPTIMIZE,
                action=f"优先研究已点亮尤里卡/鼓舞的项(当前有 {len(inp.boosted)} 个)",
                reason="尤里卡直接省掉约 40% 的研究量,而它不会过期但会被路线绕过",
                consequence="绕过去等于白扔已经完成的触发条件",
                confidence=Confidence.RECOMMENDED,
                alternative="主线急需的关键科技可以插队,但插队理由要能说出来",
                dedup_key="playbook:research:eureka",
            )
        )
    tech = inp.power.ratios.get("techs") if inp.power else None
    if tech is not None and tech.usable and tech.value < 0.75:
        out.append(
            Advice(
                module="playbook.research",
                priority=Priority.CORE,
                action="科技线收窄到「解锁下一级军事单位 + 增长必需」两条",
                reason=f"你的科技数 {tech.mine:g},{tech.best_name} 是 {tech.best:g}"
                f"(比 {tech.value:.2f}×)",
                consequence="全面铺科技在落后档意味着每条线都比对手晚,哪条都用不上",
                confidence=Confidence.RECOMMENDED,
                alternative="若已锁定文化/宗教路线,收窄的对象换成该路线的市政线",
                dedup_key="playbook:research:narrow",
            )
        )
        # 科研产出的来源纠偏(2026-08-05,外部攻略共识 + 机制):
        # 科研 = 人口(每市民固定科研)+ 学院及其邻接,与"选哪条科技线"无关。
        # 落后时把注意力放在换科技顺序上是治不了病的 —— T335 科研 0.25×
        # 的根因是 8 城 67 人口,不是研究顺序排错了。
        sci = inp.power.ratios.get("science") if inp.power else None
        if sci is not None and sci.usable and sci.value < 0.75:
            n_campus = sum(
                1
                for c in inp.my_cities or []
                for d in (getattr(c, "districts", None) or [])
                if "CAMPUS" in str(d).upper()
            )
            n_cities = len(inp.my_cities or []) or (
                inp.power.me.cities if inp.power else 0
            )
            gap = ""
            if n_cities:
                gap = f";{n_cities} 座城里有学院的 {n_campus} 座"
            out.append(
                Advice(
                    module="playbook.research",
                    priority=Priority.CORE,
                    action="科研落后要治的是【人口与学院】,不是研究顺序",
                    reason=(
                        f"科研 {sci.value:.2f}× —— 科研产出来自人口(每个市民都产科研)"
                        f"与学院及其邻接加成,换科技顺序改变不了产出总量{gap}"
                    ),
                    consequence="只调研究顺序而不补城/人口/学院,差距每回合都会继续拉大",
                    confidence=Confidence.RECOMMENDED,
                    alternative=(
                        "按性价比排:补城 > 缺学院的城补学院(优先靠山/雨林的高邻接位)"
                        " > 用食物/住房解开人口停滞 > 图书馆等建筑"
                    ),
                    dedup_key="playbook:research:science_source",
                )
            )
    return out


# ===========================================================================
# 决策树 5:战争 —— 打不打、什么时候打
# ===========================================================================


def uu_timing_push(inp: DoctrineInput) -> list[Advice]:
    """UU 可建造窗口播报(2026-08-05,外部高手 timing push 框架落地)。

    机制(全部 gamedata 可验证,无经验数字):UU 的前置科技开窗;
    **升级目标的前置科技一旦研究完成,UU 就永远造不出来了** —— 窗口是
    被自己的科技进度关掉的,所以它可倒计时、可预测,甚至可以主动暂缓
    研究来延长。这一条与 L1 军力比互相独立:军力比回答"打得动吗",
    窗口回答"这个身份优势还剩多久"。

    T335 实证:罗马军团窗口 = 铁器时代 → 学徒制,整段窗口零战争,
    等到 T314 想认真打时主力已是弩手,等于用罗马打了一局通用文明。
    """
    out: list[Advice] = []
    kit = inp.kit
    if kit is None or not kit.windows:
        return out
    researched = inp.researched or set()
    for w in sorted(kit.windows, key=lambda w: -w.value):
        state = civ_kit.window_state(w, researched, available=inp.boosted or set())
        if not state or state == civ_kit.WINDOW_LOCKED:
            continue
        if w.edge <= 0:
            continue  # 不构成代差的 UU 不值得为它调整节奏
        name = w.uu.name or w.uu.unit_type
        if state == civ_kit.WINDOW_OPEN:
            out.append(
                Advice(
                    module="playbook.uu_window",
                    # OPTIMIZE 而非 CORE:窗口开着会持续很多回合,是【长期机会】;
                    # 而 CORE 档要留给"不做就赔"的风险项(如"别啃没破墙的城")。
                    # 2026-08-05 实测:初版写 CORE,直接把 T110 那条"不要进攻
                    # 带墙城市"挤出了配额 —— 机会建议吃掉安全建议是本末倒置。
                    priority=Priority.OPTIMIZE,
                    action=f"{name} 的窗口开着 —— 这是本局把文明身份兑现成城市的时段",
                    reason=(
                        f"{name} 战力 {w.uu.power} vs 同期通用 {w.peer_power};"
                        f"研究出「{w.upgrade_name}」的前置科技后就再也造不出它"
                    ),
                    consequence="窗口关掉不可逆:错过就等于这局没用过本文明的军事身份",
                    confidence=Confidence.RECOMMENDED,
                    alternative=(
                        "不打仗也要兑现:窗口期内把它当便宜的防御力量铺开,"
                        "省下的产能转民生"
                    ),
                    dedup_key=f"playbook:uu:open:{w.uu.unit_type}",
                    detail=(
                        "高手手法(timing push):开打前先在各城囤前一级单位并攒金/铁,"
                        "窗口打开当回合用 upgrade_unit 批量升级,一次形成兵力差",
                        "近战单位不可省:夹击与支援加成只有近战吃得到,纯远程军队推不动城",
                    ),
                )
            )
        elif state == civ_kit.WINDOW_CLOSING:
            out.append(
                Advice(
                    module="playbook.uu_window",
                    priority=Priority.CORE,
                    action=(
                        f"{name} 的窗口即将关闭 —— 「{w.upgrade_name}」的科技一完成"
                        f"就再也造不出它"
                    ),
                    reason="窗口是被你自己的科技进度关掉的,所以你能控制它",
                    consequence="研究完成即不可逆",
                    confidence=Confidence.RECOMMENDED,
                    alternative=(
                        f"要么现在把 {name} 一次性造/升级到位,"
                        f"要么先研究别的科技把窗口留着"
                    ),
                    dedup_key=f"playbook:uu:closing:{w.uu.unit_type}",
                )
            )
    return out


def war_timing(inp: DoctrineInput) -> list[Advice]:
    """宣战与攻城的时机判定。

    「能不能打」由 L1 军力比回答,「什么时候打」由 UU 窗口和对手状态回答 ——
    这两件事 v3 混在一起,是它的决策树 5 最容易误导人的地方。
    """
    out: list[Advice] = []
    if not _tier_known(inp):
        return out
    mil_ratio = inp.power.military_ratio
    at_war = [c for c in inp.civs or [] if getattr(c, "is_at_war", False)]

    # —— 开战窗口:军力优势 + UU 窗口都开着的时候最便宜 ——
    # 军力比必须【可信】才谈窗口(2026-08-06 实机事故):法国局 T4-T10,
    # 我方 1 勇士 + 1 侦察兵、对手军力未知,比值算出 2.00×,这条建议
    # 反复推荐"选一个接壤的弱邻居动手" —— 拿两个单位去开战。
    # 绝对量太小或对手无数据时,比值是噪音不是情报。
    mil_known = inp.power.ratio_usable("military")
    if not at_war and mil_known and mil_ratio >= 1.5:
        window = inp.kit.best_window if inp.kit else None
        why_now = []
        if (
            window is not None
            and window.uu.prereq
            and (window.uu.prereq in (inp.researched or set()))
        ):
            why_now.append(
                f"{window.uu.name} 已解锁且窗口在 "
                f"{window.counter_name or '通用单位追平'} 之前关闭"
            )
        why_now.append(f"军力比 {mil_ratio:.2f}×")
        out.append(
            Advice(
                module="playbook.war",
                priority=Priority.CORE,
                action="现在是开战性价比最高的窗口 —— 选一个接壤的弱邻居动手",
                reason=" ‖ ".join(why_now),
                consequence=(
                    "军力优势会随对手科技上升自动归零。不兑现成城市,"
                    "这段优势就只是维护费"
                ),
                confidence=Confidence.OPTIONAL,
                alternative=(
                    "不想开战就把军力换成外交筹码:军力强时的和平/开边境要价更高"
                ),
                dedup_key="playbook:war:window",
                detail=(
                    "宣战当回合打不了 —— 引擎的战斗状态到下回合才同步。"
                    "正确顺序:宣战 → 本回合把单位摆到发起线 → 下回合一起打",
                ),
            )
        )

    # —— 已在交战:攻城四档 ——
    if at_war:
        from civ_mcp import war_planner

        mil = _units(inp)
        # 破墙判定统一走 war_planner.can_break_walls(审查 2026-08-05 F2-2):
        # 此前按 PROMOTION_CLASS_SIEGE 判 —— 弩手(RS40)时代喊"用远程磨墙"
        # (T335 实测 21 回合磨不动),造出野战加农炮(RANGED 类,RS60)后
        # 又反过来喊"缺攻城单位"。与宣战门禁 NO_SIEGE_VS_WALLS 同一把尺。
        breakers = [
            u
            for u in mil
            if war_planner.can_break_walls(str(getattr(u, "unit_type", "") or ""))
        ]
        ranged = [u for u in mil if _promotion_class(u) == _RANGED_CLASS]
        walled = any(
            getattr(vc, "has_walls", False)
            for c in at_war
            for vc in getattr(c, "visible_cities", None) or []
        )
        if walled and not breakers and not ranged:
            grade, act, why = (
                "🔴",
                "停止攻城,转掠夺经济",
                "既无破墙单位也无远程单位,近战啃城墙是纯亏损交换",
            )
        elif walled and not breakers:
            grade, act, why = (
                "🟠",
                "停止磨墙,优先补破墙单位;期间转掠夺/歼灭出城部队",
                "现有远程强度不足以磨动城墙(弩手对文艺复兴墙"
                "21 回合零进展是 T335 实测);破墙单位 = 攻城系/"
                "野战加农炮档远程/海军远程/轰炸机",
            )
        elif mil_ratio < 0.7:
            grade, act, why = (
                "🟠",
                "转防守消耗,并评估求和",
                f"军力比 {mil_ratio:.2f}× —— 进攻方在劣势下攻城会被反推",
            )
        else:
            grade, act, why = (
                "🟢",
                "可以推进:集结 → 磨墙 → 近战收尾",
                f"军力比 {mil_ratio:.2f}×,破墙单位 {len(breakers)} 个",
            )
        out.append(
            Advice(
                module="playbook.war",
                priority=Priority.CORE,
                action=f"{grade} {act}",
                reason=why,
                consequence="",
                confidence=Confidence.RECOMMENDED,
                alternative="僵持超过 5 回合且城血没掉 → 求和(10 回合冷却后)或转掠夺",
                dedup_key="playbook:war:siege_grade",
            )
        )

    # —— 占城后的保留/夷平 ——
    for c in inp.my_cities or []:
        loyalty_pt = float(getattr(c, "loyalty_per_turn", 0) or 0)
        flip = int(getattr(c, "turns_to_loyalty_flip", 0) or 0)
        if loyalty_pt < 0 and 0 < flip <= 15:
            out.append(
                Advice(
                    module="playbook.war",
                    priority=Priority.CORE,
                    action=(
                        f"{getattr(c, 'name', '该城')} 约 {flip} 回合后叛变 —— "
                        f"现在决定:派总督+驻军保,还是让它走"
                    ),
                    reason=f"忠诚 {loyalty_pt:+.1f}/回合",
                    consequence="叛变会连同城里的区域与建筑一起失去,且会倒向原主",
                    confidence=Confidence.CERTAIN,
                    alternative=(
                        "按性价比排:总督 Victor > 驻军 > 买纪念碑 > 政策卡;"
                        "四项加起来仍为负时,与其砸钱不如让它叛变后再推平重建"
                    ),
                    subject=f"city:{getattr(c, 'name', '')}",
                    dedup_key=f"playbook:war:loyalty:{getattr(c, 'name', '')}",
                )
            )
    return out


# ===========================================================================
# 决策树 6:扩城(阈值改为与实测对手中位数比,§2.3)
# ===========================================================================


def expansion_timing(inp: DoctrineInput) -> list[Advice]:
    out: list[Advice] = []
    mine = len(inp.my_cities or []) or (inp.power.me.cities if inp.power else 0)
    median = _rival_city_median(inp)
    if median is None or not mine:
        return out

    if mine < median:
        gap = median - mine
        # 红警下让步但**不静默**(2026-08-06):事实(你落后几座城)照说,
        # 动作(现在就转产开拓者)撤掉 —— 那个动作会被 game_state 直接
        # 拒绝(Error: RED_ALERT_CIVILIAN),催了也执行不了。
        # 信息不丢、矛盾消失,这比整条静默更诚实。
        red = (inp.threat_level or "").upper() == "RED"
        out.append(
            Advice(
                module="playbook.expand",
                priority=(
                    Priority.OPTIMIZE
                    if red
                    else (Priority.CORE if gap >= 2 else Priority.OPTIMIZE)
                ),
                action=(
                    f"先解除红色警报,再补这 {gap} 座城"
                    f"(你 {mine} 座,对手中位数 {median} 座)"
                    if red
                    else f"补 {gap} 座城 —— 你 {mine} 座,对手中位数 {median} 座"
                ),
                reason=(
                    "城市数是复利的底数;差距会随时间自己放大。"
                    + (
                        "但红警期间造平民会被工具层直接拒绝 —— 敌军还在打击范围内,"
                        "现在出的开拓者就是送人头"
                        if red
                        else ""
                    )
                ),
                consequence=(
                    "注意:城市数**不单独**作为成败判据 —— 校准局 T100 只有 5 城,"
                    "却是全场军力第一并攻下一座首都。它只是七个测量维度里的一个"
                ),
                confidence=Confidence.OPTIONAL,
                alternative=(
                    "先清掉/驱离逼近的威胁,红警一解除立刻补城"
                    if red
                    else "没有安全落点时,攻占比铺城更快:一座打下来的城省掉开拓者与成长期"
                ),
                subject="production:civilian",
                dedup_key="playbook:expand:behind",
            )
        )
    elif mine > median and _tier_allows(inp, PowerTier.EVEN):
        out.append(
            Advice(
                module="playbook.expand",
                priority=Priority.OPTIMIZE,
                action=f"城市数已过对手中位数({mine} vs {median})—— 转向纵向发展",
                reason="再铺城会稀释宜居度与忠诚度,边际收益开始下降",
                consequence="",
                confidence=Confidence.OPTIONAL,
                alternative="除非有卡位价值极高的落点(战略资源/挡住对手扩张方向)",
                dedup_key="playbook:expand:enough",
            )
        )
    return out


# 开拓者闲置多少回合算"停在手里"。1 回合可能只是在赶路。
SETTLER_IDLE_TURNS = 2

# 军力比跌破这条线就不再是"窗口"(war_timing 开窗线是 1.5×)
WAR_WINDOW_FLOOR = 1.3
# 目标城墙情报超过这么多回合没更新就不可信 —— 法国局 T64 败在 9 回合前的旧情报
INTEL_STALE_TURNS = 8


def _rank_war_targets(
    civs: list, domination: bool
) -> list[tuple[int, int, int, str, object]]:
    """开战目标排序。返回 (首都键, 城墙键, 人口, 拥有者, 城) —— 越小越优先。

    统治路线下【原始首都优先】,压过无墙和低人口。

    2026-08-11 罗马局 T268 实测:同一份简报里,"统治胜利判据:占领所有其它
    文明的【原始首都】(不是所有城市)"和"目标定 挪威 的 奥勒松(82,61),
    人口 1"并排出现 —— 而奥勒松不是首都,打下来统治计数一个不涨。旧排序键
    只有 (有无城墙, 人口),挑的是【最好打的】城,不是【计分的】城。

    这比"拖着不兑现"更贵:窗口有限,拿它去打一座不计分的城,窗口照样关闭,
    战争疲劳和外交惩罚照样付,而统治进度纹丝不动。两局都败在这个窗口上,
    复盘一直只归因于犹豫,现在看目标选错同样有份。

    非统治路线下维持旧序:那时首都只是"又硬又远的城",没有额外价值。
    """
    targets: list[tuple[int, int, int, str, object]] = []
    for civ in civs:
        if getattr(civ, "is_at_war", False):
            continue
        cname = str(getattr(civ, "civ_name", "?") or "?")
        for vc in getattr(civ, "visible_cities", None) or []:
            walls = bool(getattr(vc, "has_walls", False))
            pop = int(getattr(vc, "population", 0) or 0)
            cap = bool(getattr(vc, "is_original_capital", False))
            # 0 排在前面 —— 统治路线下首都得 0,其余得 1
            cap_key = 0 if (domination and cap) else 1
            targets.append((cap_key, 1 if walls else 0, pop, cname, vc))
    targets.sort(key=lambda t: (t[0], t[1], t[2]))
    return targets


def war_campaign(inp: DoctrineInput) -> list[Advice]:
    """军力窗口【兑现计划】—— 把"现在是开战窗口"升级成能执行的表。

    两局赔的是同一笔学费:
      罗马 T335 军力 1.57×(1137)→ 拖到 T350 才动手 → 现代城墙 + 无近战
        → 攻城失败,窗口变成维护费;
      法国局 T55-62 军力 1.63× → 按 9 回合前的旧情报判"雅典无墙"
        → T64 侦察发现有墙,比值已掉回 1.26×,窗口过期大半。

    war_timing 已经在报窗口,但它只有**快照**没有**倒计时**("军力比 1.63×"),
    只有方向没有目标("选一个接壤的弱邻居")。缺的四件事本树补上:
      1. 点名目标城(无墙 > 低人口 > 近)
      2. 编成核对(能占城的 / 能破墙的,各差多少)
      3. 窗口倒计时(对手军力增速外推)—— 集结来不及就直说别打
      4. 情报时效(城墙状态多久没看过了)
    """
    out: list[Advice] = []
    turn = int(inp.turn or 0)
    if not _tier_known(inp) or turn <= 0:
        return out
    # 已经在打了 → 攻城四档归 war_timing,本树不插嘴
    if any(getattr(c, "is_at_war", False) for c in inp.civs or []):
        return out
    if not inp.power.ratio_usable("military"):
        return out
    ratio = inp.power.military_ratio
    if ratio < 1.5:
        return out

    from civ_mcp import war_planner

    mil = _units(inp)
    takers = [
        u
        for u in mil
        if war_planner.can_capture_city(str(getattr(u, "unit_type", "") or ""))
    ]
    breakers = [
        u
        for u in mil
        if war_planner.can_break_walls(str(getattr(u, "unit_type", "") or ""))
    ]

    domination = "DOMINATION" in str(getattr(inp, "victory_type", "") or "").upper()
    targets = _rank_war_targets(inp.civs or [], domination)

    countdown = None
    hist = getattr(inp, "power_history", None)
    if hist is not None:
        try:
            countdown = hist.military_window_turns(inp.power, WAR_WINDOW_FLOOR)
        except Exception:
            countdown = None

    if not targets:
        out.append(
            Advice(
                module="playbook.war_plan",
                priority=Priority.OPTIMIZE,
                action=(
                    f"军力 {ratio:.2f}× 但没有已侦察到的敌城 —— "
                    f"先探图找目标,或把军力兑现成外交筹码"
                ),
                reason="没有目标的军力优势不是资本,是每回合的维护费",
                consequence="优势会随对手科技上升自动归零,不兑现就是白付",
                confidence=Confidence.OPTIONAL,
                alternative="军力强时的和平条款/开放边境要价更高,这也是兑现",
                dedup_key="playbook:war_plan:no_target",
            )
        )
        return out

    cap_key, walls, pop, owner, vc = targets[0]
    tname = str(getattr(vc, "name", "?") or "?")
    tx, ty = getattr(vc, "x", "?"), getattr(vc, "y", "?")
    is_cap = cap_key == 0

    # 编成核对
    gaps: list[str] = []
    if not takers:
        gaps.append("能占城的近战单位 0 个(远程/攻城打空血也占不下城)")
    if walls and not breakers:
        gaps.append("能破墙的单位 0 个(城墙让近战伤害大幅削减)")

    # 集结 ETA 与窗口倒计时的对比 —— 这是本树的核心判断
    verdict = ""
    if gaps:
        replace = _replacement_turns(inp)
        need = (0 if takers else 1) + (0 if (not walls or breakers) else 1)
        eta = int(round(replace * need)) if replace < 90 else None
        if eta is None:
            verdict = "产能未知或过低,补不出这支队 —— 别开战"
        elif countdown is not None and eta > countdown:
            verdict = (
                f"补齐要 {eta} 回合,窗口只剩 {countdown} 回合 —— "
                f"来不及,不要开战。把军力转成外交筹码"
            )
        elif countdown is not None:
            verdict = f"补齐要 {eta} 回合,窗口还剩 {countdown} 回合 —— 来得及,现在就补"
        else:
            verdict = f"补齐约 {eta} 回合"
    elif countdown is not None and countdown <= 3:
        verdict = f"编成齐了,窗口只剩 {countdown} 回合 —— 本回合就宣战"
    else:
        verdict = "编成齐了 —— 可以动手"

    detail = [
        f"目标 {owner} 的 {tname}({tx},{ty}),人口 {pop},"
        f"{'有城墙' if walls else '无城墙'}"
        + ("、【原始首都:打下计统治 +1】" if is_cap else ""),
        f"现有:{len(takers)} 个能占城、{len(breakers)} 个能破墙、共 {len(mil)} 个军事单位",
    ]
    if domination and not is_cap:
        # 统治路线却没有首都可打:说明所有已侦察到的首都都已易主或未探明。
        # 这一句必须说出来 —— 否则读简报的人会以为这座城计分。
        detail.append(
            f"⚠️ {tname} 不是原始首都,打下来【统治计数不涨】 —— "
            f"已侦察范围内没有可打的原始首都,先探图找,"
            f"或明确这一仗是为了削弱对手/夺资源而不是为了胜利条件"
        )
    if walls:
        detail.append(
            f"城墙情报若超过 {INTEL_STALE_TURNS} 回合没更新就先侦察再决定 —— "
            f"法国局 T64 正是按 9 回合前的旧情报判「无墙」,到了才发现有墙"
        )
    if countdown is not None:
        detail.append(
            f"窗口倒计时按对手军力增速外推:约 {countdown} 回合后比值跌破 "
            f"{WAR_WINDOW_FLOOR}×〔线性外推,非精确〕"
        )

    out.append(
        Advice(
            module="playbook.war_plan",
            priority=Priority.CORE,
            action=(
                f"军力窗口 {ratio:.2f}× —— 目标定 {owner} 的 {tname}({tx},{ty})。"
                + (f"缺:{';'.join(gaps)}。" if gaps else "")
                + verdict
            ),
            reason=(
                "窗口会自己关闭:对手科技上升,同样的军队数月后就不再是优势。"
                "两局都死在这一步 —— 罗马 T335 的 1.57× 拖 15 回合、"
                "法国局 T55-62 的 1.63× 拖到掉回 1.26×"
            ),
            consequence=(
                "不兑现成城市,这段优势就只是维护费 —— "
                "而且维护费会一直付到你解散它们为止"
            ),
            confidence=Confidence.RECOMMENDED,
            alternative=(
                "不想开战就把军力换成外交筹码:军力强时的和平/开边境要价更高。"
                "这也算兑现,拖着不做才是纯亏"
            ),
            dedup_key="playbook:war_plan:cash_in",
            detail=tuple(detail),
        )
    )
    return out


def expansion_campaign(inp: DoctrineInput) -> list[Advice]:
    """扩张【计划】—— 把"补 N 座城"升级成带落点、产地和期限的行动表。

    expansion_timing 只回答"该不该补"(诊断),不回答"从哪来、去哪、
    几回合"(计划) —— 与营地问题同构,所以解法同构(camp_campaign 模式)。

    两局实证:罗马局 7+ 开拓者闲置 200 回合、8 城对 13 城;法国局
    T64 只有 2 城对 4 城。两次都不是"不知道该补城"——简报一直在催——
    而是"知道该补,但没有一份说清下一步动作的表"。

    分三支:
      1. 开拓者已在手 → 催落地(这是最贵的闲置:每回合都在烧机会成本)
      2. 没有开拓者且城数落后 → 给产地 + ETA
      3. RED → 静默(话语权让给 expansion_timing 的红警让步版,
         同一件事不说两遍)
    """
    out: list[Advice] = []
    turn = int(inp.turn or 0)
    if turn <= 0:
        return out
    # 红警下 game_state 会直接拒绝造平民,这里静默 —— 两棵树对同一件事
    # 各说一遍,正是 2026-08-06 修掉的"简报自相矛盾"的形态。
    if (inp.threat_level or "").upper() == "RED":
        return out

    median = _rival_city_median(inp)
    mine = len(inp.my_cities or []) or (inp.power.me.cities if inp.power else 0)
    if median is None or not mine:
        return out

    # —— 支 1:开拓者在手 ——
    settlers = [
        u
        for u in (inp.my_units or [])
        if "SETTLER" in str(getattr(u, "unit_type", "") or "").upper()
    ]
    if settlers:
        idle = [
            u
            for u in settlers
            if float(getattr(u, "moves_remaining", 0) or 0)
            >= float(getattr(u, "max_moves", 0) or 0)
            > 0
        ]
        n = len(settlers)
        threats_near = bool(inp.threats)
        out.append(
            Advice(
                module="playbook.expand_plan",
                priority=Priority.CORE,
                action=(
                    f"{n} 个开拓者在手 —— 本回合就调 get_settle_advisor 拿落点并出发,"
                    f"能落地的直接落" + ("(有单位整回合没动)" if idle else "")
                ),
                reason=(
                    f"你 {mine} 座城、对手中位 {median} 座。开拓者停在手里是最贵的闲置:"
                    f"它既不产出也不成长,还占着 1 格人口和维护"
                ),
                consequence=(
                    "罗马局实证:7+ 个开拓者闲置 200 回合,最终 8 城对 13 城 —— "
                    "城市数是复利的底数,晚一回合建城,后面每一回合都少一份产出"
                ),
                confidence=Confidence.RECOMMENDED,
                alternative=(
                    "已探明区域没有合格落点,就先派侦察兵探图再落 —— "
                    "但别让开拓者跟着一起停"
                ),
                subject="unit:settler",
                dedup_key="playbook:expand_plan:land_it",
                detail=(
                    (
                        "威胁在场:开拓者单独出门会被抓,派一个军事单位同格护送"
                        if threats_near
                        else "沿途没有已知威胁,可以直接走"
                    ),
                ),
            )
        )
        return out

    # —— 支 2:没有开拓者且落后 → 给产地和 ETA ——
    if mine >= median:
        return out
    gap = median - mine
    best = None
    for c in inp.my_cities or []:
        p = float(getattr(c, "production", 0) or 0)
        if best is None or p > float(getattr(best, "production", 0) or 0):
            best = c
    prod = float(getattr(best, "production", 0) or 0) if best is not None else 0.0
    from civ_mcp import war_planner

    cost = war_planner.unit_base_cost("UNIT_SETTLER")
    if best is None or prod <= 0 or cost <= 0:
        return out

    eta = int(round(cost / prod))
    name = getattr(best, "name", "产能最高的城")
    gold = float(inp.gold or 0)
    # 金购价与产能成本不是一回事,给不出准确价 —— 只在金币明显充裕时
    # 提示"去查",不报一个编的数字。
    buy_hint = (
        f";金币 {gold:.0f},可以用 purchase_item 查金购价直接买,省下的回合数就是净赚"
        if gold >= 200
        else ""
    )
    out.append(
        Advice(
            module="playbook.expand_plan",
            priority=Priority.CORE if gap >= 2 else Priority.OPTIMIZE,
            action=(
                f"立扩张时间表:{name}(产能 {prod:.0f}/回合)转产开拓者,"
                f"约 {eta} 回合出一个 → 最早 T{turn + eta} 落第 {mine + 1} 座城{buy_hint}"
            ),
            reason=(
                f"你 {mine} 座、对手中位 {median} 座,差 {gap} 座。"
                f"「该补城」这件事简报一直在说,缺的是从哪造、几回合"
            ),
            consequence=(
                "城少 → 学院少 → 科研落后 → 单位代差 → 一切被动。"
                "罗马局 T362 的 0.27× 科研就是这条链走完的样子"
            ),
            confidence=Confidence.RECOMMENDED,
            alternative=(
                "没有安全落点时,攻占比铺城更快 —— 但要先过宣战门禁"
                "(需要能占城的近战单位)"
            ),
            subject="production:civilian",
            dedup_key="playbook:expand_plan:schedule",
            detail=(
                f"开拓者基础成本 {cost} 锤〔gamedata 导出值〕—— "
                f"实际成本随已有城市数逐城上浮,ETA 按基础值算偏乐观",
            ),
        )
    )
    return out


# ===========================================================================
# 决策树 7:区域布局 —— 城市职能分工
# ===========================================================================


def district_roles(inp: DoctrineInput) -> list[Advice]:
    """给每座城一个职能。不给具体格子(district_planner 在做),只给分工。

    分工的意义在于**不要每城每种区域都建**:摊薄的城市每条线都不成立。
    """
    out: list[Advice] = []
    cities = inp.my_cities or []
    if len(cities) < 2:
        return out
    unspecialized = [
        c
        for c in cities
        if len(getattr(c, "districts", None) or []) <= 1
        and int(getattr(c, "population", 0) or 0) >= 4
    ]
    if not unspecialized:
        return out
    names = "、".join(getattr(c, "name", "?") for c in unspecialized[:3])
    out.append(
        Advice(
            module="playbook.district",
            priority=Priority.OPTIMIZE,
            action=f"给 {names} 各定一个专长(1-2 个区域),不要每城都建同一套",
            reason="这些城人口已到 4+ 但只有 ≤1 个区域",
            consequence="每城都摊两三种区域 = 每条线都差一截,相邻加成也吃不满",
            confidence=Confidence.RECOMMENDED,
            alternative="前线城例外:军营 + 城墙,不做产出型专长",
            dedup_key="playbook:district:specialize",
            detail=(
                "分工的常见形态:山多→学院城 ‖ 矿多→工业城 ‖ 沿河→商业城 ‖ 边境→军事城",
                "工业区的产能辐射 6 格 —— 中期就该规划哪几座城共享一个工业区",
            ),
        )
    )
    return out


# ===========================================================================
# 决策树 8:伟人 —— 只补「没用就 pass」这条决策
# ===========================================================================


def great_person_policy(inp: DoctrineInput) -> list[Advice]:
    """lategame 的 great_person 模块负责列可招募的人;这里只给取舍规则。

    每 REVIEW_EVERY 回合提一次,且只在已锁定胜利路线时 —— 没有路线就没有
    取舍标准,这时候提只会变成又一条无法判断的建议。
    """
    if not inp.victory_type or inp.turn < MIDGAME_FROM:
        return []
    if inp.turn % REVIEW_EVERY != 0:
        return []
    return [
        Advice(
            module="playbook.gp",
            priority=Priority.BONUS,
            action=f"过一遍伟人池:与 {inp.victory_type} 无关的直接 pass,把点数留给下一位",
            reason="伟人点数是跨类别累积的,pass 不是浪费而是储蓄",
            consequence="为了「不浪费」去招一个用不上的伟人,才是真的浪费",
            confidence=Confidence.OPTIONAL,
            alternative="对手即将招走一位对你路线关键的伟人时,用金/信仰买断是划算的",
            dedup_key="playbook:gp:review",
        )
    ]


# ===========================================================================
# 决策树 9:中期(T50+)—— 路线复审与军力平衡
# ===========================================================================


def midgame_review(inp: DoctrineInput) -> list[Advice]:
    out: list[Advice] = []
    if inp.turn < MIDGAME_FROM or not _tier_known(inp):
        return out

    # 军力平衡 —— 阈值来自 L1 实测比值,不再是硬编码的绝对数
    ratio = inp.power.military_ratio
    if ratio < 0.7:
        out.append(
            Advice(
                module="playbook.midgame",
                priority=Priority.CORE,
                action="扩军到至少与最强对手持平",
                reason=f"军力比 {ratio:.2f}×",
                consequence=("军力低于 0.7× 时,你的一切发展成果都由对手的善意保管"),
                confidence=Confidence.RECOMMENDED,
                alternative="买单位比造快;边境城造墙的性价比高于野战单位",
                dedup_key="playbook:midgame:rearm",
            )
        )

    # 路线复审
    if inp.turn % REVIEW_EVERY == 0:
        axis = inp.power.strongest_axis
        line = "、".join(
            f"{DIM_CN.get(r.dim, r.dim)} {r.value:.2f}×"
            for r in sorted(
                (r for r in inp.power.ratios.values() if r.usable),
                key=lambda r: -r.value,
            )[:3]
        )
        out.append(
            Advice(
                module="playbook.midgame",
                priority=Priority.CORE,
                action=(
                    f"T{inp.turn} 路线复审:你现在的相对优势是 {line}"
                    + (
                        f" —— 路线应该压在【{DIM_CN.get(axis.dim, axis.dim)}】上"
                        if axis is not None
                        else ""
                    )
                ),
                reason="每 20 回合复审一次,是因为优势会随对手的选择漂移",
                consequence="沉没成本不是留在原路线上的理由",
                confidence=Confidence.RECOMMENDED,
                alternative=(
                    "确认要换道就用 set_victory_goal 显式改,别让路线停留在口头上"
                ),
                dedup_key=f"playbook:midgame:review:{inp.turn}",
            )
        )
    return out


# ===========================================================================
# 决策树 10:危机响应
# ===========================================================================


def crisis_response(inp: DoctrineInput) -> list[Advice]:
    """被宣战后的分层响应。crisis_manager 负责报事件,这里给动作顺序。"""
    out: list[Advice] = []
    new_wars = [c for c in inp.civs or [] if getattr(c, "is_at_war", False)]
    if not new_wars:
        return out
    civilians = [
        u
        for u in inp.my_units or []
        if int(getattr(u, "combat_strength", 0) or 0) <= 0
        and int(getattr(u, "ranged_strength", 0) or 0) <= 0
    ]
    if civilians:
        names = "、".join(getattr(u, "name", "?") for u in civilians[:3])
        out.append(
            Advice(
                module="playbook.crisis",
                priority=Priority.SURVIVAL,
                action=f"本回合先把平民收回城内:{names}",
                reason=f"与 {len(new_wars)} 家交战中,野外平民会被直接俘虏",
                consequence="一个建造者约等于 5-7 回合产能,而且它带走的是已投入的成本",
                confidence=Confidence.CERTAIN,
                dedup_key="playbook:crisis:civilians",
            )
        )
    if _tier_known(inp) and inp.power.military_ratio < 0.7:
        out.append(
            Advice(
                module="playbook.crisis",
                priority=Priority.CORE,
                action="转防守消耗:边境城造/买城墙,单位进城不野战",
                reason=f"军力比 {inp.power.military_ratio:.2f}×,野战会被逐个吃掉",
                consequence="",
                confidence=Confidence.RECOMMENDED,
                alternative="10 回合冷却后立刻评估求和 —— 长期消耗战里落后方只会更落后",
                dedup_key="playbook:crisis:turtle",
            )
        )
    return out


# ===========================================================================
# 汇总
# ===========================================================================

TREES = (
    opening_queue,
    barbarian_response,
    camp_campaign,
    neighbor_policy,
    research_rationale,
    uu_timing_push,
    war_timing,
    war_campaign,
    expansion_timing,
    expansion_campaign,
    district_roles,
    great_person_policy,
    midgame_review,
    crisis_response,
)


def evaluate(inp: DoctrineInput) -> list[Advice]:
    """跑全部决策树。单棵失败不影响其余 —— 策略层不许拖垮简报。"""
    out: list[Advice] = []
    for tree in TREES:
        try:
            out.extend(tree(inp))
        except Exception:
            continue
    return out
