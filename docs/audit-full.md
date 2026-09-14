# 全功能审计报告 — Agent 用量仪表盘

- **日期**：2026-09-14
- **审计对象**：`http://127.0.0.1:8931/`（`serve.py`，PID 40228，后台常驻）
- **审计方式**：Kimi Web Bridge 本地 daemon（`127.0.0.1:10086`），会话 `dashboard-audit`
- **覆盖范围**：顶栏 / 筛选条（Agent·模型·项目）/ 时间范围（预设＋自定义日历）/ 4 张 KPI 卡 /
  活跃度热力图 / 每日趋势图 / Agent 用量表 / 模型用量表 / 按月汇总 / 项目分布 / 会话明细 /
  数据来源折叠卡 / 页脚 / 中英切换 / URL 状态恢复 / 响应式断点 / 无障碍 / 性能 / 安全响应头
- **实测环境**：视口 1336×588（`dpr 2.2`），数据 `data.js` 523 KB，13 agents / 121 日行 / 1206 会话 / 134 项目行

## 只读声明（重要）

我**没有**改动任何数据或代码：未点击「刷新数据」（那会重跑 `collect.py` 覆写 `data.js`）、
未触发 `/refresh`（唯一一次跨站 POST 返回 403，未产生副作用）、未点击「复制」按钮。
交互只改动浏览器端状态（筛选、时间范围、图表模式、语言、`history` URL），并可用刷新还原。
涉及「改值」的操作（下拉、搜索框）通过派发原生 `change`/`input` 事件驱动，走的是与真人操作完全相同的代码路径。

---

## 一、结论摘要

**功能面：整体可用，没有崩溃或数据错乱。** 全站零 JS 报错（整轮交互中 `onerror`/`unhandledrejection` 计数为 0），
四个 KPI 与原始数据**逐位吻合**，预设时间范围、行点击筛选、图表三种模式、会话搜索/排序/展开、日期选择器、
中英切换、URL 恢复全部正常。

**问题集中在三类：数字可信度、窄屏布局、无障碍。**

| # | 级别 | 问题 | 模块 |
| --- | --- | --- | --- |
| A1 | **高** | 非法/过期 URL 参数使控件显示"全部"，但全页进入**假零态**且翻页不恢复 | URL 状态 |
| A2 | **高** | 选了「模型」后，Agent 用量表不过滤 → 同页出现 3.67B 与 1.45B 两个总用量 | Agent 表 |
| A3 | **高** | 「项目分布」不跟随时间范围 → 近 7 日 45 行里 **27 行是空的**，今日 45 行里 **36 行是空的** | 项目表 |
| A4 | 中 | 模型下拉 41 项 vs 模型用量表 47 行，对不上 | 模型筛选 |
| A5 | 中 | `hasTok` 用全时段判定、数字按范围过滤 → Cursor 类来源显示 `0/0/0.0%/—` 而非活动量 | KPI |
| A6 | 中 | 硬编码英文单位：中文界面出现 `(最长 7d)`；英文是 `7days`（还少了空格） | KPI |
| A7 | 中 | `collect.py` 写的 `warnings` 前端**从不展示** → 采集失败对用户完全静默 | 数据来源 |
| B1 | **高**（视觉） | 「模型：」「项目：」标签被挤压**竖排**，所有宽度都复现 | 筛选条 |
| B2 | **高**（视觉） | 三个下拉框字体实算 **Arial**，与全站字体不一致 | 筛选条 |
| B4 | 中 | 窄屏（vw ≤ 700）表格撑破页面，整页横向滚动到 775px | 表格 |
| C1 | 中 | 键盘焦点**不可见**（全站仅 1 条 `:focus` 规则） | 无障碍 |
| C4 | 中 | 5 处文本对比度低于 WCAG AA（最低 **2.56**，字号仅 10.5px） | 无障碍 |
| C2/C3 | 中 | 6 个表单控件无程序化标签；10 个分段按钮 **0 个** `aria-pressed` | 无障碍 |
| D1 | 低 | `data.js` 523 KB 未压缩 + `no-store`，每次加载全量重下 | 性能 |

B1/B2 的 CSS 级细节与修复片段见 `docs/ui-audit.md`（本次一并复测确认）。

---

## 二、服务与运行时

- `start.sh` 用 `nohup python3 serve.py` 常驻后台，轮询等端口起来再 `open` 页面；PID 40228 监听 `127.0.0.1:8931`（仅回环）。
- 页面资源只有 2 个请求：`index.html` 83 KB、`data.js` 523 KB（+ favicon）。导航总耗时 **239 ms**，`data.js` 解析 **7 ms**，DOM **2173** 节点 → 当前规模下性能良好。
- 会话明细「展开更多」一次渲染 **1206 行仅 20 ms**，可接受。

## 三、功能逐项验证

| 模块 | 结果 | 实测证据 |
| --- | --- | --- |
| 预设范围（今日/7/14/30/全部） | ✅ | 范围、KPI、柱数同步；`today 661M → 7d 3.67B → 14d 3.71B → 30d 3.93B → all 5.49B`；URL `?p=` 同步 |
| 自定义日期 + 查询 | ✅ | 弹层 `2026年09月`、30 天、16 天禁用；「今天」回填并关闭；`查询` 生效且清除预设高亮；空值点击 = 无操作；`2030-01-01/1999-01-01` 被规范化为 `2026-04-29 ~ 2026-09-14` |
| Agent 筛选 | ✅ | `codex`→Agent 表 1 行、模型下拉 3 项；`dimcode`→`[14,12,17]`；`cursor`→`[14,16,12]`；下拉与表格联动重建 |
| 模型筛选 | ⚠️ | 过滤生效（KPI 1.45B），但 **Agent 表未跟随**（见 A2） |
| 项目筛选 | ⚠️ | 过滤生效、行高亮；但项目**行集合**不跟随范围（见 A3） |
| 行点击筛选 / 再点取消 | ✅ | Agent 行→`?a=dimcode`、项目行→`?pj=Desktop%2Ftrading-logic`，再点还原 |
| 热力图 Token/费用切换 | ✅ | 标题 `Token 活跃度`↔`费用活跃度`，色阶重算 |
| 趋势图 全量/净生成/费用 | ✅ | 柱数 21/14/7，图例 3/2/1 项，费用模式 Y 轴变 `$7.68…$30.73` |
| 图表 tooltip | ✅ | 热力图格 `2026-09-14 / 669M tokens`；柱 `2026-09-08 · 275M / 新输入 3.5M / 输出 0.87M / 缓存读取 271M / 净生成 4.37M / 估算成本 $0.03`；离开即隐藏 |
| 会话明细 搜索 | ✅ | 1206 → `dimcode` 913 → `zzzzzz` 显示「未找到匹配的会话记录」并隐藏展开按钮 |
| 会话明细 排序 | ✅ | total/cost/net 三种首行各不相同 |
| 会话明细 展开/收起 | ✅ | 8 ↔ 1206 行，按钮文案切换 |
| 按月汇总 | ✅ | 固定全时段 6 行（与「固定全时间段」的说明一致） |
| 数据来源折叠卡 | ✅ | 默认收起，点击展开，13 行 |
| 中英切换 | ✅ | `<html lang>` 与标题、预设、周月名、占位符、图例全量切换；两种语言下 **0 个空 i18n 节点**（无缺键） |
| URL 恢复 | ⚠️ | 正常参数可恢复；非法参数会触发 A1 |
| 复制会话 ID | ⏭️ | 未点击（失败兜底会弹**阻塞式 `prompt()`**，自动化下会卡住页面）；`isSecureContext=true` 且 `navigator.clipboard` 可用 |

## 四、缺陷详情

### A1（高）非法 URL 参数 → 全页假零态

**复现**：打开 `/?a=nope&m=nomodel&pj=noproj`

**现象**：下拉框显示「全部 Agent / 全部模型 / 全部项目」，时间范围显示正常 7 日，
但 **KPI `0`、净生成 `—`、热力图标题变成「活跃度」且 371 格全空**；点「今日」预设也**不恢复**。

**根因**：`index.html` `renderAgentBar()` 在校验失败时静默改写状态（`selModel='all'; modelSelEl.value='all'`），
但**没有重新 `rebuildView()`**，于是 `daily` 仍为空、`hasTok` 仍为 `false`，控件与数字就此脱钩。
初始化时只有 `a` 参数做了校验（`if (a && AGENTS[a])`），`m`/`pj` 完全没有校验。

**建议**：初始化时把 `m`/`pj` 与真实可选值比对后再采用；或在 `renderAgentBar()` 改写状态后补一次
`rebuildView(); computeStreak(); computeMaxes();`。

### A2（高）模型筛选下 Agent 表与 KPI 矛盾

**证据**：`?p=7&m=deepseek-v4.1-flash` → KPI `1.45B`，Agent 表合计仍为 **3.67B**（比值 2.531）。

**根因**：`renderAgents()` 聚合的是 `viewRows`（只按 agent+project 过滤），不含模型过滤；
而 `renderHero/renderBars/renderHeat` 用的是 `daily`（含模型过滤）。

**建议**：让 Agent 表也走模型过滤后的数据集（用 `daily` 聚合，或在 `renderAgents()` 内按 `selModel` 过滤）。

### A3（高）项目分布不跟随时间范围

**证据**：

| 范围 | 项目表行数 | 其中 `总计=—` 且 `天数=0` |
| --- | --- | --- |
| 近 7 日 | 45 | **27** |
| 今日 | 45 | **36** |
| 全部 | 45 | 10 |

**根因**：`renderProjects()` 末尾遍历 `D.sessions` 时无条件 `agg[s.project] = agg[s.project] || {...}`，
只要历史上某个项目有过会话就会建行，且不判断该会话是否落在范围内。

**建议**：仅当范围内确有数据时才建行（`tot/ev` 均为 0 时不建行），或对该循环加范围判断。

### A4（中）模型下拉与模型表计数不一致

**证据**：数据里 41 个不同模型名；下拉 41 项；模型表 **47 行**——同名模型被按 agent 拆行：
`deepseek-v4-flash`×3、`deepseek-v4-pro`×3、`k3-256k`×2、`gemini-3.8-flash`×2。

**根因**：表格用 `modelKeyOf()`（agent 为「全部」时键为 `agent::model`），下拉用裸 `modelName` 聚合。

**建议**：两者取同一口径；若保留按 agent 拆行，下拉也应显示为 `agent · model` 以便定位。

### A5（中）时间范围与 `hasTok` 口径不一致

**证据**：项目 `cursor.com` 全时段 641M tokens，但近 7 日为 0 → 页面停留在 token 模式显示 `0 / 0 / 0.0% / —`。
且本机 **13 个 agent 全部 `hasTokens:true`** → 前端那套「活动量模式」（KPI 活动量、隐藏柱状模式切换、热力图「活跃度」）
在当前数据下实际不可达。

**根因**：`hasTok` 由 `src.some(r => r.totalTokens > 0)`（**全时段**）决定，而数字按所选范围过滤；
`collect.py` 的 `hasTokens = bool(info.get("tokens"))` 也只在"从未采集到 token"时才为假。

**建议**：`hasTok` 改为按范围判定；`collect.py` 在来源不可用时（如 Cursor 登录态失效）显式输出 `hasTokens:false`。

### A6（中）硬编码英文单位

**证据**：中文 `日均 $7.06 · 当前连续 7天 (最长 7d)`；英文 `Daily avg $7.06 · Current streak 7days (Best 7d)`。

**根因**：`renderHero()` 中 `` `${S.kpiStreakMax} ${rangeBestStreak}d` `` 里的 `d` 是硬编码；
`` `${curStreak}${S.kpiDaysUnit}` `` 又缺空格。

**建议**：`${rangeBestStreak}${S.kpiDaysUnit}`，并给 `${curStreak} ${S.kpiDaysUnit}` 补空格。

### A7（中）采集警告被静默丢弃

**证据**：`data.js` 顶层有 `warnings` 数组；`collect.py` 会往里写
「ccusage 未安装…」「cursor 登录态失效…」「dimcode 读取失败…」等，并在 stderr 打印；
但 `index.html` **全文没有读取 `warnings`**（本次实测该数组为空，所以问题未暴露）。

**建议**：在「数据来源与覆盖说明」卡片或顶部加一条警示条，把 `D.warnings` 渲染出来。

### B1 / B2（高·视觉）筛选条标签竖排 + 下拉框字体为 Arial

**证据**：实测 `.ab-label` 高度 **34px**（单行应为 17px）、宽度被压到 **24px**，
在 1336/1214/900/700/520 各宽度**全部复现**；`#agentSel/#modelSel/#projSel/#sessSort`
的 `font-family` 实算为 **`Arial`**，而 `body` 与标签都是 `-apple-system, "SF Pro Text", "PingFang SC"…`。

**根因**：`.ab-label` 缺 `white-space:nowrap`，flex 收缩全部落在标签上（下拉框有 `min-width:160px` 顶住）；
`<select>` 不继承字体，而项目里 `.btn-subtle`/`.dp-input`/`.sess-search` 都写了 `font-family:inherit`，唯独两个 select 漏了。

**修复**（详见 `docs/ui-audit.md`）：

```css
.agentbar .ab-label { white-space: nowrap; flex: 0 0 auto; }
.agent-sel, .sess-sort { font-family: inherit; }
```

### B4（中）窄屏整页横向滚动

**证据**：vw 700 / 520 / 390 时文档宽度固定为 **775px**，溢出的最外层元素是
`#projTable`（730px）与 `#sessionTable`（728px）；vw ≥ 900 无溢出。

**根因**：热力图有 `.heat-scroll{overflow-x:auto}` 兜底，但表格没有等价的横向滚动容器。

**建议**：给各表格加 `.table-scroll{overflow-x:auto}` 包裹层（并同步给 `.card` 加 `min-width:0`）。

### 其余视觉项（低）

沿用 `docs/ui-audit.md`：`.bar-model-sel` 为死代码（DOM 内 0 处引用）；筛选条左右 padding 20px vs 卡片 24px（内容错位 4px）；
「项目」下拉被最长选项撑到 **487px**；42 个模型 / 46 个项目塞进原生下拉且无搜索。

## 五、无障碍

| 项 | 实测 | 说明 |
| --- | --- | --- |
| 键盘焦点可见性 | ❌ | 全站样式表里 `:focus` 规则**只有 1 条**（`.sess-search:focus`）；`.agent-sel`/`.sess-sort` 明确 `outline:none` 且无替代 → 键盘用户看不出焦点在哪 |
| 表单标签 | ❌ | `#agentSel/#modelSel/#projSel/#sessSort/#dStart/#dEnd/#sessSearch` 共 7 个控件无 `label[for]`/`aria-label`（`.ab-label` 是 `<span>`）→ 读屏只念 "combobox" |
| 选中态语义 | ❌ | 10 个分段按钮（时间预设 / 热力图指标 / 图表模式）**0 个** `aria-pressed`、**0 个** `role`，选中态只靠 `.on` class |
| 对比度（AA 需 4.5） | ❌ | `.sess-proj` 10.5px **2.56**、`.price-badge` 11px **2.56**、图表坐标轴 11 处（`fill:#94a3b8`、10.5px）**2.56**、`.badge-tag` **3.57**、未选中 `.tabs button` **4.20** |
| 小字号 | ⚠️ | 11px×63、11.5px×57、10.5px×27 个文本节点 < 12px |
| 热力图可达性 | ❌ | 格子是 `div`，提示仅绑 `mousemove` → 键盘/触摸无法查询；且 `user-select:none` |
| 弹层键盘操作 | ❌ | 日期弹层只能点外部关闭，无 Esc、无焦点管理 |
| 按钮可访问名 | ✅ | 无空名按钮；无重复 `id` |
| 结构 | ✅ | 语言切换会同步 `<html lang>`；表格用 `<th>` |

## 六、安全与隐私

| 项 | 结果 |
| --- | --- |
| 监听范围 | ✅ 仅 `127.0.0.1:8931` |
| `Content-Security-Policy` | ✅ `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'` |
| 点击劫持 | ✅ `X-Frame-Options: DENY` |
| 缓存 | ✅ 页面与 `data.js` 均 `Cache-Control: no-store` |
| CSRF | ✅ 跨站 `Origin` 的 `POST /refresh` 返回 **403**（未触发任何刷新）；同源/无 Origin 才放行 |
| 跨域读取 | ✅ 无 CORS 响应头，`data.js` 不被其它源读取 |
| 数据文件权限 | ✅ `data.js` `-rw-------`（0600），仅本机用户可读 |
| 其它 | ⚠️ `Server: SimpleHTTP/0.6 Python/3.12.13` 泄露版本号（本机服务，影响极小）；HTTP/1.0 无 keep-alive；`data.js` 未压缩 |

**隐私**：「数据来源」卡直接把本机目录（`~/.codex/sessions`、`~/.local/share/devin/cli/sessions.db` 等）明文渲染在页面上，
会话 ID 与项目路径（`Desktop/trading-logic`、`myprojects/kimi-usage-dashboard`）也全部可见。
本机自用没问题，但**截图/录屏分享前需要留意**（这正是 README 里截图为手工准备的原因）。

## 七、修复优先级建议

1. **A1**（假零态）—— 影响"打开的页面是错的"这类最坏体验，且修复点很小（补一次 `rebuildView()` 或校验参数）。
2. **A2 / A3**（表与 KPI 互相矛盾、空行泛滥）—— 直接损害"数字可信度"。
3. **B1 + B2**（各一行 CSS）—— 视觉收益最大，一次性解决"文字不好看"。
4. **B4 + C1 + C2 + C3**（表格滚动容器、`:focus-visible`、`label`/`aria-pressed`）。
5. **A5 / A6 / A7**（口径统一、单位、警告展示）。
6. **A4、C4、C5-C7、D1**（口径与打磨）。

## 附录：复现要点

```bash
# 所有只读探测都通过本地 WebBridge daemon
curl -s -X POST http://127.0.0.1:10086/command -H 'Content-Type: application/json' \
  -d '{"action":"navigate","args":{"url":"http://127.0.0.1:8931/","newTab":true},"session":"dashboard-audit"}'

# A1：非法参数 -> 观察控件显示"全部"但 KPI=0
#   http://127.0.0.1:8931/?a=nope&m=nomodel&pj=noproj

# A2：模型筛选下比对 KPI 与 Agent 表合计
#   http://127.0.0.1:8931/?p=7&m=deepseek-v4.1-flash   -> KPI 1.45B vs 表 3.67B

# B2：字体
#   getComputedStyle(document.getElementById('modelSel')).fontFamily  -> "Arial"
```

> 未执行项：`/refresh`（会覆写 `data.js`）、「刷新数据」按钮、「复制」按钮（失败兜底是阻塞式 `prompt()`）、
> 「无数据/无 data.js」空态（需破坏数据才能复现，仅做了代码走查）。
