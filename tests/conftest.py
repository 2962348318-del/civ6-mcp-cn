"""pytest 全局夹具。

2026-08-05:`GameState.has_read()` 优先读磁盘上的 data/turn_reads.json
(跨进程持久化 —— mcp_call 每次调用都是新进程,内存 set 必丢)。
副作用是**单元测试会读到实机对局的真实状态**:DeepSeek 打局时写进
["units"],于是 test_skipping_get_units_blocks_the_turn 里一个全新的
空 GameState 也报告"读过 units",测试假失败。

把状态文件重定向到临时目录,测试之间以及测试与实机之间彻底隔离。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_turn_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试用独立的 turn_reads 文件,绝不碰真实对局状态。"""
    from civ_mcp import game_state

    monkeypatch.setattr(
        game_state, "_TURN_READS_PATH", tmp_path / "turn_reads.json", raising=False
    )
    yield


@pytest.fixture(autouse=True)
def _clear_gate_state():
    """清空门禁的 id(gs) 兜底表。

    正常路径的状态已改为挂在实例上(见 end_turn._gate_state),但兜底表
    仍按 id 索引 —— 地址复用会让测试互相串台(全量跑挂、单独跑过)。
    """
    from civ_mcp import end_turn

    end_turn._GATE_STATE.clear()
    yield
    end_turn._GATE_STATE.clear()
