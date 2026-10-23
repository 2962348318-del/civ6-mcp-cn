"""任务 R —— 危机管理预案。

safety.py 拦的是"还没发生的送死";这里管的是"已经发生的坏事"。危机无法
阻止(宣战是对面按的按钮),能做的是**在发生的那一回合把损失最小化的
方案直接拍在简报第一行** —— 而不是让模型在慌乱中自由发挥。

三个预案全部是确定性组装:局面数据进,行动清单出。每段 ≤8 行。
输出格式对齐计划书任务 R 的三个示例。

数值诚实性:
    · 驻军的城防加成、城墙数值、总督忠诚数值均未导出 —— 文案只说"做什么",
      不承诺具体数字;能算的(忠诚叛变回合数)按计划书公式算。
    · 预计战线方向由调用方传入(来自实时观测),本模块不猜。
"""

from __future__ import annotations

# 忠诚公式与 world_model.loyalty_pressure 同源(计划书近似):
# 新占/受压城市满忠诚 100,净压力为负时 ≈ 100/|净值| 回合叛变。
LOYALTY_FULL = 100.0
MAX_LINES = 8


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 预案一:被宣战
# ---------------------------------------------------------------------------


def war_declared_crisis(attacker, game_state: dict | None = None) -> list[str]:
    """被宣战当回合的存活策略。≤8 行,简报第一行级别。不抛异常。

    attacker: dict/鸭子类型 —— civ_name, military_strength, direction(可选,
              如"西",来自实时观测,本模块不猜战线)。
    game_state: {"my_military", "civilians_outside"(城外平民数),
                 "cities_without_walls"(无墙城名列表), "gold"}
    """
    try:
        gs = game_state if isinstance(game_state, dict) else {}
        if isinstance(attacker, dict):
            name = str(attacker.get("civ_name") or attacker.get("name") or "敌方")
            their = _int(attacker.get("military_strength"))
            direction = str(attacker.get("direction") or "")
        else:
            name = getattr(attacker, "civ_name", "敌方") or "敌方"
            their = _int(getattr(attacker, "military_strength", 0))
            direction = str(getattr(attacker, "direction", "") or "")
        mine = _int(gs.get("my_military"))

        head = f"🔴 {name}向你宣战!"
        if their and mine:
            head += f"军力 {their} vs 你 {mine}"
            if mine and their >= mine * 2:
                head += "——硬拼必输,目标是守住等他厌战"
        out = [head, "当前存活策略:"]

        civs_out = _int(gs.get("civilians_outside"))
        if civs_out:
            out.append(
                f"· 立即把城外 {civs_out} 个平民撤回城内——他的骑兵第一目标就是抓平民"
            )
        else:
            out.append("· 确认没有平民在城外(有就立刻撤)")
        out.append("· 每座城至少 1 个驻军——驻军吃城防加成(数值未导出,机制如此)")
        no_walls = [str(c) for c in (gs.get("cities_without_walls") or [])]
        if no_walls:
            out.append(f"· {('、'.join(no_walls[:2]))} 没有城墙——生产立刻切城墙")
        out.append("· 首都优先造远程——躲在城防后面射,让他啃墙")
        gold = _int(gs.get("gold"))
        if gold >= 200:
            out.append(
                f"· 国库 {gold} 金——立即买 1-2 个最便宜的单位补防,"
                f"金币换时间,这时候不花更待何时"
            )
        if direction:
            out.append(
                f"· 预计战线:{name}从{direction}面来(观测值)——"
                f"{direction}部城市是主战场,兵力先往那边调"
            )
        return out[:MAX_LINES]
    except Exception:
        return ["🔴 被宣战!平民回城、每城驻军、边境切城墙、首都造远程——先活下来。"]


# ---------------------------------------------------------------------------
# 预案二:平民被杀
# ---------------------------------------------------------------------------


def civilian_killed_crisis(
    victim_type: str, location, game_state: dict | None = None
) -> list[str]:
    """建造者/开拓者死亡后的止损。≤8 行。不抛异常。

    location: (x, y)。game_state: {"camps_nearby"(6 格内已知营地数),
    "killer"(凶手描述,可选), "rebuild_cost"(重造产能,可选)}
    """
    try:
        gs = game_state if isinstance(game_state, dict) else {}
        try:
            x, y = int(location[0]), int(location[1])
            loc = f"({x},{y})"
        except (TypeError, ValueError, IndexError):
            loc = "(位置未知)"
        victim = str(victim_type or "平民")
        for k, cn in (("SETTLER", "开拓者"), ("BUILDER", "建造者")):
            if k in victim.upper():
                victim = cn
                break

        out = [f"⚠️ {victim}在 {loc} 被杀——该格已进死亡格档案,别再送第二个。"]
        killer = str(gs.get("killer") or "")
        if killer:
            out.append(f"· 凶手:{killer}——它还在附近,先处理它再谈重建。")
        camps = _int(gs.get("camps_nearby"))
        if camps:
            out.append(
                f"· 附近有 {camps} 个未拆营地——营地不拆、出兵不停,这不是最后一个死者。"
            )
            out.append("· 下一个平民生产前,先用军事单位清掉 6 格内的营地。")
        else:
            out.append(
                "· 6 格内没有已知营地——凶手可能来自迷雾,先派军事单位探那片黑,再走平民。"
            )
        cost = _int(gs.get("rebuild_cost"))
        if cost:
            out.append(
                f"· 重造要 {cost} 锤——这笔学费的教训:平民必须"
                f"与军事单位【同格】才受保护,相邻不算(实机验证过)。"
            )
        else:
            out.append(
                "· 教训:平民必须与军事单位【同格】才受保护,"
                "相邻护卫拦不住抓人(实机验证过)。"
            )
        return out[:MAX_LINES]
    except Exception:
        return ["⚠️ 平民被杀——查死亡格档案,拆营地,下个平民带同格护卫。"]


# ---------------------------------------------------------------------------
# 预案三:忠诚度崩溃
# ---------------------------------------------------------------------------


def loyalty_collapse_crisis(city, pressure: float) -> list[str]:
    """忠诚度崩溃时的挽救方案。≤8 行。不抛异常。

    city: dict/鸭子类型 —— name, loyalty(当前值,缺省按满 100 算)。
    pressure: 每回合净忠诚变化(负数 = 在流失;来自 loyalty_pressure 或实测)。
    """
    try:
        try:
            pressure = float(pressure)
        except (TypeError, ValueError):
            return []
        if pressure >= 0:
            return []  # 没在流失就不是危机,不占版面
        if isinstance(city, dict):
            name = str(city.get("name") or "该城")
            loyalty = float(city.get("loyalty") or LOYALTY_FULL)
        else:
            name = getattr(city, "name", "该城") or "该城"
            loyalty = float(getattr(city, "loyalty", LOYALTY_FULL) or LOYALTY_FULL)

        turns = max(1, int(loyalty / abs(pressure)))
        out = [
            f"🔴 {name}忠诚每回合 {pressure:+.1f},当前 {loyalty:.0f}"
            f"——约 {turns} 回合后叛变(计划书公式近似)。可做的事,按见效排序:"
        ]
        out.append(
            "· ① 立刻派防御型总督(如 Victor)——总督忠诚加成是最大单项"
            "(数值未导出,以游戏内为准)"
        )
        out.append("· ② 驻军 + 保持宜居度不红——两者都是小额加成,叠着用")
        out.append("· ③ 买纪念碑(有忠诚回复的建筑先买不建)")
        out.append("· ④ 涨人口:人口就是忠诚压力,饥饿的城守不住")
        if turns <= 5:
            out.append(
                f"· 只剩 {turns} 回合——若上面几项都来不及/不想投:"
                f"叛变前把能卖的建筑卖掉换金,别把整座城白送。"
            )
        else:
            out.append("· 若评估后决定放弃:趁忠诚归零前卖建筑止损,别拖到白送。")
        return out[:MAX_LINES]
    except Exception:
        return []
