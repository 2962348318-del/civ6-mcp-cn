# Civ6 MCP CN

**让 AI 通过 MCP 读取《文明 VI》局面、执行操作，并持续检查回合决策有没有遗漏。**

[English](README.en.md) · [安装与排障](docs/getting-started.md) · [上游与改动来源](docs/provenance.md) · [参与贡献](CONTRIBUTING.md)

本项目是 [Liam Wilkinson / civ6-mcp](https://github.com/lmwilki/civ6-mcp) 的独立维护衍生版本，保留原作者 MIT 许可证。这个版本重点整理中文叙事、Windows 连接稳定性、原版规则集兼容与长期决策辅助；MCP 接口、FireTuner 协议实现等基础能力来自上游。

当前为 **0.1.0 开源准备版**。离线测试结果见 [验证记录](docs/validation.md)。本轮没有连接真实游戏验证，不能把单元测试通过解释成已实证通关或稳定支持全部规则集。

## 能做什么

| 能力 | 说明 |
| --- | --- |
| 读写游戏状态 | 查询单位、城市、地图、科技、外交，执行移动、生产、研究和回合推进 |
| 决策纪律 | 回合完成度门禁、闲置平民提醒、城市扩张检查、战斗风险拦截 |
| 长期状态 | 世界模型、计划、日记、复盘与跨局教训，减少上下文压缩后的遗忘 |
| 战略辅助 | 局势分档、分数差距分析、文明套件、科技/政策/区域建议 |
| 多客户端连接 | 单个本地 HTTP 服务共享 FireTuner 连接；也提供 stdio 与命令行入口 |

游戏操作通过 FireTuner/Lua 接口完成，核心读写不需要视觉模型。部分菜单操作、截图及恢复功能依赖可选平台组件。建议与门禁属于启发式辅助，仍需检查真实游戏反馈。

## 先决条件

- Python **3.12+**，以及能安装 Python 依赖的环境。
- 已安装并自行拥有的《文明 VI》；开启 Tuner，载入单人存档。
- 一个支持 MCP 的客户端。模型账户和 API 由客户端自行配置；本仓库不提供账户或 API 额度。
- Windows 是本地改动的主要使用环境；macOS/Linux 支持继承自上游，衍生版本尚未逐个平台实机复测。

**规则集边界：**随仓库提供的规则快照来自 `RULESET_STANDARD`。风云变幻、迭起兴衰及 MOD 的机制不应直接套用这些静态数值；请根据加载的规则集重新导出数据，参见 [安装与排障](docs/getting-started.md)。

## 1. 安装

从本仓库页面下载 ZIP 并解压，或通过 Git 克隆本仓库。随后在项目根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/doctor.py
```

macOS/Linux 将 Python 路径替换为 `.venv/bin/python`。

已安装 [uv](https://docs.astral.sh/uv/) 时，也可以使用：

```bash
uv sync --frozen
uv run python scripts/doctor.py
```

`doctor.py` 只检查依赖、规则文件和工具注册，**不会连接或启动游戏**。如需 Windows OCR/菜单自动化组件，执行 `python -m pip install -e ".[launcher-windows]"`，其中 `python` 应为当前虚拟环境的解释器。

## 2. 连接游戏

在游戏选项中打开 **Tuner**，关闭自动结束回合，重启游戏并载入存档。Windows 安装要求与上游说明见 [安装与排障](docs/getting-started.md)。Tuner 会禁用成就。

推荐用一个共享服务，避免多个客户端竞争游戏的 `4318` 端口：

```powershell
.\.venv\Scripts\python.exe tools/serve_http.py --port 8123
```

在 MCP 客户端中填入：

```json
{
  "mcpServers": {
    "civ6": {
      "type": "http",
      "url": "http://127.0.0.1:8123/mcp"
    }
  }
}
```

客户端若只支持 stdio，使用 [stdio 示例](examples/mcp-stdio.json)，将解释器、项目路径替换为自己的绝对路径。**共享 HTTP 与独占 stdio 二选一**；关闭 SDK 的 FireTuner GUI，保持一个游戏连接拥有者。

## 3. 开始使用

先让客户端调用 `get_game_overview`，核实规则集、回合和玩家，再调用 `get_units`、`get_cities`。接手已有对局时，先读 `get_diary`。完整回合流程见 [AGENTS.md](AGENTS.md)。

没有游戏也能查看工具列表和参数：

```powershell
.\.venv\Scripts\python.exe tools/civ.py --list
.\.venv\Scripts\python.exe tools/civ.py --schema unit_action
```

连接准备好后，可从命令行读取概览：

```powershell
.\.venv\Scripts\python.exe tools/civ.py get_game_overview
```

这条实际调用会尝试复用本地 HTTP 服务，或启动队列 driver 并连接游戏。与离线自检的行为不同。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

默认只收集 `tests/`；`scripts/test_connection.py` 是需要真实游戏的手动脚本。CI 执行离线测试、导入检查及安装包检查，不声称覆盖完整对局。

```text
src/civ_mcp/     MCP 服务、Lua 查询、连接与策略模块
tests/          离线单元与回归测试
tools/          HTTP 服务与命令行/队列工具
docs/gamedata/  原版规则快照，构建 wheel 时随包分发
data/strategy/  文明策略模板
examples/       可移植配置示例
```

运行状态默认写入用户目录 `.civ6-mcp`；`CIV_MCP_WORKSPACE` 可以指定独立目录。命令行 driver 的临时队列写入项目 `var/`。这些运行文件均不应提交到公共仓库。

## 开源与来源

保留 [LICENSE](LICENSE) 中的原作者版权及 MIT 授权；衍生版本的范围见 [NOTICE](NOTICE) 和 [来源记录](docs/provenance.md)。游戏客户端、个人存档、真实对局日志、本机账号配置与 Git 历史均不在本次源码导出中。随包规则快照是规则数据，不包含游戏安装文件或美术资源。

Civilization VI 及相关名称属于其权利人。本项目是社区工具，与 Firaxis、2K、OpenAI 及上游作者没有官方隶属或背书关系。
