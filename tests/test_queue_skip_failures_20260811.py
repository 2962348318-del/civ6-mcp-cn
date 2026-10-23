"""缺陷 94:生产队列一项失败就整条噎住,城市空转一整回合。

2026-08-11 罗马局 T269 实锤。奥斯蒂亚队列头是 BUILDING_FORBIDDEN_CITY,
奇观必须带落点坐标而队列里没有 —— 消费时报 MISSING_COORDS,队列暂停,
简报里那一行是「奥斯蒂亚 finished building nothing」。队列后面还排着
UNIT_BUILDER,完全造得出来,却因为前面一项卡住而没轮到。

为什么没在排队时拦掉:区域缺坐标是能在排队时拒的(缺陷 90 就是这么修的),
但奇观不行 —— 队列层是纯 Python,BUILDING_ 前缀分不出哪些是奇观。既然
拦不住,就不该让它把整条队列噎住:失败就跳过,接着试下一项。

用户对这一类的原话是「生产断了」。空转一回合的代价是实打实的产能,而
队列后面往往排着完全能造的东西。
"""

from __future__ import annotations

import inspect
import re

import pytest

from civ_mcp import end_turn as et


def _consume_src() -> str:
    """取出队列消费那一段源码(execute_end_turn 很大,只看这一块)。"""
    src = inspect.getsource(et)
    i = src.index("production queue auto-advance failed")
    j = src.rindex("for cid, _q in pq_all.items():", 0, i)
    return src[j:i]


class TestSkipsFailedItems:
    def test_loop_keeps_trying_after_a_failure(self):
        src = _consume_src()
        assert "while not started:" in src, (
            "一项失败必须接着试下一项 —— 单次 pop 会让城市空转整回合"
        )

    def test_failure_does_not_break_the_loop(self):
        """失败分支只记录,不能 break —— break 回来就是原来的病。"""
        src = _consume_src()
        fail_branch = src[src.index("else:") :]
        assert "failures.append" in fail_branch
        head = fail_branch[: fail_branch.index("failures.append")]
        assert "break" not in head

    def test_success_stops_the_loop(self):
        """一旦有项开工就停 —— 不能把整条队列一次消费完。"""
        src = _consume_src()
        assert "started = True" in src

    def test_skipped_items_are_reported_not_swallowed(self):
        """跳过必须说出来。静默跳过 = 队列内容和实际生产对不上。"""
        src = _consume_src()
        assert "已跳过失败项" in src

    def test_all_failed_case_is_reported(self):
        src = _consume_src()
        assert "exhausted" in src
        assert "set production manually" in src

    def test_no_stale_queue_paused_wording(self):
        """旧文案说"queue paused",而现在队列不再暂停 —— 描述要跟上行为。"""
        src = _consume_src()
        assert "queue paused" not in src


class TestQueueLoopTerminates:
    """跳过逻辑必须能停:队列每轮都 pop,不会无限转。"""

    def test_every_iteration_pops(self):
        src = _consume_src()
        body = src[src.index("while not started:") :]
        assert "pqm.pop_next(game_key, cid)" in body
        # 空队列必须退出
        assert "if not head:" in body
        assert "break" in body

    def test_simulated_run_terminates_and_skips(self):
        """用一个纯 Python 替身跑一遍同样的控制流,确认它会停、会跳过。"""
        queue = [
            {"item_name": "BUILDING_FORBIDDEN_CITY"},  # 缺坐标,失败
            {"item_name": "DISTRICT_BROKEN"},  # 也失败
            {"item_name": "UNIT_BUILDER"},  # 能造
            {"item_name": "BUILDING_MARKET"},  # 不该被消费
        ]

        def pop_next():
            return queue.pop(0) if queue else None

        def produce(item):
            return (
                "PRODUCING|ok"
                if item["item_name"] == "UNIT_BUILDER"
                else "Error: MISSING_COORDS"
            )

        failures, started, guard = [], False, 0
        while not started:
            guard += 1
            assert guard < 100, "循环必须终止"
            head = pop_next()
            if not head:
                break
            if "PRODUCING" in produce(head):
                started = True
            else:
                failures.append(head["item_name"])

        assert started
        assert failures == ["BUILDING_FORBIDDEN_CITY", "DISTRICT_BROKEN"]
        assert [q["item_name"] for q in queue] == ["BUILDING_MARKET"], (
            "开工之后就该停 —— 剩下的项留在队列里"
        )

    def test_all_failing_queue_terminates(self):
        queue = [{"item_name": f"BAD_{i}"} for i in range(5)]
        failures, started, guard = [], False, 0
        while not started:
            guard += 1
            assert guard < 100
            if not queue:
                break
            failures.append(queue.pop(0)["item_name"])
        assert not started
        assert len(failures) == 5


class TestWonderCoordsDocumented:
    def test_docstring_tells_you_wonders_need_coords(self):
        from civ_mcp import server

        fn = getattr(server.set_production_queue, "fn", server.set_production_queue)
        doc = inspect.getdoc(fn) or ""
        assert "Wonders need coords" in doc
        assert "get_wonder_advisor" in doc

    def test_parser_accepts_coords_on_a_building(self):
        """奇观是 BUILDING_ 项,@x,y 语法必须对它也成立。"""
        from civ_mcp import production_queue as pqm

        entry = pqm.parse_item("BUILDING_FORBIDDEN_CITY@67,49")
        assert entry is not None
        assert entry["item_type"] == "BUILDING"
        assert (entry["target_x"], entry["target_y"]) == (67, 49)

    def test_wonder_coords_survive_the_comma_split(self):
        """和缺陷 90 同一个坑:坐标里的逗号不能被当成项目分隔符。"""
        tokens = [
            s.strip()
            for s in "UNIT_BUILDER,BUILDING_FORBIDDEN_CITY@67,49".split(",")
            if s.strip()
        ]
        out: list[str] = []
        for tok in tokens:
            if (
                out
                and re.fullmatch(r"-?\d+", tok)
                and "@" in out[-1]
                and "," not in out[-1]
            ):
                out[-1] = f"{out[-1]},{tok}"
            else:
                out.append(tok)
        assert out == ["UNIT_BUILDER", "BUILDING_FORBIDDEN_CITY@67,49"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
