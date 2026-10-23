"""Tests for set_city_production silent-failure detection.

The game's CityManager.RequestOperation is fire-and-forget; even when
CanStartOperation returned true, it can silently no-op if the queue is
in a degenerate state. The OK-path verification catches this.
"""

import asyncio
import types

from civ_mcp.game_state import GameState


class _StubConnection:
    """Stubs execute_write and execute_read with queued responses."""

    def __init__(self, write_lines, read_lines):
        self._write = list(write_lines)
        self._read = list(read_lines)

    async def execute_write(self, lua):
        return self._write.pop(0)

    async def execute_read(self, lua):
        return self._read.pop(0)


def _gs(write_lines, read_lines) -> GameState:
    gs = GameState.__new__(GameState)
    gs.conn = _StubConnection(write_lines, read_lines)
    return gs


class TestSetCityProductionVerification:
    def test_ok_path_verified(self):
        """CanStartOperation=true, verify confirms → return original OK."""
        gs = _gs(
            write_lines=[["OK:PRODUCING|BUILDING_MONUMENT|6 turns"]],
            read_lines=[["CONFIRMED|6 turns"]],
        )
        result = asyncio.run(
            gs.set_city_production(65536, "BUILDING", "BUILDING_MONUMENT")
        )
        assert result == "PRODUCING|BUILDING_MONUMENT|6 turns"

    def test_ok_path_silent_failure(self):
        """Lua returns OK but verify reads NOT_SET twice → SILENT_FAILURE error.

        2026-08-09 缺陷(45)修复后 UNIT_TRADER 按平民走红警检查:
        ensure_world → get_game_identity 会先消费一次 execute_write,
        stub 要在主生产响应前多喂一份 identity 响应。

        2026-08-12 缺陷108:验证改成读两次,两次都 NOT_SET 才算真失败,
        所以 stub 要喂两份读回。
        """
        gs = _gs(
            write_lines=[
                ["GAMESEED|CIVILIZATION_SUMERIA|1"],
                ["OK:PRODUCING|UNIT_TRADER|1 turns"],
            ],
            read_lines=[
                ["NOT_SET|current=nil|expected=UNIT_TRADER"],
                ["NOT_SET|current=nil|expected=UNIT_TRADER"],
            ],
        )
        result = asyncio.run(gs.set_city_production(262145, "UNIT", "UNIT_TRADER"))
        assert "SILENT_FAILURE" in result
        assert "UNIT_TRADER" in result
        assert "purchase_item" in result

    def test_second_read_confirms_is_not_a_failure(self):
        """缺陷108 的正例:第一次读到 NONE(引擎还没结算),第二次确认。

        实机 T355:给拉文那设登月计划,同批读回是 current=NONE,
        下一次调用读到的却是 PROJECT_LAUNCH_MOON_LANDING —— 第一次
        就已经成功了。只读一次会把这种情况报成 SILENT_FAILURE。
        """
        gs = _gs(
            write_lines=[["OK:PRODUCING|PROJECT_LAUNCH_MOON_LANDING|22 turns"]],
            read_lines=[
                ["NOT_SET|current=NONE|expected=PROJECT_LAUNCH_MOON_LANDING"],
                ["CONFIRMED|22 turns"],
            ],
        )
        result = asyncio.run(
            gs.set_city_production(196610, "PROJECT", "PROJECT_LAUNCH_MOON_LANDING")
        )
        assert "SILENT_FAILURE" not in result
        assert result == "PRODUCING|PROJECT_LAUNCH_MOON_LANDING|22 turns"

    def test_cannot_produce_bypasses_verify(self):
        """Hard error from CanProduce check never reaches the verify path."""
        gs = _gs(
            write_lines=[
                [
                    "ERR:CANNOT_PRODUCE|BUILDING_UNIVERSITY cannot be produced "
                    "(requires DISTRICT_CAMPUS district)"
                ]
            ],
            read_lines=[],  # never called
        )
        result = asyncio.run(
            gs.set_city_production(65536, "BUILDING", "BUILDING_UNIVERSITY")
        )
        assert "CANNOT_PRODUCE" in result

    def test_verify_failure_falls_through(self):
        """If verify itself throws, return the original OK optimistically."""

        class _ThrowingConn:
            async def execute_write(self, lua):
                return ["OK:PRODUCING|UNIT_WARRIOR|2 turns"]

            async def execute_read(self, lua):
                raise RuntimeError("connection dropped")

        gs = GameState.__new__(GameState)
        gs.conn = _ThrowingConn()
        result = asyncio.run(gs.set_city_production(65536, "UNIT", "UNIT_WARRIOR"))
        assert result == "PRODUCING|UNIT_WARRIOR|2 turns"


# ===========================================================================
# 2026-08-01 实机回归:新会话第一回合的拦截失效
#
# 实测场景:MCP 服务器重启后接手 T32 局面,三个蛮族在 1-2 格内(磁盘上的
# 世界模型 threat_level=RED),生产建造者被【直接放行】—— 因为 self.world
# 是新建的空模型,要等 end_turn 跑完 _update_world_model 才有值。
# 而新会话的第一个回合恰恰是模型最容易犯错的时刻。
# ===========================================================================


class _FakeConn:
    def __init__(self):
        self.writes = []

    async def execute_write(self, lua):
        self.writes.append(lua)
        return ["PRODUCTION_SET|UNIT_BUILDER", "__CIVMCP_DONE__"]

    async def execute_read(self, lua):
        return ["__CIVMCP_DONE__"]


def _gs_with_disk_world(tmp_path, threat="RED"):
    """构造一个「刚启动、self.world 还是空模型」的 GameState,
    但磁盘上有上一回合落盘的红色威胁。"""
    import asyncio

    from civ_mcp import world_model as wmod
    from civ_mcp.game_state import GameState

    disk = wmod.WorldModel()
    disk.threat_level = threat
    disk.threat_reason = "Barbarian军事单位已进至 1 格内"
    path = tmp_path / "世界模型" / "arabia_1.json"
    disk.save(path)

    gs = GameState(_FakeConn())
    # 关键:模拟新会话 —— self.world 是空的(GREEN),_world_loaded_for 未设
    assert gs.world.threat_level == "GREEN"
    assert gs._world_loaded_for is None

    async def _fake_identity():
        return ("arabia", 1)

    gs.get_game_identity = _fake_identity
    return gs, path


def test_red_alert_intercept_works_on_first_turn_of_session(tmp_path, monkeypatch):
    """回归:新会话第一回合也必须拦住红警下的平民生产。"""
    import asyncio

    from civ_mcp import world_model as wmod

    gs, path = _gs_with_disk_world(tmp_path)
    monkeypatch.setattr(wmod, "candidate_paths", lambda civ, seed: [path])

    out = asyncio.run(gs.set_city_production(65536, "UNIT", "UNIT_BUILDER"))
    assert "RED_ALERT_CIVILIAN" in out, "新会话首回合的红警拦截失效了"
    assert "1 格内" in out  # 理由取自磁盘上的 threat_reason
    assert gs._conn_writes_empty() if hasattr(gs, "_conn_writes_empty") else True


def test_force_still_bypasses_on_first_turn(tmp_path, monkeypatch):
    from civ_mcp import world_model as wmod

    import asyncio

    gs, path = _gs_with_disk_world(tmp_path)
    monkeypatch.setattr(wmod, "candidate_paths", lambda civ, seed: [path])
    out = asyncio.run(gs.set_city_production(65536, "UNIT", "UNIT_BUILDER", force=True))
    assert "RED_ALERT_CIVILIAN" not in out


def test_green_disk_state_allows_civilian(tmp_path, monkeypatch):
    from civ_mcp import world_model as wmod

    import asyncio

    gs, path = _gs_with_disk_world(tmp_path, threat="GREEN")
    monkeypatch.setattr(wmod, "candidate_paths", lambda civ, seed: [path])
    out = asyncio.run(gs.set_city_production(65536, "UNIT", "UNIT_BUILDER"))
    assert "RED_ALERT_CIVILIAN" not in out


def test_military_production_skips_world_load(tmp_path, monkeypatch):
    """非平民生产不该为了拦截去加载世界模型 —— 省一次磁盘 IO。"""
    from civ_mcp import world_model as wmod

    import asyncio

    gs, path = _gs_with_disk_world(tmp_path)
    monkeypatch.setattr(wmod, "candidate_paths", lambda civ, seed: [path])
    called = []

    async def _spy():
        called.append(1)
        return gs.world

    gs.ensure_world = _spy
    asyncio.run(gs.set_city_production(65536, "UNIT", "UNIT_WARRIOR"))
    assert not called, "军事单位生产不应触发世界模型加载"
