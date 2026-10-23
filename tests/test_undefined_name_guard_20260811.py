"""2026-08-11 罗马局 T228 暴露的第三批缺陷回归测试。

  (80) get_gp_advisor 直接崩:MCP 工具签名收的是 unit_index,函数体里写的是
       unit_id —— 调用必然 NameError。这条从来没被跑到过,因为没有测试碰
       这个工具,而 lint 也没进门禁。

  (81) server.py 的可见度差分播种引用了未导入的 lq,NameError 被外面那圈
       `except Exception: log.debug(...)` 吞掉 —— 于是 spatial 的
       revealed-tiles 从来没被播种过,而且日志里一个字都看不到。

  (82) fortify 对睡着的单位静默降级,并且撒谎:CanStartOperation(FORTIFY)
       对睡眠中的单位恒为 false,于是代码掉进 SLEEP 兜底分支,给前线火枪手
       回了一句「这种单位不能进入防御姿态」—— 火枪手当然能。实际结果是它
       继续睡:没有 +6 防御加成,敌人贴脸也不会自动唤醒,而这句话让人以为
       是单位类型限制,永远不会重试。现场用 run_lua 验证过:同一个单位
       CanStartCommand(WAKE) 为 true,即它就是睡着的。

  (83) 游戏自己把回合结束了,整回合日记消失:AutoEndTurn 默认开着,
       skip_remaining_units 把所有单位 FinishMoves 之后,游戏判定「玩家没有
       待办」直接结束回合 —— end_turn 从没被调用。实机后果是 T228 的日记
       条目根本不存在,而下一次 end_turn 读到的 ov.turn 已是 229,于是 T228
       的内容被写进 T229,日记与回合号整体错位;AI 回合也在 agent 不知情时
       跑完。日记是跨会话记忆的唯一载体,回合推进必须只有 end_turn 一条路径。

  (84) 近战战报把「正在赢」报成「完全没打动」:火枪手(CS55)打英国重装
       步兵(CS45),预估对敌 52 伤害,战报却写 enemy HP:100 -> 100/100,
       只有我方 HP 100 -> 66 是真的。几秒后 get_units 读到敌方真值 66/100
       —— 伤害确实打进去了。根因是战后读数只等了 0.5 秒;代码里那句
       「同一回合帧内无论等多久都读不到战后 HP」的断言实测不成立,等够
       就读得到。这比伦敦那次的假成功更贵:假成功只是浪费兵力,假失败会
       劝你放弃一场正在赢的攻击。

这五条是同一类缺陷:**运行期才暴露的假成功/假失败**。所以除了各自的回归
断言,这里把 ruff F821 拉进门禁,让整类问题不再复发。
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


def strip_lua_comments(code: str) -> str:
    """剥掉 Lua 行注释再做「不该出现」断言。

    这一批里踩了三次同一个坑:注释中提到旧写法(用来解释修了什么),
    断言 `"旧写法" not in code` 就误报。要断言的是**可执行代码**。
    """
    return "\n".join(
        ln for ln in code.splitlines() if not ln.lstrip().startswith("--")
    )


class TestNoUndefinedNamesInSrc:
    """(80)(81) 整类门禁:src/ 下不许有未定义名字。"""

    def test_ruff_f821_clean(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--select",
                "F821",
                "--output-format",
                "concise",
                str(SRC),
            ],
            capture_output=True,
            text=True,
        )
        # game_launcher 用 "PIL.Image.Image" 做延迟标注,PIL 只在函数里 import。
        real = [
            ln
            for ln in proc.stdout.splitlines()
            if "F821" in ln and "Undefined name `PIL`" not in ln
        ]
        assert not real, "src/ 存在未定义名字(运行期才会炸):\n" + "\n".join(real)


class TestGpAdvisorSignature:
    """(80) 工具参数名和函数体必须对得上。"""

    def test_tool_body_uses_its_own_parameter(self):
        from civ_mcp import server

        fn = getattr(server.get_gp_advisor, "fn", server.get_gp_advisor)
        src = inspect.getsource(fn)
        assert "gs.get_gp_advisor(unit_index)" in src
        assert "gs.get_gp_advisor(unit_id)" not in src

    def test_every_tool_body_only_uses_declared_params(self):
        """把同类错误一次扫干净:工具函数体里出现的 *_id / *_index 形参名,
        必须真的在签名里。"""
        from civ_mcp import server

        source = Path(server.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        suspects = {"unit_id", "unit_index", "city_id", "player_id", "other_player_id"}
        bad: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            declared = {
                a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            }
            if not declared & suspects:
                continue
            # 收集本函数(含嵌套 _run)里被读取的可疑名字
            used = {
                sub.id
                for sub in ast.walk(node)
                if isinstance(sub, ast.Name)
                and isinstance(sub.ctx, ast.Load)
                and sub.id in suspects
            }
            # 嵌套函数自己声明的同名参数不算
            for missing in sorted(used - declared):
                bad.append(f"{node.name}(): 用了 {missing},但签名里只有 {sorted(declared)}")

        assert not bad, "工具函数引用了不存在的形参:\n" + "\n".join(bad)


class TestRevealedTileSeedWired:
    """(81) 播种路径必须真的能跑,失败也必须看得见。"""

    def test_lq_is_imported(self):
        from civ_mcp import server

        assert hasattr(server, "lq"), "server.py 没有导入 lua 查询模块"
        assert callable(server.lq.build_revealed_tiles_seed_query)
        assert callable(server.lq.parse_revealed_tiles_seed)

    def test_seed_failure_is_not_debug_level(self):
        from civ_mcp import server

        source = Path(server.__file__).read_text(encoding="utf-8")
        idx = source.index("Failed to seed revealed tiles")
        line = source[source.rindex("\n", 0, idx) + 1 : source.index("\n", idx)]
        assert "log.warning" in line, (
            "播种失败必须至少 warning —— 之前是 debug,NameError 被静默吞了整局"
        )


class TestFortifyWakesSleepingUnits:
    """(82) 睡着的单位要先唤醒,失败信息不许把状态问题说成类型问题。"""

    def test_wakes_before_fortifying(self):
        from civ_mcp.lua import units as lua_units

        code = lua_units.build_fortify_unit(3)
        assert "UnitCommandTypes.WAKE" in code
        # 唤醒必须发生在 SLEEP 兜底之前,否则等于没修
        wake = code.index("UnitCommandTypes.WAKE")
        sleep_fallback = code.index('GameInfo.UnitOperations["UNITOPERATION_SLEEP"]')
        assert wake < sleep_fallback

    def test_no_recheck_between_wake_and_fortify(self):
        """WAKE 下一帧才结算 —— 同帧复查 CanStartOperation 拿到的是旧答案,
        复查等于把这条修复变回原样。"""
        from civ_mcp.lua import units as lua_units

        code = lua_units.build_fortify_unit(3)
        wake = code.index("RequestCommand(unit, UnitCommandTypes.WAKE)")
        fortify = code.index(
            "RequestOperation(unit, UnitOperationTypes.FORTIFY)", wake
        )
        # 注释里提到 CanStartOperation 不算复查(这一批踩过三次)
        assert "CanStartOperation" not in strip_lua_comments(code[wake:fortify])

    def test_sleep_message_does_not_blame_the_unit_type(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from civ_mcp.game_state import GameState

        gs = GameState.__new__(GameState)
        gs.conn = MagicMock()
        gs.conn.execute_write = AsyncMock(return_value=["OK:SLEEPING", "---END---"])

        msg = asyncio.run(gs.fortify_unit(3))
        assert "cannot fortify" not in msg
        assert "这种单位" not in msg
        assert "+6" in msg, "必须说明睡眠丢掉了防御加成"
        assert "alert" in msg, "必须给出前线单位的替代做法"


class TestGpClassComesFromTheGreatPersonObject:
    """(87) 伟人职业读错了表:GameInfo.Units 没有 GreatPersonClass 列。

    修掉 (80) 的 NameError 之后,get_gp_advisor 仍然对每个伟人都回
    "Could not get GP advisor info. Is this a Great Person unit?" ——
    实机 run_lua 验证:uInfo.GreatPersonClass 恒为 nil,而守卫写的是
    `== ""`,nil ~= "" 拦不住,于是一路走到 classToDistrict[nil] 才在
    UNKNOWN_GP_CLASS 处 bail,报错指向"不认识的职业",真因是取错了列。
    权威来源是伟人对象:GetClass() → GameInfo.GreatPersonClasses[idx]。
    """

    def test_advisor_query_uses_great_person_object(self):
        from civ_mcp.lua import great_people as lua_gp

        code = strip_lua_comments(lua_gp.build_gp_advisor_query(3))
        assert "GameInfo.GreatPersonClasses[gpObj:GetClass()]" in code
        assert "uInfo.GreatPersonClass" not in code

    def test_nil_guard_covers_nil_not_just_empty_string(self):
        from civ_mcp.lua import great_people as lua_gp

        code = lua_gp.build_gp_advisor_query(3)
        assert "gpClass == nil or gpClass ==" in code, (
            "只判 == \"\" 挡不住 nil —— 这正是这条缺陷藏了四局的原因"
        )

    def test_activation_failure_hint_also_fixed(self):
        """大作家/艺术家/音乐家那条'必须站在有空作品槽的城市中心'提示,
        此前因为同一个 nil 而从来没出现过。"""
        from civ_mcp.lua import great_people as lua_gp

        code = strip_lua_comments(lua_gp.build_activate_great_person(3))
        assert "uInfo.GreatPersonClass" not in code
        assert "GameInfo.GreatPersonClasses[gp:GetClass()]" in code
        assert "GREAT_PERSON_CLASS_MUSICIAN" in code


class TestGateDoesNotLieWhenTurnAlreadyAdvanced:
    """(88) 门禁在回合已经推进之后仍报「不能结束回合」。

    实机 T237:门禁报 T237 拦截,但此刻回合已到 T238 —— 喀山城血从 113 回到
    133(一回合 +20 回血)、所有单位移动力刷新、下一次 end_turn 报 238→239。
    门禁自身是前置的,推进来自中途外交会话(俄罗斯丢城后连送两个和平提案)
    让原始 ACTION_ENDTURN 留在途中继续结算。危害:调用方按旧局面下令,
    且重调时复用的反思落到下一个回合号上,日记整体错位一格。
    """

    def test_gate_rechecks_turn_before_reporting_block(self):
        import inspect

        from civ_mcp import end_turn as et

        src = inspect.getsource(et.execute_end_turn)
        idx = src.index("if gate_msg:")
        window = src[idx : idx + 1800]
        assert "_get_turn_number" in window, "返回拦截消息前必须复查回合号"
        assert "advanced = True" in window
        # 复查必须发生在 return gate_msg 之前,否则等于没查
        assert window.index("_get_turn_number") < window.index("return gate_msg")


class TestOnlyEndTurnAdvancesTheTurn:
    """(83) 回合推进必须只有 end_turn 一条路径。"""

    def test_disable_query_targets_auto_end_turn(self):
        from civ_mcp.lua import units as lua_units

        code = lua_units.build_disable_auto_end_turn()
        assert 'UserConfiguration.SetValue("AutoEndTurn", 0)' in code
        assert "SaveCheckpoint" not in code, "只改运行时值,不许写用户磁盘配置"

    def test_exported_from_lua_package(self):
        from civ_mcp import lua as lq

        assert callable(lq.build_disable_auto_end_turn)

    def test_skip_paths_disable_it_first(self):
        import inspect

        from civ_mcp.game_state import GameState

        for name in ("skip_unit", "skip_remaining_units"):
            src = inspect.getsource(getattr(GameState, name))
            assert "ensure_manual_turn_control" in src, f"{name} 没有关掉自动结束回合"
            assert src.index("ensure_manual_turn_control") < src.index(
                "lq.build_skip"
            ), f"{name} 必须在 FinishMoves 之前关"

    def test_guard_is_idempotent(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from civ_mcp.game_state import GameState

        gs = GameState.__new__(GameState)
        gs.conn = MagicMock()
        gs.conn.execute_write = AsyncMock(return_value=["OK:AUTO_END_TURN_DISABLED"])

        async def go():
            first = await gs.ensure_manual_turn_control()
            second = await gs.ensure_manual_turn_control()
            return first, second

        first, second = asyncio.run(go())
        assert first is True and second is False
        assert gs.conn.execute_write.await_count == 1, "每进程只该发一次,别每回合浪费往返"

    def test_failure_is_logged_not_swallowed(self):
        import inspect

        from civ_mcp.game_state import GameState

        src = inspect.getsource(GameState.ensure_manual_turn_control)
        assert "log.warning" in src, "关不掉必须留痕 —— 这正是上一批 (81) 的教训"

    def test_end_turn_reports_turn_drift(self):
        from pathlib import Path

        from civ_mcp import server

        src = Path(server.__file__).read_text(encoding="utf-8")
        assert "_turn_drift" in src
        # 漂移警告必须进返回值,只写日志等于没说
        idx = src.index("回合漂移:上一条日记记在")
        tail = src[idx : idx + 900]
        assert "result" in tail, "漂移必须拼进 end_turn 的返回文本"


class TestAttackFollowupPollsForRealHp:
    """(84) 战后血量要轮询到真值,读不到就明说读不到。"""

    def test_followup_polls_instead_of_single_sleep(self):
        import inspect

        from civ_mcp import game_state

        src = inspect.getsource(game_state.GameState.attack_unit)
        assert "ATTACK_FOLLOWUP_TRIES" in src
        assert "for _ in range(ATTACK_FOLLOWUP_TRIES)" in src
        assert "await asyncio.sleep(0.3 if not is_melee else 0.5)" not in src

    def test_poll_budget_is_bounded(self):
        from civ_mcp import game_state

        assert 2 <= game_state.ATTACK_FOLLOWUP_TRIES <= 8
        total = game_state.ATTACK_FOLLOWUP_TRIES * game_state.ATTACK_FOLLOWUP_DELAY
        assert total <= 3.0, "一回合十几次攻击,总等待不能失控"

    def test_poll_stops_once_damage_shows(self):
        """读到掉血或目标消失就该停 —— 否则每次攻击都白等满 2 秒。"""
        import inspect

        from civ_mcp import game_state

        src = inspect.getsource(game_state.GameState.attack_unit)
        loop = src[src.index("for _ in range(ATTACK_FOLLOWUP_TRIES)") :]
        assert "break" in loop
        assert "probe < pre_hp" in loop

    def test_unresolved_hp_is_not_rendered_as_untouched(self):
        import inspect

        from civ_mcp import game_state

        src = inspect.getsource(game_state.GameState.attack_unit)
        assert "?未结算" in src, (
            "轮询不出真值时,箭头右侧照抄战前值会读成「这一击没打动」"
        )


class TestCityAttackNoProgressIsNotAFalseFailure:
    """(86) 攻城战报的 NO_PROGRESS 曾经建立在同帧陈旧城血上,必然误报。

    实机 T232:火枪手两击各打进 54 点(城血 175→127→73,城墙 25→16→9),
    战报两次都写 `city HP:175 -> 175/200 NO_PROGRESS(...别再拿人填)`。
    这条判定本是伦敦那次为防假成功加的,自己却成了假失败的源头 ——
    而假失败会直接劝停一场正在赢的攻城。
    """

    @staticmethod
    def _gs(defense_rows):
        from unittest.mock import AsyncMock, MagicMock

        from civ_mcp.game_state import GameState

        gs = GameState.__new__(GameState)
        gs.conn = MagicMock()
        gs.conn.execute_write = AsyncMock(side_effect=defense_rows)
        return gs

    def test_stale_no_progress_is_replaced_by_real_damage(self):
        import asyncio

        rows = [["CITY_DEFENSE|沃罗涅什|5|3|35|127|200|16|100|0", "---END---"]]
        gs = self._gs(rows)
        result = (
            "MELEE_ATTACK|target:CITY 沃罗涅什 at (81,54)"
            "|city HP:175 -> 175/200"
            "|NO_PROGRESS(城血未下降 —— 你的战力打不穿这座城的城防,"
            "换攻城单位/升级兵种,别再拿人填)"
            "|your HP:100 -> 100 CS:55"
        )
        out = asyncio.run(gs._refresh_city_attack_result(result, 81, 54))
        assert "NO_PROGRESS" not in out
        assert "别再拿人填" not in out
        assert "city HP:175 -> 127/200" in out
        assert "城血 -48" in out

    def test_walls_absorbing_damage_is_not_called_a_failure(self):
        """有墙时城血不动是正常的 —— 伤害先扣墙,不是打不动。"""
        import asyncio

        rows = [["CITY_DEFENSE|城|5|3|35|200|200|100|100|0", "---END---"]]
        gs = self._gs(rows * 5)
        result = (
            "MELEE_ATTACK|target:CITY 城 at (81,54)|city HP:200 -> 200/200"
            "|NO_PROGRESS(城血未下降 —— 你的战力打不穿这座城的城防,别再拿人填)"
        )
        out = asyncio.run(gs._refresh_city_attack_result(result, 81, 54))
        assert "别再拿人填" not in out
        assert "伤害先扣城墙" in out

    def test_capture_is_recognised_not_called_no_progress(self):
        """T233 实机最极端的一次:**占领沃罗涅什的那一击**被报成
        "city HP:27 -> 27/200 NO_PROGRESS(...别再拿人填)" —— 城已经易主,
        工具却在劝你别打了。城主变自己必须先于任何血量比较。"""
        import asyncio

        gs = self._gs([["CITY_DEFENSE|沃罗涅什|1|2|0|100|200|0|100|0", "---END---"]])
        gs._local_player_id = 1
        result = (
            "MELEE_ATTACK|target:CITY 沃罗涅什 at (81,54)|city HP:27 -> 27/200"
            "|NO_PROGRESS(城血未下降 —— 你的战力打不穿这座城的城防,别再拿人填)"
        )
        out = asyncio.run(gs._refresh_city_attack_result(result, 81, 54))
        assert "CITY_CAPTURED" in out
        assert "NO_PROGRESS" not in out
        assert "别再拿人填" not in out

    def test_attacker_standing_on_city_tile_means_captured(self):
        """(89) T260 攻克斯摩棱斯克那一击又报了 NO_PROGRESS —— (86) 加的城主
        检查没救住,因为易主结算比 2 秒轮询还慢,窗口内 owner 和城血都还是旧值。
        更早、更硬的判据是攻击者自己站到了城格上:近战占领在 Civ6 里就是移动
        进城,单位位置一变就是既成事实,不依赖城市对象何时刷新所有权。"""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from civ_mcp.game_state import GameState

        gs = GameState.__new__(GameState)
        gs.conn = MagicMock()
        # 位置查询(GameCore)说单位已在城格上
        gs.conn.execute_read = AsyncMock(return_value=["POS|70|58", "---END---"])
        # 城市查询仍返回旧值(owner 未刷新、城血未降)——正是实机的情形
        gs.conn.execute_write = AsyncMock(
            return_value=["CITY_DEFENSE|斯摩棱斯克|5|5|39|59|200|0|200|0", "---END---"]
        )
        result = (
            "MELEE_ATTACK|target:CITY 斯摩棱斯克 at (70,58)|city HP:59 -> 59/200"
            "|NO_PROGRESS(城血未下降 —— 你的战力打不穿这座城的城防,别再拿人填)"
        )
        out = asyncio.run(gs._refresh_city_attack_result(result, 70, 58, 3))
        assert "CITY_CAPTURED" in out
        assert "NO_PROGRESS" not in out
        assert "别再拿人填" not in out

    def test_position_check_runs_before_hp_comparison(self):
        import inspect

        from civ_mcp import game_state

        src = inspect.getsource(game_state.GameState._refresh_city_attack_result)
        assert src.index("build_unit_position_query") < src.index(
            "build_enemy_city_defense_query"
        ), "位置判据必须先于城市血量/城主查询 —— 它结算得更早"

    def test_attack_passes_unit_index_through(self):
        import inspect

        from civ_mcp import game_state

        src = inspect.getsource(game_state.GameState.attack_unit)
        assert "_refresh_city_attack_result(\n                    result, target_x, target_y, unit_index\n                )" in src or "target_y, unit_index" in src

    def test_wall_only_progress_still_counts(self):
        import asyncio

        rows = [["CITY_DEFENSE|城|5|3|35|200|200|82|100|0", "---END---"]]
        gs = self._gs(rows)
        result = (
            "RANGE_ATTACK|target:CITY 城 at (81,54)|city HP:200 -> 200/200"
            "|city walls: 100/100"
        )
        out = asyncio.run(gs._refresh_city_attack_result(result, 81, 54))
        assert "城墙 -18" in out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
