# 安装与排障

## 游戏设置

Windows 在游戏选项中启用 Tuner，关闭自动结束回合，重启并载入单人存档。根据上游的 Windows 安装说明，需要通过 Steam「工具」安装 Sid Meier's Civilization VI SDK。关闭 SDK 的 FireTuner GUI，再让 MCP 连接 TCP 4318。

macOS/Linux 的历史配置路径见 [上游 README](upstream/README.md)。本衍生版本本轮没有逐个平台实机验证，也不会自动修改游戏配置。

## 安装与离线检查

使用 Python 3.12 或更新版本创建虚拟环境，安装本仓库：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/doctor.py
```

若提示 `No module named mcp`，确认运行的是安装依赖时使用的同一个 Python。若规则文件缺失，重新解压完整源码；wheel 发行版应包含 `civ_mcp/resources/gamedata/`。

## 连接方式

推荐 `python tools/serve_http.py --port 8123`，客户端连接 `http://127.0.0.1:8123/mcp`。该服务默认只监听本机，未提供远程认证。`--core` 只暴露核心工具，供工具数量受限的客户端使用。

stdio 配置中的解释器必须来自当前项目环境，并已安装此包；运行参数为 `-m civ_mcp`。不要同时用 HTTP 服务、stdio 服务器、SDK FireTuner GUI 和队列 driver 争用同一游戏连接。

## 规则数据

随包 `docs/gamedata/` 是原版 `RULESET_STANDARD` 的快照，不代表所有资料片和 MOD。游戏加载后可在独占连接空闲时运行：

```powershell
.\.venv\Scripts\python.exe scripts/export_gamedata.py
```

脚本连接游戏并写入源码目录 `docs/gamedata/`。先停止其他连接拥有者，并备份需要保留的旧规则快照。完成后重启 MCP；wheel 用户需从对应源码重建并安装，才能把新数据写入发行包。

## 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| `TUNER_BUSY` 或握手失败 | 确认游戏已载入；只保留一个连接拥有者；不要无条件杀进程 |
| 工具数量过多，客户端加载失败 | 停止服务后用 `--core` 重启，重新挂载客户端 |
| 中文输出乱码 | 使用 `python -m civ_mcp`，并设置 `PYTHONIOENCODING=utf-8` |
| 安装成功但策略数据为空 | 运行 doctor；确认规则文件随包安装且规则集匹配 |
| 断线后不知道动作是否成功 | 先重新读概览、单位、城市，再决定是否重试，避免重复购买/移动 |

游戏恢复、菜单 OCR 和自动启动依赖实际平台与游戏状态，不能通过离线测试证明可用。单元测试不需要启动游戏；手动 `scripts/test_connection.py` 会尝试连接。
