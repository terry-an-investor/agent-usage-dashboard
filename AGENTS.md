# AGENTS

## 项目

本地 agent token 用量仪表盘：`collect.py` 采集各 agent 用量生成 `data.js`，`index.html` 纯前端渲染，`serve.py` 在 `http://127.0.0.1:8931` 托管页面并提供 `/refresh` 重建接口。

## 测试

三层，各管一件事。改代码前先看这张表，改完按下面的「固化循环」走。

| 层 | 位置 | 跑什么 | 依赖 |
|---|---|---|---|
| 单测 | `tests/test_collect.py` | 采集端的纯逻辑：日期解析、估算、价目表、缓冲指纹、库访问、数据自洽 | 仅 Python 标准库 |
| e2e | `tests/e2e/*.test.js` | 真实浏览器里的渲染与口径：各视图数字是否自洽、筛选/状态是否同步、边界与提示 | Node 内置 `node:test` + **系统 Chrome**（零 npm 依赖） |
| live test | kimi-webbridge（见下节） | **真实数据 + 真实浏览器**的最终确认 | 用户浏览器 |

```bash
npm test              # 单测 + e2e（推荐，提交前必跑）
npm run test:py       # 只跑单测（~0.1s）
npm run test:e2e      # 只跑 e2e（~35s，会起 headless Chrome）
node --test --test-name-pattern 成本 "tests/e2e/*.test.js"   # 只跑匹配的用例
```

## 固化循环：每个 bug 都要变成一条测试

修 bug / 加功能时按这个顺序走，不要跳过第 1 步：

1. **先写一条会失败的测试**复现它。
   - 渲染/口径类 → 加进 `tests/e2e/dashboard.test.js`；优先断言**跨视图一致性**
     （KPI 与表格合计相等、模型表合计不超过 KPI、状态与实际渲染一致），
     而不是复刻实现细节。
   - 采集/数值类 → 加进 `tests/test_collect.py`。数值语义用 `DataJsConsistencyTest`
     那种"对真实产物复算"，纯函数用直接调用。
   - 确实无法自动化的（如只在特定用户数据下出现），在 commit message 里写明原因。
2. **再修代码**，跑到该用例变绿。
3. **跑全量**：`npm test`。单测若因有意变更而变红，同步更新测试并在 commit 里说明。
4. **live test 真实数据**：e2e 用的是合成 fixture，只能证明口径自洽；真实数据的形状
   （缺字段、空值、异常模型名）要靠 live test 兜。改了 `collect.py` 还要先
   `./refresh.sh` 重新生成 `data.js`。
5. **提交**：message 里写清"修了什么、为什么、如何验证"。

### 写测试时的约定

- **e2e 数据一律用合成 fixture**，不碰仓库里真实的 `data.js`（那是个人数据）。
  fixture 在 `tests/e2e/server.py` 里生成，**日期相对"今天"现算**，这样测试不会随时间腐化。
- 每条用例开独立标签页（`openPage()`），互不污染。
- 断言失败信息要能直接读出问题（写清期望与实际），别只写 `assert.ok(x)`。
- `tests/e2e/browser.js` 是零依赖的 CDP 封装（navigate / eval / 时区覆盖三种能力）。
  需要新能力时优先扩展它，而不是引入 Playwright/Puppeteer：
  本机 Playwright 要占 1.1G 浏览器缓存，而这些断言并不需要那套 API。

## Live Test：用 kimi-webbridge

改动 `index.html` / `collect.py` 后，除了上面的自动化测试，还要在**真实数据 + 真实浏览器**里做一次 live test，不要只改代码不验证。

1. 启动服务：`./start.sh`（幂等，等价 `python3 serve.py`），页面 `http://127.0.0.1:8931/index.html`。
2. kimi-webbridge daemon 默认 `http://127.0.0.1:10086`；连不上先跑 `~/.kimi-webbridge/bin/kimi-webbridge start`（只许 `start`，不要 `stop`/`restart`）。
3. 一次 live test 固定一个 session 名（如 `usage-dashboard-livetest`），每个请求都带顶层 `session` 字段：

   ```bash
   curl -s -X POST http://127.0.0.1:10086/command \
     -H 'Content-Type: application/json' \
     -d '{"action":"navigate","args":{"url":"http://127.0.0.1:8931/index.html","newTab":true,"group_title":"用量仪表盘 live test"},"session":"usage-dashboard-livetest"}'
   ```

   > 用 `curl -d` 内联 JSON 时**不要**在 `code` 字段里放未转义的双引号或 `\s`，
   > shell 与 JSON 双重转义很容易弄坏请求体；复杂表达式改用
   > `--data-binary @/tmp/req-<随机>.json`（文件用文件工具写，别用 heredoc）。
4. 常用检查：
   - `snapshot` 读页面结构定位元素（用 `@e` ref 操作，优于手写 CSS 选择器）；
   - `evaluate` 直接断言：读 `window.DASHBOARD_DATA`、检查各环图/表格总量是否一致、模拟筛选交互（`evaluate` 共享页面 JS realm，重复声明用 IIFE 包裹）；
   - `cdp` 可覆盖时区（`Emulation.setTimezoneOverride`）等环境，测时区相关逻辑；
   - `screenshot` 返回文件路径，用 Read 工具查看。
5. 结束时不要主动 `close_session`，标签页关闭由用户发起。

## 容易踩的坑（已有测试覆盖，改这些地方时先看用例）

- **成本口径**：`costUsd` 与 `costEst` 可以并存，按天/按行二选一会静默丢数据；
  渲染统一走 `costCell()`，判断"有没有记账成本"必须看 `hasCost`（无成本的行也带 `cost: 0`）。
- **日期算术**：用 `shiftDay/isoOf` 这类"天序号"运算，别混用
  `new Date('YYYY-MM-DD')`（按 UTC 解析）与 `getDate()/iso()`（按本地取值）。
- **并行采集**：`collect.py` 的 7 个来源跑在线程池里，共享聚合状态的读写必须持 `_state_lock`。
- **增量缓存**：缓存用"输入指纹"兜正确性；给 `_input_sources()` 增删输入时，
  要保证指纹能覆盖它，否则会拿旧结果。sqlite 不能用 mtime 做指纹
  （活跃 WAL 的 mtime 每几秒就变，缓存会永不命中）。
- **无 token 来源**：`hasTokens: false` 的 agent 走"活动量"口径，
  KPI 标题、tab 可见性、图例要一起切。
