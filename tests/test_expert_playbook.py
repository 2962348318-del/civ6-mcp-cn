"""高玩知识库 —— 数据自检与查询接口测试。"""

from __future__ import annotations

from civ_mcp import expert_playbook as pb


def test_self_check_clean():
    """id 唯一、置信度/来源/主题词汇合法、回合窗口有序 —— 零问题才能上线。"""
    assert pb.self_check() == []


def test_every_topic_has_entries():
    covered = {e.topic for e in pb.ENTRIES}
    assert covered == set(pb.TOPICS)


def test_directory_when_no_args():
    text = pb.get_playbook()
    assert "目录" in text
    for t in pb.TOPICS:
        assert t in text
    # 目录不倒全文 —— 每条 rule 的正文不该出现
    assert "复利" not in text


def test_topic_filter_returns_full_rules():
    text = pb.get_playbook(topic="蛮族")
    assert "引信" in text and "拆营" in text
    assert "尤里卡" not in text  # 别的主题不混进来


def test_turn_filter_respects_phase_window():
    early = pb.get_playbook(topic="开局", turn=2)
    assert "原地建城" in early
    late = pb.get_playbook(topic="开局", turn=200)
    assert "没有匹配条目" in late


def test_unknown_topic_lists_valid_ones():
    assert "主题可选" in pb.get_playbook(topic="不存在的主题")


def test_entries_carry_confidence_and_source():
    text = pb.get_playbook(topic="难度")
    assert "社区共识" in text  # 难度表数值必须标明来源类型
    assert "推荐" in text or "确定" in text


def test_phase_headline_covers_all_turns():
    """1-500 每个回合都要有阶段提醒 —— 例行审视不能在某段回合突然沉默。"""
    for turn in range(1, 501):
        assert pb.phase_headline(turn), f"T{turn} 无阶段提醒"


def test_phase_headline_points_to_tool():
    assert "get_expert_playbook" in pb.phase_headline(30)
