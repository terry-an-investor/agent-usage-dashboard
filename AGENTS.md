# AGENTS

## 项目

本地 agent token 用量仪表盘：`collect.py` 采集各 agent 用量生成 `data.js`，`index.html` 纯前端渲染，`serve.py` 在 `http://127.0.0.1:8931` 托管页面并提供 `/refresh` 重建接口。

## Live Test：用 kimi-webbridge

改动 `index.html` / `collect.py` 后，用 **kimi-webbridge** skill 在真实浏览器里做 live test 验证，不要只改代码不验证。

1. 启动服务：`./start.sh`（幂等，等价 `python3 serve.py`），页面 `http://127.0.0.1:8931/index.html`。
2. kimi-webbridge daemon 默认 `http://127.0.0.1:10086`；连不上先跑 `~/.kimi-webbridge/bin/kimi-webbridge start`（只许 `start`，不要 `stop`/`restart`）。
3. 一次 live test 固定一个 session 名（如 `usage-dashboard-livetest`），每个请求都带顶层 `session` 字段：

   ```bash
   curl -s -X POST http://127.0.0.1:10086/command \
     -H 'Content-Type: application/json' \
     -d '{"action":"navigate","args":{"url":"http://127.0.0.1:8931/index.html","newTab":true,"group_title":"用量仪表盘 live test"},"session":"usage-dashboard-livetest"}'
   ```

4. 常用检查：
   - `snapshot` 读页面结构定位元素（用 `@e` ref 操作，优于手写 CSS 选择器）；
   - `evaluate` 直接断言：读 `window.DASHBOARD_DATA`、检查各环图/表格总量是否一致、模拟筛选交互（`evaluate` 共享页面 JS realm，重复声明用 IIFE 包裹）；
   - `screenshot` 返回文件路径，用 Read 工具查看。
5. 结束时不要主动 `close_session`，标签页关闭由用户发起。
