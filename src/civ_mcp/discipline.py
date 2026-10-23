"""L4 执行纪律层 —— 把 Agent 自己的手管住。

═══════════════════════════════════════════════════════════════════════════
 设计文档(本 docstring 即交付物)
═══════════════════════════════════════════════════════════════════════════

## 0. 这一层为什么必须存在

safety.py 治的是**工具没告诉我**(平民旁边有骑手、远程先移动了)。
本模块治的是**工具告诉我了、我还是做错了**。

校准局两次实证,两次都赔掉关键单位:

    T22  全国唯一的勇士去追蛮族侦察兵,阵亡。当时 1 城 4 产能,
         补员要 10 回合,开局差点直接崩盘。
    T108 罗马军团 51 血继续攻开罗,阵亡。工具**明白返回了 `hp:12`** ——
         没有任何信息缺失,是推进惯性压过了止损。

这类问题写进文档没用(下一个会话的我照样会忘),必须做成**执行前拦截**。
拦截风格沿用 safety.py:说明原因 + 给替代方案 + 告知可 `force=True` 越过。

## 1. 四条拦截(方案 v4 §L4)

| 码 | 触发 | 实证 |
|---|---|---|
| `LAST_MILITARY_UNIT`  | 全国唯一军事单位【近战】主动进攻 | T22 |
| `LOW_HP_ASSAULT`      | 近战单位 <60% HP 攻【城市】 | T108 |
| `STAGGERED_ASSAULT`   | 近战攻城时援军还在路上,单位先上 | T69 |
| `UNREAD_COMBAT_STATE` | 同一单位连续攻击,期间没读过 get_units | 上面两条的共同根因 |

三条基于 `MoveContext` 的拦截**全部只对近战生效**(`ctx.is_ranged` 一律放行),
理由见 §1.1。

### 1.1 为什么只拦近战、只拦攻城

远程单位攻击零反伤(校准局 #6 修复后已确认),残血远程炮击城市是**正解**
而不是错误;野战里残血单位补最后一刀也常常是对的。把拦截限定在
「近战 × 攻城」这个交集上,是为了让拦截**几乎不误伤**——误伤一次,
下一个我就会养成无脑 force 的习惯,那这层就白做了。

#### 1.1a LAST_MILITARY_UNIT 漏了远程(2026-08-06 修)

初版的 `check_last_military_unit` 忘了加 `is_ranged` 放行,和同文件的
`check_low_hp_assault` 口径不一致。法国局 T64 实证:弓箭手作为全国唯一
军事单位在城内远程攻击,被拦 **7 次,7 次全部 force 通过,零损失**,
每次都造成击杀或重创。

这正是 §1.1 警告的那种误伤——而且是最坏的一种:连续 7 次「拦了、越过、
没事」直接把 force 训练成了肌肉记忆,这一层对后面真正该拦的那一次也就
失效了。本条拦截的全部理由是「唯一单位阵亡 = 城市裸奔」,而远程攻击
不承受反击,阵亡这个前提根本不成立,所以拦得没有依据。

### 1.2 UNREAD_COMBAT_STATE 的机制

游戏引擎在同一帧内读不到战后 HP(校准局 0d,6 次实证)。所以「连打两次」
本质上是拿**过期的血量**做第二次决策。0d 的修复让返回值显示真实战后血,
但只有**重新读一次**才拿得到全局状态(反击、援军、ZOC 变化)。
本拦截要求两次攻击之间必须有一次 `get_units`,把"读一眼"变成硬约束。

## 2. 零新增往返

四条拦截需要的事实全部由 `safety.build_move_safety_query()` 一次带回
(该查询在 `attack_unit(force=False)` 时本来就会跑)。本模块只加解析字段,
不加 Lua 往返 —— 方案 v4 §4「明确不做」的硬约束。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field

from civ_mcp.safety import PASS, REINFORCE_RADIUS, MoveContext, Verdict

# 近战攻城的血量下限(百分比)。低于此值继续攻城 = T108 的复刻。
LOW_HP_PCT = 60

__all__ = [
    "LOW_HP_PCT",
    "REINFORCE_RADIUS",
    "REINFORCE_MIN",
    "CombatMemory",
    "check_attack",
    "check_last_military_unit",
    "check_low_hp_assault",
    "check_staggered_assault",
    "check_unread_combat_state",
]

# 有多少援军在半径内才算「值得等」。1 个不算 —— 等 1 个兵不值得让战线停一回合。
REINFORCE_MIN = 2


# ---------------------------------------------------------------------------
# 跨调用状态:谁刚打过、什么时候读过单位
# ---------------------------------------------------------------------------


@dataclass
class CombatMemory:
    """记录本回合每个单位的攻击次数,以及最近一次 get_units 的时点。

    时点用一个单调递增的计数器表示,不用真实时间 —— 时间会被网络延迟
    干扰,而"读过没有"是个纯粹的顺序问题。
    """

    _clock: int = 0
    _last_read: int = -1
    # unit_index -> (回合, 该回合攻击次数, 最后一次攻击的时点)
    _attacks: dict[int, tuple[int, int, int]] = field(default_factory=dict)

    def notice_units_read(self) -> None:
        """调用方读过 get_units —— 战场状态已刷新。"""
        self._clock += 1
        self._last_read = self._clock

    def notice_attack(self, unit_index: int, turn: int) -> None:
        """记一次已执行的攻击。"""
        self._clock += 1
        prev = self._attacks.get(unit_index)
        count = prev[1] + 1 if prev and prev[0] == turn else 1
        self._attacks[unit_index] = (turn, count, self._clock)

    def attacked_without_reread(self, unit_index: int, turn: int) -> bool:
        """该单位本回合打过,且此后没有人读过单位状态。"""
        prev = self._attacks.get(unit_index)
        if not prev or prev[0] != turn:
            return False
        return self._last_read < prev[2]

    def attacks_this_turn(self, unit_index: int, turn: int) -> int:
        prev = self._attacks.get(unit_index)
        return prev[1] if prev and prev[0] == turn else 0

    def new_turn(self, turn: int) -> None:
        """回合切换时清掉过期记录(不清也不会错,只是会长)。"""
        self._attacks = {
            uid: rec for uid, rec in self._attacks.items() if rec[0] >= turn - 1
        }


# ---------------------------------------------------------------------------
# 拦截
# ---------------------------------------------------------------------------


def _name(ctx: MoveContext) -> str:
    return ctx.unit_type.replace("UNIT_", "") or "该单位"


def check_last_military_unit(ctx: MoveContext, force: bool = False) -> Verdict:
    """规则 1:全国唯一【近战】单位不许主动进攻(T22 实证)。

    唯一军事单位阵亡 = 城市裸奔 + 补员期间任何一个蛮族都能终结这局。
    进攻的收益上限是杀一个兵,风险下限是输掉整局 —— 后悔值完全不对称。

    远程单位放行,口径与 check_low_hp_assault 一致(见 docstring §1.1a):
    远程攻击零反伤,「唯一单位阵亡」这个前提不成立,拦了就是纯误伤。
    法国局 T64 唯一弓箭手被拦 7 次、7 次 force 通过、零损失。

    刻意不给非阻断提醒(远程攻完会耗尽移动力、暴露在敌方下回合打击范围内):
    safety.Verdict 只有 blocked/code/message 三个字段,而调用方
    game_state.attack_unit 只在 `verdict.blocked` 为真时才把 message 通过
    as_error() 呈现出来 —— 放行路径上的 message 会被直接丢弃。要做提醒得改
    Verdict 和调用方,那是另一件事;这里宁可静默放行,也不塞一个到不了人眼前
    的字段假装提醒过了。
    """
    if force or not ctx.found or ctx.is_civilian or ctx.is_ranged:
        return PASS
    if ctx.my_military_total != 1:
        return PASS
    return Verdict(
        blocked=True,
        code="LAST_MILITARY_UNIT",
        message=(
            f"{_name(ctx)} 是你**全国唯一**的军事单位,主动进攻的风险与收益完全不对称:"
            f"赢了杀一个兵,输了城市裸奔、补员期间任何威胁都能终结这局。"
            f" 校准局 T22 就是这么赔掉开局唯一的勇士的(当时补员要 10 回合)。"
            f" 可选做法:①回城驻防/堡垒,让对方来攻你(防守方吃地形和城防加成);"
            f" ②只做驱离——移动到威胁与平民之间,不发起攻击;"
            f" ③先补第二个军事单位再考虑进攻。"
            f" 确认要冒险请传 force=True。"
        ),
    )


def check_low_hp_assault(ctx: MoveContext, force: bool = False) -> Verdict:
    """规则 2:近战单位 <60% HP 不许攻城(T108 实证)。

    只拦近战 × 攻城,理由见模块 docstring §1.1。
    """
    if force or not ctx.found or ctx.is_civilian or ctx.is_ranged:
        return PASS
    if not ctx.target_is_enemy_city:
        return PASS
    pct = ctx.hp_pct
    if pct is None or pct >= LOW_HP_PCT:
        return PASS
    city = ctx.enemy_city_name or "该城"
    return Verdict(
        blocked=True,
        code="LOW_HP_ASSAULT",
        message=(
            f"{_name(ctx)} 当前 {ctx.hp}/{ctx.max_hp} 血({pct:.0f}%),"
            f"低于攻城血线 {LOW_HP_PCT}%。城市有城防加成且会**反击**,"
            f"残血近战攻城是校准局 T108 赔掉罗马军团的原样复刻"
            f"(当时 51 血,攻开罗,阵亡)。"
            f" 可选做法:①原地 fortify 回血(城外每回合回 10,己方领土内更快),"
            f"两三回合后满血再上;②换满血单位打这一下,残血的退到二线;"
            f"③先用远程单位把 {city} 的城防磨掉再近战进场——远程攻城零反伤。"
            f" 攻城不差这两回合,少一个近战单位才是真的推不动。"
            f" 确认要冒险请传 force=True。"
        ),
    )


def check_staggered_assault(ctx: MoveContext, force: bool = False) -> Verdict:
    """规则 3:攻城时援军还在路上,不许单位先上(T69 实证)。

    分批投入 = 让对方逐个吃掉。攻城的正确形态是同一回合多个单位一起到位。
    """
    if force or not ctx.found or ctx.is_civilian or ctx.is_ranged:
        return PASS
    if not ctx.target_is_enemy_city:
        return PASS
    if ctx.adjacent_military > 0:
        return PASS  # 已经有友军贴住目标,不算分批
    if ctx.nearby_military < REINFORCE_MIN:
        return PASS  # 本来就没有援军可等,拦了也没用
    return Verdict(
        blocked=True,
        code="STAGGERED_ASSAULT",
        message=(
            f"{_name(ctx)} 要单独攻打 {ctx.enemy_city_name or '这座城'},"
            f"但目标周围没有任何己方军事单位贴住,而 {REINFORCE_RADIUS} 格内"
            f"还有 {ctx.nearby_military} 个己方军事单位没到位。"
            f" 分批进场 = 让守军逐个吃掉:第一个单位挨完全部反击后退场,"
            f"第二个单位下回合面对的还是满城防。校准局 T69 就是这么送的。"
            f" 可选做法:①这回合让所有参战单位移动到发起线(城市相邻格),"
            f"**下回合一起打**;②如果有远程单位,让它先开始磨城防,近战等集结完毕;"
            f"③确实只想试探城防,用远程打一发(零反伤)而不是近战。"
            f" 确认要单独上请传 force=True。"
        ),
    )


def check_unread_combat_state(
    memory: CombatMemory, unit_index: int, turn: int, force: bool = False
) -> Verdict:
    """规则 4:同一单位连续攻击之间必须重读一次单位状态。

    依据见模块 docstring §1.2 —— 同帧读不到战后 HP,连打两次等于拿过期
    血量做第二次决策。
    """
    if force:
        return PASS
    if not memory.attacked_without_reread(unit_index, turn):
        return PASS
    n = memory.attacks_this_turn(unit_index, turn)
    return Verdict(
        blocked=True,
        code="UNREAD_COMBAT_STATE",
        message=(
            f"该单位本回合已经攻击 {n} 次,而这之后你没有再调过 get_units。"
            f" 游戏引擎在同一帧内不刷新战后状态,你手上的血量、反击结果、"
            f"敌方援军位置都可能是过期的——校准局两次赔单位都是这个根因。"
            f" 先调一次 get_units 看真实血量再决定要不要补这一刀"
            f"(只要读过就自动放行,不需要 force)。"
        ),
    )


def check_attack(
    ctx: MoveContext,
    memory: CombatMemory | None = None,
    unit_index: int = -1,
    turn: int = 0,
    force: bool = False,
) -> Verdict:
    """按后悔值从高到低跑全部攻击前拦截,返回第一条命中的。

    顺序即优先级:输掉整局 > 赔一个单位 > 打得低效 > 信息过期。
    永不抛异常 —— 拦截层自己炸掉会连累正常操作。
    """
    try:
        if memory is not None and unit_index >= 0:
            v = check_unread_combat_state(memory, unit_index, turn, force)
            if v.blocked:
                return v
        for check in (
            check_last_military_unit,
            check_low_hp_assault,
            check_staggered_assault,
        ):
            v = check(ctx, force)
            if v.blocked:
                return v
        return PASS
    except Exception:
        return PASS
