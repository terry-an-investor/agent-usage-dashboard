# kimi-usage-dashboard

Kimi Code token 用量本地仪表盘 —— 读取本机会话日志，聚合并可视化你的 token 消耗与活跃度。

所有数据都在本地处理，不上传任何东西。

## 功能

- **顶部统计条**：范围内 tokens、单日峰值、平均每日、活跃天数、最长连续
- **Token 活跃度热力图**：最近一年，GitHub 风格
- **指标卡片**：活跃度 / Token / Cache 命中率等
- **每日 Token 构成**：堆叠柱状图（新输入 / 输出 / 缓存读取 / 缓存写入），可按模型筛选
- **模型用量表**：按总 token 排序，含按官方 API 价的估算成本
- **按月汇总** 与 **API 定价参考**
- **时间范围筛选**：今日 / 近 7 日 / 近 14 日 / 近 30 日 / 全部 / 自定义区间；刷新数据后保持所选范围（通过 URL 参数恢复）

## 快速开始

```bash
npm install   # 安装本地固定的 ccusage（数据聚合用）
./start.sh    # 启动本地服务（127.0.0.1:8931）并打开页面
```

页面上的「刷新数据」按钮会调用 `refresh.sh` 重新聚合最新日志并刷新页面；也可以手动执行：

```bash
./refresh.sh
```

## 工作原理

1. 数据来自本机 `~/.kimi-code/sessions/**/wire.jsonl` 中服务器返回的真实 token 计数；
2. `refresh.sh` 用 [ccusage](https://github.com/ryoppippi/ccusage)（本地固定版本，离线模式）聚合出 daily / monthly / session 三份 JSON，合并写入 `data.js`；
3. `serve.py` 托管页面并提供 `/refresh` 接口（页面上「刷新数据」按钮背后调用的就是它）；
4. `index.html` 纯前端渲染，无框架、无构建步骤。

> `data.js` 是你个人的用量数据，已被 `.gitignore` 排除，不会提交到仓库。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `index.html` | 仪表盘页面（样式 + 渲染逻辑全在里面） |
| `serve.py` | 本地服务：托管静态文件 + `/refresh` 接口 |
| `refresh.sh` | 调用 ccusage 聚合日志，重建 `data.js` |
| `start.sh` | 启动服务并打开浏览器（重复执行安全） |

## License

[MIT](LICENSE)
