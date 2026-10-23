# 来源与衍生版本范围

## 基础项目

原项目：[lmwilki/civ6-mcp](https://github.com/lmwilki/civ6-mcp)，原作者 Liam Wilkinson，许可证 MIT。原始 LICENSE 完整保留，原项目的 README、AGENTS 与历史 changelog 保存在 `docs/upstream/`。

本地仓库记录的上游基点为 `dd2019056371b92ea4854e879ddf05a8cad95e8a`，版本为 1.1.11；导出时本地 HEAD 为 `321b2ba9753af98bf67084945dd29d9bcd9956c7`。**导出包含该 HEAD 之后尚未提交的当前文件内容**，因此 HEAD 哈希并不独自标识整个导出快照。发行文件清单和 SHA-256 见交付包外部的发布记录。

## 保留的本地工作

| 方向 | 可定位的实现 |
| --- | --- |
| 中文规则兼容与反馈 | `src/civ_mcp/lua/`、`knowledge.py`、`game_state.py` |
| 连接与多客户端 | `connection.py`、`tuner_lock.py`、`http_lifespan.py`、`tools/serve_http.py` |
| 回合与战斗纪律 | `end_turn.py`、`end_turn_guard.py`、`discipline.py`、`safety.py` |
| 战略与跨回合状态 | `world_model.py`、`learning_store.py`、`playbook.py`、`power_balance.py`、`score_gap.py` |
| 工具与回归覆盖 | `tools/civ.py`、`tools/driver.py`、`tests/` |

以上描述实现位置，不声称每个模块都从零原创。部分模块在本地 HEAD 中已有，部分为未提交改动；准确归属应使用上游基点和实际代码差异判断。原项目的 Star、下载量、用户与历史提交不计入本衍生版本的采用数据。

对导出前的 184 个源码/测试文件按上游基点比较（忽略换行风格差异）：137 个文件在上游基点不存在，33 个内容发生变化，14 个相同。这个文件级统计反映本地开发规模，不代表新增文件的全部内容均为原创，也不代表已公开提交。

## 这次发布整理

本轮新增资源寻址与 wheel 打包、队列状态目录、安装自检、双语文档和 CI；修复独立测试夹具受到新增扩张规则影响的问题，并保留规则本身的回归测试。

这是经筛选的源码快照，从新 Git 历史开始；未复制旧 `.git`、虚拟环境、个人宿主配置、对局存档、实际对局日志或交接聊天。未纳入上游的 web、benchmark/eval 发布流水线和云端数据发布工具，以保持本发行版聚焦于 MCP 服务和本地使用。源码模块中保留的云遥测接口只有配置相关环境变量时才启用。

## 当前身份与发布状态

项目名为 `civ6-mcp-cn`，公开仓库为 [2962348318-del/civ6-mcp-cn](https://github.com/2962348318-del/civ6-mcp-cn)，维护账号为 `2962348318-del`。PyPI 包名尚未验证或注册，当前安装方式是仓库安装或本地 wheel。
