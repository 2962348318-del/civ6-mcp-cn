"""任务 N —— 总督策略。

⚠️ 先说最重要的一条,否则这个模块会给出对本机无效的建议:

    **本机规则集是 RULESET_STANDARD(原版,无资料片),导出的 40 张表里
    没有 Governors / GovernorPromotions 表**(实测 `_all.json` 的 tables 键里
    确实没有)。总督是 Rise & Fall 引入的系统。

    所以本模块【不硬编码任何总督类型、晋升名或数值】。它只做两件事:
      1. 对着 `get_governors` 的实时返回做确定性检查(有没有点数没花、
         有没有总督没派、派了但没就任)——这些检查不依赖任何总督知识。
      2. 如果实时返回里【确实存在】某个总督类型,再按胜利路线给优先级。
         匹配靠 governor_type 字符串,匹配不上的一律不评论,不猜。

    如果实时返回是空的(本局没有总督系统),模块返回空列表,一个字都不输出。

这是被上一轮"地形移动力表凭印象编"打脸后定下的做法:
规则要么从游戏库导,要么从实时返回读,不能凭常识写。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# governor_type 关键词 → (职能, 最契合的胜利路线)。
# 只用于给【实时返回里确实存在】的总督排序;不构成"总督存在"的断言。
# 关键词取自 server.py 文档串里出现过的类型名(GOVERNOR_THE_EDUCATOR /
# GOVERNOR_THE_DEFENDER),其余靠通用词根匹配,匹配不上就不评论。
_ROLE_KEYWORDS: tuple[tuple[str, str, str], ...] = (
    ("EDUCATOR", "科技产出", "SCIENCE"),
    ("DEFENDER", "城防与忠诚", "DOMINATION"),
    ("BUILDER", "产能与砍树", "DOMINATION"),
    ("MERCHANT", "金币与购买", ""),
    ("AMBASSADOR", "城邦与外交", ""),
    ("CARDINAL", "信仰与宗教", "RELIGION"),
    ("ENGINEER", "基建与城市改良", ""),
)


@dataclass(frozen=True)
class Governor:
    """一位总督的实时状态。字段与 lua/models.py 的 AppointedGovernor 对齐。"""

    governor_type: str
    name: str = ""
    assigned_city_id: int = -1
    assigned_city_name: str = ""
    is_established: bool = False
    turns_to_establish: int = 0
    available_promotions: tuple[str, ...] = ()


@dataclass
class GovernorContext:
    """总督判断所需的事实,全部来自 get_governors 的实时返回。"""

    points_available: int = 0
    appointed: list[Governor] = field(default_factory=list)
    # available_to_appoint 里的 governor_type
    available_types: tuple[str, ...] = ()
    victory_type: str = ""
    # 城市 id -> 名字,用于把"未派驻"变成可执行的建议
    city_names: dict[int, str] = field(default_factory=dict)
    # 处于威胁下的城市名(RED 警戒),优先派防御型总督
    threatened_cities: tuple[str, ...] = ()


def _role(governor_type: str) -> tuple[str, str] | None:
    """从 governor_type 推职能。匹配不上返回 None —— 不猜。"""
    up = (governor_type or "").upper()
    for keyword, role, victory in _ROLE_KEYWORDS:
        if keyword in up:
            return role, victory
    return None


def rank_for_victory(types: tuple[str, ...], victory_type: str) -> list[str]:
    """按胜利路线给可任命的总督排序。只排能识别职能的,其余保持原序附在后面。"""
    vt = (victory_type or "").upper()
    matched: list[str] = []
    known: list[str] = []
    unknown: list[str] = []
    for gt in types:
        role = _role(gt)
        if role is None:
            unknown.append(gt)
        elif vt and role[1] == vt:
            matched.append(gt)
        else:
            known.append(gt)
    return matched + known + unknown


def get_governor_advice(ctx: GovernorContext, max_items: int = 4) -> list[str]:
    """总督建议。本局没有总督系统(实时返回全空)时返回空列表。"""
    has_system = bool(ctx.appointed or ctx.available_types or ctx.points_available)
    if not has_system:
        return []

    out: list[str] = []

    # 1. 点数闲置 —— 总督点数不花就是纯亏,而且它不会累积成任何东西。
    if ctx.points_available > 0 and ctx.available_types:
        ranked = rank_for_victory(ctx.available_types, ctx.victory_type)
        best = ranked[0]
        role = _role(best)
        reason = ""
        if role:
            reason = f"({role[0]}"
            if ctx.victory_type and role[1] == ctx.victory_type.upper():
                reason += f",最契合 {ctx.victory_type.upper()} 路线"
            reason += ")"
        out.append(
            f"⚠️ 有 {ctx.points_available} 点总督点数没花 —— "
            f"点数闲置不产生任何收益。推荐先任命 {best}{reason},"
            f"用 appoint_governor 任命。"
        )
    elif ctx.points_available > 0:
        out.append(
            f"⚠️ 有 {ctx.points_available} 点总督点数没花 —— "
            f"用 get_governors 看可任命/可晋升的选项,点数闲置是纯亏。"
        )

    # 2. 已任命但没派驻 —— 最常见的浪费:任命完就忘了。
    idle = [g for g in ctx.appointed if g.assigned_city_id < 0]
    for g in idle:
        line = (
            f"⚠️ {g.name or g.governor_type} 已任命但未派驻任何城市 —— 完全不产生效果。"
        )
        if (
            ctx.threatened_cities
            and (_role(g.governor_type) or ("", ""))[1] == "DOMINATION"
        ):
            line += (
                f" 当前受威胁的城市:{('、'.join(ctx.threatened_cities[:2]))},优先考虑。"
            )
        line += " 用 assign_governor 派驻。"
        out.append(line)

    # 3. 派驻中但还没就任 —— 不是问题,是提醒别重复操作。
    establishing = [
        g for g in ctx.appointed if g.assigned_city_id >= 0 and not g.is_established
    ]
    for g in establishing[:1]:
        turns = g.turns_to_establish
        tail = f",还需 {turns} 回合" if turns > 0 else ""
        out.append(
            f"💡 {g.name or g.governor_type} 正在 {g.assigned_city_name} 就任中{tail} —— "
            f"就任前不产生效果,别急着调走。"
        )

    # 4. 有可用晋升没点 —— 晋升是免费的战力,唯一成本是记得点。
    upgradable = [g for g in ctx.appointed if g.available_promotions]
    if upgradable and ctx.points_available > 0:
        names = "、".join(g.name or g.governor_type for g in upgradable[:2])
        out.append(
            f"💡 {names} 有可用晋升,而你还有点数 —— "
            f"用 promote_governor 点掉,晋升效果立即生效。"
        )

    if not out:
        return []
    if len(out) > max_items:
        omitted = len(out) - max_items
        out = out[:max_items]
        out.append(f"   (另有 {omitted} 条总督建议已省略)")
    return ["== 总督 ==", *out]
