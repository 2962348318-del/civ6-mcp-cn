"""缺陷 111:整局都在按「神级」推理,而实际难度是军阀。

2026-08-12 罗马局 T358,用户一句"你忘记了吗这个游戏难度不是神级"。实测:

    PlayerConfigurations[me]:GetHandicapTypeID() = -966254459
    GameInfo.Difficulties["DIFFICULTY_WARLORD"].Hash = -966254459   ← 匹配
    GameInfo.Difficulties["DIFFICULTY_DEITY"].Hash  = 1543611863

八档里的第三档。AI 在王子以下拿【减益】,君主以上才拿加成 —— 这条界线
决定了该保守还是该主动兑现优势,而我整局都站在错误的一侧推理。

两处合起来造成的:

  ① doctrine.agenda 的 consequence 写死了"神级下 AI 的产出优势你追不上"。
     它每回合出现在简报里,而这句话在军阀难度下是反的。我据此把统治路线
     的可行性压低了一整档 —— 理由是"神级 AI 会有反应",一个凭空的前提。

  ② 简报头部只印【实测档位】。那是"我 vs 对手"的实测比值(统治级),
     和难度设置是两个概念,挨在一起却只显示一个。真正的难度就在
     get_game_overview 的第一行:

         Turn 355 | 罗马 (图拉真) | Score: 651 | 军阀
                                                  ^^^^
     每回合都在眼前,从没进过任何判断链,也从没被读过。

修法不是"按难度调策略"—— power_balance 的模块头注早就写明难度加成表
在导出数据里是空的,判定必须看实测比值。修的是【让难度可见】,并且不再
用一个写死的难度前提去论证与难度无关的事(议程是规则驱动,任何难度都成立)。
"""

from __future__ import annotations

import inspect

import pytest

from civ_mcp import doctrine, power_balance


class TestDifficultyIsVisible:
    def test_headline_shows_difficulty(self):
        snap = power_balance.PowerSnapshot(
            turn=358,
            me=power_balance.Side(name="罗马"),
            difficulty="DIFFICULTY_WARLORD",
        )
        out = power_balance.headline(snap)
        assert "军阀" in out
        assert "3/8" in out, "光写档名看不出它在八档里靠哪头"

    def test_localised_name_passes_through(self):
        """实机 get_game_overview 直接给「军阀」,不是 DIFFICULTY_* 常量。"""
        assert power_balance.difficulty_label("军阀") == "军阀"

    def test_deity_is_labelled_as_the_top(self):
        assert "8/8" in power_balance.difficulty_label("DIFFICULTY_DEITY")

    def test_missing_difficulty_is_silent(self):
        """采集不到时不能瞎猜,也不能印一个空括号。"""
        assert power_balance.difficulty_label("") == ""
        snap = power_balance.PowerSnapshot(turn=358, me=power_balance.Side(name="罗马"))
        assert "难度" not in power_balance.headline(snap)

    def test_measure_accepts_and_stores_it(self):
        snap = power_balance.measure(
            358,
            power_balance.Side(name="罗马"),
            [],
            difficulty="DIFFICULTY_WARLORD",
        )
        assert snap.difficulty == "DIFFICULTY_WARLORD"

    def test_end_turn_passes_the_world_model_value(self):
        from civ_mcp import end_turn as et

        src = inspect.getsource(et)
        i = src.index("power_balance.measure(")
        seg = src[i : i + 400]
        assert "difficulty=" in seg, "采集到了却不传下去,等于没采集"
        assert 'getattr(wm, "difficulty"' in seg

    def test_difficulty_does_not_drive_the_tier(self):
        """难度只用于显示。档位判定必须继续看实测比值 ——
        难度加成表在导出数据里是空的,任何按难度名调策略的分支都是猜的。"""
        me = power_balance.Side(name="罗马", science=358, techs=62, military=1485)
        rival = power_balance.Side(name="刚果", science=212, techs=49, military=487)
        easy = power_balance.measure(358, me, [rival], difficulty="DIFFICULTY_SETTLER")
        hard = power_balance.measure(358, me, [rival], difficulty="DIFFICULTY_DEITY")
        assert easy.tier is hard.tier


class TestAgendaAdviceDropsTheDeityPremise:
    def test_no_hardcoded_deity_claim(self):
        src = inspect.getsource(doctrine)
        i = src.index("AI 的开战判定是**规则驱动**的")
        seg = src[i : i + 200]
        assert "神级" not in seg, (
            "议程是规则驱动这一点与难度无关 —— 不要用写死的难度前提去论证它"
        )

    def test_the_actual_point_survives(self):
        """修的是那个前提,不是这条建议本身。"""
        src = inspect.getsource(doctrine)
        assert "满足议程比造兵便宜一个数量级" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
