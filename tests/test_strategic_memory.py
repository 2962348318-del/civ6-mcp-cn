"""strategic_memory 按局分键 —— 跨局串味修复(2026-08-09)的回归测试。"""

from __future__ import annotations

from civ_mcp import strategic_memory as sm


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(sm, "MEMORY_FILE", tmp_path / "strategic_summary.json")


def test_summaries_are_isolated_per_game(monkeypatch, tmp_path):
    """A 局写的摘要 B 局读不到 —— 这就是之前串味的那条路。"""
    _isolate(monkeypatch, tmp_path)
    sm.write_summary(300, "A 局:准备发射火星舱", game_id="rome_1")
    out = sm.read_summary(game_id="greece_2")
    assert "火星舱" not in out
    assert "No strategic summary exists yet" in out


def test_same_game_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sm.write_summary(42, "扩张期,4 城", goals="T60 前 5 城", game_id="rome_1")
    out = sm.read_summary(game_id="rome_1")
    assert "扩张期,4 城" in out and "T60 前 5 城" in out
    assert "Turn 42" in out


def test_no_game_id_falls_back_to_legacy_file(monkeypatch, tmp_path):
    """拿不到身份时落全局文件 —— 比丢内容强,但只在无 game_id 时。"""
    _isolate(monkeypatch, tmp_path)
    sm.write_summary(10, "无身份写入")
    assert "无身份写入" in sm.read_summary()
    assert (tmp_path / "strategic_summary.json").exists()


def test_keyed_read_warns_about_legacy_file(monkeypatch, tmp_path):
    """分局读空但存在旧全局文件时,提示它可能属于别局 —— 不静默回退读。"""
    _isolate(monkeypatch, tmp_path)
    sm.write_summary(200, "上一局的旧摘要")  # 旧版行为留下的全局文件
    out = sm.read_summary(game_id="rome_1")
    assert "上一局的旧摘要" not in out  # 绝不把别局内容当本局
    assert "未分局的旧摘要" in out


def test_game_id_sanitized_for_filename(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sm.write_summary(5, "内容", game_id="rome/..\\evil_1")
    files = list(tmp_path.glob("strategic_summary_*.json"))
    assert len(files) == 1
    assert ".." not in files[0].name and "/" not in files[0].name
