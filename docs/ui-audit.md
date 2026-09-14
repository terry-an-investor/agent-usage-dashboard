# 仪表盘 UI 审计报告（Kimi Web Bridge 只读实测）

- **日期**：2026-09-14
- **对象**：`http://127.0.0.1:8931/`（由 `serve.py` 提供，后台常驻）
- **方法**：Kimi Web Bridge 本地 daemon（`127.0.0.1:10086`），会话 `dashboard-audit`，只做
  `navigate` / `snapshot` / `screenshot` / `evaluate`(仅 `getComputedStyle` 读取)。
  **未点击任何控件、未触发 `/refresh`、未改动任何状态**，纯只读。
- **实测环境**：视口 `1336 × 588`，`devicePixelRatio = 2.2`，页面语言＝中文，URL 自带 `?p=7`

## 结论

顶部筛选条（Agent / 模型 / 项目）确实有问题，而且**不只是"字体"**。实测确认：

| 级别 | 问题 | 位置 |
| --- | --- | --- |
| **P1** | 标签「模型：」「项目：」被挤成**竖排**（逐字换行） | `.agentbar .ab-label` |
| **P2** | 三个下拉框字体回退成 **Arial**，与全站字体不一致 | `.agent-sel` |
| **P3** | 原生外观未重置，自定义圆角/边框被系统控件样式压过 | `.agent-sel` |
| **P4** | 筛选条左右内边距 20px ≠ 其它卡片 24px，**整体错位 4px** | `.agentbar` / `.rangebar` |
| **P5** | 「项目」下拉宽度被撑到 **486.7px**，与 160/221px 的两个框不成比例 | `.agent-sel` |
| **P6** | `.bar-model-sel` 是**死代码**（DOM 中 0 处使用） | `index.html` |
| **P7** | 42 个模型 / 46 个项目塞进原生下拉，无搜索 | `.agent-sel` |
| **P8** | 非标准字重 `550/650/750`、亚像素边框 `0.909px` | 全局 |

---

## P1 — 标签竖排（最显眼）

**现象**：`模型：` 被拆成 `模` / `型` / `：` 三行竖着排，`项 目 ：` 同样；只有 `Agent：` 侥幸没换行。

**根因**：`.agentbar` 是 `display:flex`，标签 `<span class="ab-label">` 没有 `white-space:nowrap`，
默认 `flex-shrink:1` 会**优先压缩标签**；而 `<select>` 有 `min-width:160px` 顶住了压缩。
实测宽度：`160 + 221 + 486.7 + 5×8(gap) = 907.7px`，容器可用仅 `1060 − 2×20 = 1020px`，
留给 3 个标签只剩 ≈112px（每个 ≈37px），而 `模型：` 需要 ≈44px → 中文逐字换行。

**影响**：窗口越窄越严重（该条没有响应式 `flex-wrap`），是最直接命中"文字不太好看"的一条。

**修复**：

```css
.agentbar .ab-label { white-space: nowrap; flex: 0 0 auto; }
```

---

## P2 — 下拉框字体回退成 Arial

**实测证据**（`getComputedStyle`）：

| 元素 | font-family |
| --- | --- |
| `body` | `-apple-system, "system-ui", "SF Pro Text", "PingFang SC", …` |
| `.ab-label`（标签） | `-apple-system, "system-ui", "SF Pro Text", "PingFang SC", …` ✅ |
| `#agentSel` / `#modelSel` / `#projSel` | **`Arial`** ❌ |
| `#sessSort` | **`Arial`** ❌ |

**根因**：表单控件（`select`/`input`/`button`）**不继承** `font-family`，浏览器 UA 样式表给了 `Arial`。
项目里 `.btn-subtle`、`.dp-input`、`.sess-search`、`.btn-copy`、`.dp-head button` 都写了
`font-family: inherit`，**唯独 `.agent-sel` 和 `.sess-sort` 漏了** → 只有下拉框"字体不一样"，
和用户主观感受完全一致。

**修复**（两处都加）：

```css
.agent-sel { font-family: inherit; }
.sess-sort { font-family: inherit; }
```

---

## P3 — 原生外观未重置

**实测**：`appearance: auto`、`-webkit-appearance: auto`。
所以 CSS 里写的 `border-radius:7px; border:1px solid …` 只能部分生效，macOS 仍会绘制
原生的凹陷/箭头（截图里箭头样式明显偏系统风），与页面整体扁平化设计不一致。

**修复**：重置外观并自绘箭头（重置后必须补 `padding-right` 与背景箭头，否则没有下拉提示）：

```css
.agent-sel {
  appearance: none; -webkit-appearance: none;
  padding-right: 26px;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M1 1l4 4 4-4' stroke='%2364748b' stroke-width='1.5' fill='none' stroke-linecap='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 9px center;
}
```

---

## P4 — 左右内边距不一致（4px 错位）

**实测**（同一容器 `left = 138.2`）：

| 卡片 | 实测 padding | 内容左边缘 |
| --- | --- | --- |
| `.card.agentbar` | `10px 20px` | **159.1** |
| `.card.rangebar` | `12px 20px` | **159.1** |
| `.card`（其它） | `20px 24px` | **163.1** |

两条筛选条覆盖 `.card` 的 `padding` 时把左右写成了 `20px`，导致内容比其它卡片**左缩进少 4px**，
纵向对齐看着"差了半格"。**修复**：左右保持 24px，只改纵向。

```css
.agentbar { padding: 10px 24px; }
.rangebar { padding: 12px 24px; }
```

---

## P5 — 「项目」下拉被撑到 486.7px

**实测宽度**：`agentSel 160px`、`modelSel 221px`、`projSel 486.7px`（`min-width:160px`，宽度仍随最长
选项自动增长，无 `max-width`）。三个框宽度 160/221/487 严重不成比例，筛选条视觉被拉垮。

**修复**：配合 P3 的 `appearance:none` 一起加：

```css
.agent-sel { max-width: 220px; text-overflow: ellipsis; }
```

---

## P6 — 死代码 `.bar-model-sel`

`document.querySelectorAll('.bar-model-sel').length === 0`：DOM 中 4 个 `<select>` 分别是
`agentSel.agent-sel` / `modelSel.agent-sel` / `projSel.agent-sel` / `sessSort.sess-sort`，
`.bar-model-sel` 只存在于 CSS（`index.html` 第 149–154 行），从未被引用。建议删除。

---

## P7 — 长列表原生下拉（可用性）

`#modelSel` 42 个选项、`#projSel` 46 个选项，均为原生 `<select>`，无搜索/过滤
（对比：会话明细已有 `.sess-search` 搜索框）。选项名如 `deepseek-v4.1-flash-expires-on-0910`
较长，下拉展开后查找成本高。若要优化需改成可搜索的 combobox（改动较大，非本次范围）。

---

## P8 — 低优先级

- **非标准字重**：`.ab-label{font-weight:550}`、`.tabs button.on{650}`、`h1/.hero-val{750}`。
  Apple 系统字体可插值显示正常，但非 Apple 字体栈下会被取整，建议收敛到 `500/600/700`。
- **亚像素边框**：`devicePixelRatio=2.2` 下 `1px` 边框实测为 `0.909091px`，是 2.2 缩放屏的
  取整结果（`2/2.2`），会造成等宽边框在不同卡片上略有粗细差；非本页 bug，仅记录。

---

## 建议实施顺序

1. **P1 + P2**（各 1 行 CSS，收益最大）——竖排与字体问题一次解决；
2. **P4**（2 行，消除错位）；
3. **P3 + P5**（需要一起改，自绘箭头 + 限宽）；
4. **P6**（删死代码）；**P7/P8** 视需要。

## 复现命令（只读）

```bash
curl -s -X POST http://127.0.0.1:10086/command -H 'Content-Type: application/json' \
  -d '{"action":"navigate","args":{"url":"http://127.0.0.1:8931/","newTab":true},"session":"dashboard-audit"}'
curl -s -X POST http://127.0.0.1:10086/command -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"JSON.stringify({f:getComputedStyle(document.getElementById(\"modelSel\")).fontFamily})"},"session":"dashboard-audit"}'
```
