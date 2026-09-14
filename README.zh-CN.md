# agent-usage-dashboard（原 kimi-usage-dashboard）

**[English](README.md)** | 简体中文

本机**全部 coding agent** 的 token 用量仪表盘 —— 读取机器上每个 agent 的本地会话日志 / 数据库，聚合并可视化 token 消耗、成本与活跃度。

所有数据都留在本机 —— 仅有的网络调用是可选的、只读拉取：你自己的 Cursor 用量导出与公开的 models.dev 价目表（见下）。不上传任何东西。

## 支持的 Agent

| Agent | 数据来源 | Token | 成本 |
| --- | --- | --- | --- |
| Kimi Code | `~/.kimi-code/sessions`（ccusage） | ✅ | ✅ |
| Codex | `~/.codex/sessions`（ccusage） | ✅ | ✅ |
| Claude Code / Gemini / OpenCode / Kilo / Pi / Amp / Droid / Goose / Qwen / Copilot 等 | ccusage 自动探测 | ✅ | ✅ |
| CommandCode | `~/.commandcode/projects/**.jsonl` | ✅ | ✅ |
| DimCode（dimagent） | `~/.dimcode/v2/dimcode.sqlite` | ✅ | ✅ |
| Devin | `~/.local/share/devin/cli/sessions.db` | ✅ | ≈² |
| KimiX | `~/.kimix/sessions/**/updates.jsonl` | ✅ | ≈² |
| Cursor | cursor.com 用量导出（只读，使用本机登录态）+ `~/.cursor` 本地日志 | ✅¹ | ≈² |
| Antigravity（IDE） | `~/.gemini/antigravity{,-ide}/conversations/*.db`（离线解码 protobuf `gen_metadata`） | ✅ | ≈² |
| Antigravity CLI（`agy`） | `~/.gemini/antigravity-cli/conversations/*.db`（同一套 protobuf 存储） | ✅ | ≈² |

¹ Cursor 不在本地日志记录 token。采集器用 Cursor 应用本地存储的登录态，从 cursor.com **只读拉取**你自己的用量 CSV（设 `USAGE_DASH_CURSOR_API=0` 可禁用，`USAGE_DASH_CURSOR_TIMEOUT` 调整秒数）。登录态失效或断网时自动降级为本地活动量统计。订阅制 "Included" 行没有美元成本，费用列显示 `—`。

² 无记账成本的来源显示 `≈$` **估算成本**，按 [models.dev](https://models.dev) 价目表计算（OpenCode 同款开源数据库，含 input / output / cache_read / cache_write 每百万 token 价格）。采集器每次刷新拉取 `models.dev/api.json` 并缓存到 `.cache/` 24 小时（`USAGE_DASH_PRICES=0` 可禁用，`USAGE_DASH_PRICES_TIMEOUT` 调整秒数）。无公开价格的内部模型（如 `swe-2-*`、`composer-*`）仍显示 `—`。估算值仅供参考，不是实际扣费。

> 本机 `~/.claude/projects` 里全是 `dimcode-mirror` / `commandcode-mirror` 镜像文件 —— 为避免重复计数已跳过，直接读原始数据源。

## 功能

- **筛选栏**：Agent / 模型 / 项目 三个选择器，可任意组合，全局作用于所有卡片与表格。模型选择器是按**开发商**分组的折叠树（Anthropic / OpenAI / DeepSeek 等，归属取自 models.dev 元数据）：点开发商名筛选整组，点 ▸ 展开选单个模型。各种写法的模型 ID（`provider/model`、`cursor-*` 前缀、`-expires-on-*`、日期戳、`[agent]` 前缀）会归一化，同一模型只统计一次
- **Agent 用量表**：各 Agent 的占比、活跃天数、会话数、token 构成与成本 —— 点行即可筛选
- **项目分布表**：各项目跨 Agent 的占比、天数、会话数 —— 点行即可筛选；无原生项目行来源按会话级归属
- **顶部统计条**：总用量 / 净生成 / Cache 命中率 / 估算成本（USD），均带与上一等长周期的环比增减
- **活跃度热力图**：最近一年，GitHub 风格（token / 费用 / 活动量三种口径）；下方附「分时活跃」星期 × 小时网格
- **用量分布**：Agent / 模型 / 项目 三个环形图（token 占比 Top 5 + 其他）
- **每日 Token 构成**：堆叠柱状图（新输入 / 输出 / 缓存读取 / 缓存写入）；范围恰为一天时自动切换为逐小时粒度
- **详细记录表**：小时粒度用量（时间 / Agent / 模型 / 项目 / 输入 / 输出 / 缓存 / 成本）—— 仅原生解析来源有小时粒度，ccusage 来源为日粒度；无公开价目的模型标注「未定价」
- **模型用量表**：带 Agent 归属与 USD 成本
- **按月汇总**、**会话明细**（可按 Agent / 模型 / 项目筛选与搜索）、**数据来源说明卡**
- **时间范围筛选**：今日 / 近 7 / 14 / 30 日 / 全部 / 自定义区间；所有筛选刷新后保持（URL 参数 `a`/`m`/`pj`/`p`/`s`/`e`）
- **中英双语**：右上角一键切换

## 环境要求

- **Python 3** —— 采集器 `collect.py` 与服务 `serve.py`（仅标准库，无需 pip）
- **Node.js** —— 仅 `ccusage` 需要（覆盖 kimi / codex / opencode / kilo / claude / gemini / pi 等）。若 `node_modules/.bin/ccusage` 不存在，这些来源会被跳过并提示警告，其余来源照常工作。

## 快速开始

```bash
npm install   # 安装固定版本的 ccusage 依赖（离线模式，不联网查 registry）
./start.sh    # 启动本地服务（127.0.0.1:8931）并打开页面
```

页面上的「刷新数据」按钮会调用 `refresh.sh` → `collect.py` 重新聚合最新日志；也可以手动执行：

```bash
./refresh.sh
```

## 工作原理

1. `collect.py` 分发到各数据源：一条 `ccusage daily --by-agent` 覆盖所有 ccusage 支持的 agent；另有原生解析器处理 CommandCode / KimiX（JSONL）、DimCode / Devin（SQLite，先复制到临时目录再读，安全读取使用中的 WAL 库）、Antigravity（SQLite + protobuf 解码）和 Cursor（云端用量 CSV + 本地 transcript）；
2. 所有数据归一化为按 `(日期, agent)` 的 token 记录 + 会话级记录，原子写入 `data.js`（`window.DASHBOARD_DATA`，权限 0600）；
3. `serve.py` 托管页面并提供 `/refresh` 接口；`index.html` 纯前端渲染，无框架、无构建步骤。

> `data.js` 是你个人的用量数据，已被 `.gitignore` 排除，不会提交到仓库。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `index.html` | 仪表盘页面（样式 + 渲染逻辑全在里面） |
| `collect.py` | 多 Agent 采集器：ccusage + 原生解析器 → `data.js` |
| `serve.py` | 本地服务：托管静态文件 + `/refresh` 接口 |
| `refresh.sh` | `collect.py` 的锁保护包装 |
| `start.sh` | 启动服务并打开浏览器（重复执行安全） |

## License

[MIT](LICENSE)
