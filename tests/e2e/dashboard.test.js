'use strict';
// 前端 e2e（node:test + CDP，零依赖）：断言"各视图口径一致"。
//
// 每条用例都对应一个**曾经真实出现过并被修掉**的缺陷，用来防回归；
// 数据由 tests/e2e/server.py 合成（日期相对今天生成，测试不随时间腐化）。
//
//   npm run test:e2e          # 全部
//   node --test --test-name-pattern 成本 tests/e2e/
const { test, before, after, describe } = require('node:test');
const assert = require('node:assert/strict');
const { BASE, startAll, stopAll, openPage } = require('./browser.js');

before(startAll, { timeout: 60000 });
after(stopAll);

const num = (s) => {
  const v = parseFloat(String(s ?? '').replace(/[^0-9.]/g, ''));
  return Number.isNaN(v) ? 0 : v;
};

/** n 天前的本地日期（与 tests/e2e/server.py 的 d() 同一算法） */
const iso = (n) => {
  const t = new Date();
  t.setDate(t.getDate() - n);
  const p = (x) => String(x).padStart(2, '0');
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}`;
};

async function kpiCost(page) {
  return num(await page.text('#kpiCostVal'));
}

/** Agent 表「成本」列合计（在页面内算，避免多次往返） */
async function agentCostSum(page) {
  return page.eval(`
    const num = (t) => { const v = parseFloat(String(t).replace(/[^0-9.]/g, '')); return isNaN(v) ? 0 : v; };
    return [...document.querySelectorAll('#agentTable tbody tr')]
      .filter((tr) => tr.children.length >= 10)
      .reduce((s, tr) => s + num(tr.children[9].textContent), 0);
  `);
}

async function agentSessSum(page) {
  return page.eval(`
    const num = (t) => { const v = parseFloat(String(t).replace(/[^0-9.]/g, '')); return isNaN(v) ? 0 : v; };
    return [...document.querySelectorAll('#agentTable tbody tr')]
      .filter((tr) => tr.children.length >= 10)
      .reduce((s, tr) => s + num(tr.children[3].textContent), 0);
  `);
}

/** 模型表的成本单元格：文本 + 是否估算样式 + 是否「未定价」 */
async function modelCosts(page) {
  return page.eval(`
    return [...document.querySelectorAll('#modelTable tbody tr')]
      .filter((tr) => tr.children.length > 1)
      .map((tr) => {
        const cell = tr.children[7];
        return {
          model: tr.children[0].textContent.trim(),
          text: cell.textContent.trim(),
          isApprox: !!cell.querySelector('.est-cost'),
          isUnpriced: !!cell.querySelector('.unpriced'),
        };
      });
  `);
}

async function withPage(fn, query = '', opts) {
  const page = await openPage();
  try {
    await page.goto(`${BASE}/index.html${query}`, opts);
    return await fn(page);
  } finally {
    await page.close();
  }
}

// ------------------------------------------------------------- 成本口径

describe('成本口径（H2 / H3 / H4）', () => {
  test('KPI 成本 = 记账 + 估算，且与 Agent 表合计一致', async () => {
    await withPage(async (page) => {
      // fixture：acct 记账 1.5+0.5+0.25+1.0=3.25，est 估算 2.25 ⇒ 合计 5.5
      assert.ok(Math.abs(await kpiCost(page) - 5.5) < 0.05, 'KPI 成本应为 5.5');
      assert.ok(Math.abs((await kpiCost(page)) - (await agentCostSum(page))) < 0.1,
        'KPI 成本与 Agent 表成本列合计应一致');
      // 含估算就必须带 ≈ 前缀，不能把估算伪装成记账金额
      assert.ok(await page.count('#kpiCostVal .est-cost') > 0, 'KPI 成本应带 ≈ 前缀');
    }, '?p=all');
  });

  test('模型表区分「记账 / 估算 / 未定价」三种成本', async () => {
    await withPage(async (page) => {
      const by = Object.fromEntries((await modelCosts(page)).map((c) => [c.model, c]));
      // 回归点：曾读聚合对象上不存在的 hasCost 字段，记账成本全被写成 ≈$0.00
      assert.equal(by['gpt-x'].text, '$3.25', 'gpt-x 应显示记账成本 $3.25');
      assert.equal(by['gpt-x'].isApprox, false, '记账成本不应带 ≈ 前缀');
      assert.equal(by['claude-y'].isApprox, true, '估算成本应带 ≈ 前缀');
      // 回归点：无价目模型曾显示 ≈$0.00，「未定价」永不出现
      assert.equal(by['no-price-model'].isUnpriced, true, '无价目模型应显示「未定价」');
    }, '?p=all');
  });

  test('按开发商筛选后成本不被清零', async () => {
    await withPage(async (page) => {
      // 回归点：模型筛选路径漏判 hasCost，曾把成本整体变成 $0.00
      assert.ok(await kpiCost(page) > 0, '开发商筛选下成本不应为 0');
      assert.equal(await page.count('#kpiCostVal .unpriced'), 0);
    }, '?p=all&m=dev:OpenAI');
  });

  test('同一明细不会既算记账又算估算（无双计）', async () => {
    await withPage(async (page) => {
      const modelSum = (await modelCosts(page)).reduce((s, c) => s + num(c.text), 0);
      assert.ok(modelSum <= (await kpiCost(page)) * 1.02,
        `模型表成本合计 ${modelSum} 不应超过 KPI`);
    }, '?p=all');
  });
});

// --------------------------------------------------------- 筛选与状态

describe('筛选与状态', () => {
  test('KPI 会话数与 Agent 表会话列同口径（M1）', async () => {
    // 会话数只在"活动量口径"下出现在 KPI 上，所以用无 token 的来源做断言。
    // 回归点：KPI 曾只按 agent 过滤时间范围，?a=cursor&p=7 时显示 78 而表里是 0
    await withPage(async (page) => {
      assert.ok((await page.text('#kpiTotalTitle')).includes('活动量'), '应处于活动量口径');
      const sub = await page.text('#kpiTotalSub');
      const kpiSess = num((sub.match(/会话\s*([\d,]+)/) || [])[1]);
      assert.equal(kpiSess, await agentSessSum(page), 'KPI 会话数应等于 Agent 表会话列合计');
    }, '?a=evonly&p=all');
  });

  test('展开区里的会话跟随时间范围（M2）', async () => {
    // 回归点：会话面板曾完全忽略时间范围，恒为全部条数。
    // 会话明细已并入项目分布的行展开区，口径必须跟着一起走。
    const expandedSessions = (page) => page.eval(`
      return [...document.querySelectorAll('#projTable tbody tr.proj-expand .sess-row')].length;
    `);
    const countAt = (query) => withPage(async (page) => {
      await page.click('#projTable tbody tr[data-proj="Projects/proj-a"]');
      assert.equal(await page.eval("return document.body.innerText.includes('{n}')"), false,
        '展开按钮不应残留 {n} 模板');
      return expandedSessions(page);
    }, query);
    const allCount = await countAt('?p=all');
    const todayCount = await countAt('?p=today');
    assert.ok(allCount > todayCount,
      `全时段会话数(${allCount}) 应多于今日(${todayCount})`);
  });

  test('趋势图 tab / 图例 / 实际渲染三者一致（M4）', async () => {
    await withPage(async (page) => {
      await page.click('#barModeTabs button[data-bm="cost"]');
      assert.ok((await page.text('#barLegend')).includes('估算成本'), '费用模式图例应是估算成本');

      // 切到"只有活动量"的来源：tabs 隐藏、图例改为事件
      await page.select('#agentSel', 'evonly');
      assert.equal(await page.eval("return document.getElementById('barModeTabs').style.display"),
        'none', '活动量模式应隐藏口径切换');
      assert.ok((await page.text('#barLegend')).includes('消息'), '活动量模式图例应是消息/事件');

      // 切回：应恢复用户选的"费用"，且高亮与图例同步
      await page.select('#agentSel', 'acct');
      const on = await page.eval(
        "const b = document.querySelector('#barModeTabs button.on'); return b && b.dataset.bm;");
      assert.equal(on, 'cost', '应恢复用户此前选择的费用口径');
      assert.ok((await page.text('#barLegend')).includes('估算成本'), '图例应与高亮一致');
    }, '?p=all');
  });

  test('仅会话级项目下钻后不归零（M3）', async () => {
    // 注意只断言"表格没有退化成无记录"：该项目只有活动量、没有 token，
    // 分布环图在 total=0 时显示提示是预期行为
    for (const pj of ['proj-session-only', 'proj-acct-orphan']) {
      await withPage(async (page) => {
        assert.ok(num(await page.text('#kpiTotalVal')) > 0, `${pj}: KPI 不应为 0`);
        const agentText = await page.eval(
          "return document.querySelector('#agentTable tbody').textContent");
        assert.equal(agentText.includes('无记录'), false, `${pj}: Agent 表不应退化成无记录`);
        const projText = await page.eval(
          "return document.querySelector('#projTable tbody').textContent");
        assert.equal(projText.includes('无记录'), false, `${pj}: 项目表不应退化成无记录`);
      }, `?p=all&pj=${pj}`);
    }
  });

  test('同名项目被多个 agent 共用时，会话兜底不被别的 agent 的日粒度挡住（M3b）', async () => {
    // 回归点：判"该来源是否已有项目级日粒度"用的是只按**项目名**收集的全局 Set，
    // 于是只要别的 agent 有同名项目的日粒度，本 agent 的会话兜底就被整段排除
    await withPage(async (page) => {
      // acct 的日粒度 1.0 + est 的会话兜底 0.4（成本不缩写，断言最稳）
      const cost = await kpiCost(page);
      assert.ok(Math.abs(cost - 1.4) < 0.02,
        `项目下钻应含两个 agent（记账 1.0 + 估算 0.4 = 1.4），实际 ${cost}`);
    }, '?p=all&pj=proj-shared');

    await withPage(async (page) => {
      // 只看"仅有会话"的那个 agent：修复前这里整盘归零（KPI 0 / 成本 —）
      const cost = await kpiCost(page);
      assert.ok(Math.abs(cost - 0.4) < 0.02, `a=est 下钻成本应为 0.4，实际 ${cost}`);
      const kpi = num(await page.text('#kpiTotalVal'));
      assert.ok(kpi > 0, 'a=est 下钻不应归零');
    }, '?p=all&pj=proj-shared&a=est');

    await withPage(async (page) => {
      // aggProjects 那半边也要跟上：项目表该行总量必须与 KPI 同口径。
      // 此前只有 KPI 被断言，把 aggProjects 单独改回"按项目名判重"用例不会变红
      const kpi = num(await page.text('#kpiTotalVal'));
      const row = num(await page.eval(`
        const tr = document.querySelector('#projTable tbody tr');
        return tr ? tr.children[5].textContent : '';
      `));
      assert.ok(kpi > 0 && row > 0, `项目表应有该行（kpi=${kpi} row=${row}）`);
      assert.ok(Math.abs(row / kpi - 1) < 0.05,
        `项目表该行总量(${row})应与 KPI(${kpi})同口径`);
    }, '?p=all&pj=proj-shared');
  });

  test('仅会话级项目的成本与模型计数跟着会话兜底走（M3c）', async () => {
    // 「项目+模型」双筛选：合成 modelBreakdowns 曾把 cost/costEst 写死 null，
    // 模型分支只读 mb 成本 → 成本整段丢失，而同屏会话表显示真实成本
    await withPage(async (page) => {
      const cost = await kpiCost(page);
      assert.ok(Math.abs(cost - 0.3) < 0.02,
        `单模型会话的成本应无歧义归属该模型（0.3），实际 ${cost}`);
    }, '?p=all&pj=proj-sesonly-tok&m=claude-y');

    // 模型下拉的计数也要含会话兜底，否则这里全为 0 并置灰，而模型表有真实用量
    await withPage(async (page) => {
      const counts = await page.eval(`
        return [...document.querySelectorAll('#modelSelPanel .cnt')].map(e => e.textContent.trim());
      `);
      assert.ok(counts.length > 0, '模型下拉应有条目');
      assert.ok(counts.some(c => c !== '0' && c !== '—' && c !== ''),
        `仅会话级项目下模型计数不应全为 0：${JSON.stringify(counts)}`);
    }, '?p=all&pj=proj-sesonly-tok');
  });

  test('近似行的旧日期不会把 p=all 的范围拉早（L2）', async () => {
    // proj-old-approx 的真实日粒度在 d(1)，而它的会话最后活动日在 30 天前。
    // 切到该项目再点"全部"时，rangeStart 取的是**该项目视图**的 firstDate：
    // 若 firstDate 取到近似行，范围会被整段拉早（连带"活跃天数"等失真）
    await withPage(async (page) => {
      await page.select('#projSel', 'proj-old-approx');
      await page.click('#presetTabs button[data-p="all"]');
      const label = await page.text('#rangeLabel');
      const m = label.match(/(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})/);
      assert.ok(m, `范围标签应含起止日期，实际 ${JSON.stringify(label)}`);
      assert.equal(m[1], iso(1),
        `范围起点应为真实日粒度最早一天 ${iso(1)}，而不是近似行的 ${iso(30)}`);
    });
  });

  test('多模型会话在开发商筛选下与 KPI 同口径（L3）', async () => {
    // 项目表曾用 `.every(modelMatch)` 纳入多模型会话（只要各模型都命中该开发商），
    // 而 rebuildView 只认单模型会话 → 项目表比 KPI/模型表多出这一块
    await withPage(async (page) => {
      const kpi = num(await page.text('#kpiTotalVal'));
      const row = num(await page.eval(`
        const tr = document.querySelector('#projTable tbody tr');
        return tr ? tr.children[5].textContent : '';
      `));
      assert.equal(row > 0, kpi > 0,
        `多模型会话应与 KPI 同口径（kpi=${kpi} row=${row}）`);
    }, '?p=all&pj=proj-sesonly-multi&m=dev:Anthropic');
  });

  test('项目行可展开出该项目的会话明细（与「会话」列同口径）', async () => {
    await withPage(async (page) => {
      // 默认收起
      assert.equal(await page.count('#projTable tbody tr.proj-expand'), 0,
        '项目行默认不应有展开区');

      const row = await page.eval(`
        const tr = [...document.querySelectorAll('#projTable tbody tr')]
          .find(r => r.dataset.proj === 'Projects/proj-a');
        return tr ? { sess: tr.children[4].textContent.trim(),
                      tot: tr.children[5].textContent.trim() } : null;
      `);
      assert.ok(row, '应有 proj-a 这一行');

      await page.click('#projTable tbody tr[data-proj="Projects/proj-a"]');
      assert.equal(await page.count('#projTable tbody tr.proj-expand'), 1,
        '点击后应出现该项目的展开区');
      const sessRows = await page.count('#projTable tbody tr.proj-expand .sess-row');
      assert.equal(String(sessRows), row.sess,
        `展开区的会话数(${sessRows}) 应等于该行「会话」列(${row.sess})`);

      // 再点一次收起
      await page.click('#projTable tbody tr[data-proj="Projects/proj-a"]');
      assert.equal(await page.count('#projTable tbody tr.proj-expand'), 0, '再点应收起');
    }, '?p=all');
  });

  test('独立的会话明细卡片已并入项目分布', async () => {
    await withPage(async (page) => {
      assert.equal(await page.count('#sessionTable'), 0,
        '不应再有独立的会话明细表');
      assert.equal(await page.eval(
        "return document.body.innerText.includes('会话明细')"), false,
        '不应残留「会话明细」标题');
    }, '?p=all');
  });

  test('同一项目的不同写法合并成一行（跨来源命名差异）', async () => {
    // 真实案例：Desktop/trading-logic（dimcode/devin）、~/desktop-trading-logic
    // （commandcode）、trading-logic（antigravity）本是同一个项目。
    // fixture 里 proj-a 与 ~/proj-a 同理，必须合并而不是各占一行。
    await withPage(async (page) => {
      const rows = await page.eval(`
        return [...document.querySelectorAll('#projTable tbody tr.pj-row')]
          .map(r => ({ proj: r.dataset.proj, sess: r.children[4].textContent.trim() }))
          .filter(r => /proj-a$/i.test(r.proj));
      `);
      assert.equal(rows.length, 1,
        `同一项目应只占一行，实际 ${JSON.stringify(rows)}`);
      // 代表名取"像路径"的写法，而不是扁平化/只剩末段的名
      assert.equal(rows[0].proj, 'Projects/proj-a',
        `代表名应为 Projects/proj-a，实际 ${rows[0].proj}`);
      // proj-a 自身 2 条（s-multi / s-today）+ Projects/proj-a 1 条
      assert.equal(rows[0].sess, '3',
        `会话数应为合并后的 3，实际 ${rows[0].sess}`);

      // 展开后，两种写法的会话都该在里面
      await page.click(`#projTable tbody tr[data-proj="${rows[0].proj}"]`);
      const ids = await page.eval(`
        return [...document.querySelectorAll('#projTable tbody tr.proj-expand .sess-id')]
          .map(e => e.getAttribute('title'));
      `);
      assert.equal(ids.length, 3, `展开区应有 3 条会话，实际 ${ids.length}`);
      assert.ok(ids.includes('s-proj-a-alt'),
        `另一种写法的会话也应在展开区里：${JSON.stringify(ids)}`);
    }, '?p=all');
  });

  test('事件型来源进入活动量口径', async () => {
    await withPage(async (page) => {
      assert.ok((await page.text('#kpiTotalTitle')).includes('活动量'), '标题应切到活动量');
      assert.equal(await page.eval("return document.getElementById('barModeTabs').style.display"),
        'none');
    }, '?a=evonly&p=all');
  });
});

// ------------------------------------------------------------- 边界

describe('边界与健壮性', () => {
  test('URL 日期越界不会产生倒序范围（M5）', async () => {
    await withPage(async (page) => {
      const label = await page.text('#rangeLabel');
      const m = label.match(/(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})/);
      assert.ok(m, `范围标签应含两个日期，实际: ${label}`);
      // 回归点：钳制后曾出现 start > end，并渲染出「活跃天数 0/-2279 天」
      assert.ok(m[1] <= m[2], `范围应有序，实际: ${m[1]} ~ ${m[2]}`);
      const body = await page.bodyText();
      assert.equal(body.includes('NaN'), false);
      assert.equal(body.includes('/-'), false, '不应出现负天数');
    }, '?s=2020-01-01&e=2020-01-31');
  });

  test('模型参数命中原型链时回落为全部模型（L1）', async () => {
    await withPage(async (page) => {
      // 回归点：`selModel in mAll` 会命中 Object.prototype，?m=constructor 曾整盘空白
      assert.ok((await page.text('#modelSelTxt')).includes('全部模型'));
      assert.ok(num(await page.text('#kpiTotalVal')) > 0, 'KPI 不应为 0');
    }, '?m=constructor&p=all');
  });

  test('各入口都不渲染 NaN / undefined / Infinity', async () => {
    for (const q of ['?p=all', '?p=today', '?a=evonly&p=all', '?m=dev:OpenAI&p=all',
      '?pj=proj-session-only&p=all']) {
      await withPage(async (page) => {
        const body = await page.bodyText();
        for (const bad of ['NaN', 'undefined', 'Infinity']) {
          assert.equal(body.includes(bad), false, `${q} 渲染出了 ${bad}`);
        }
      }, q);
    }
  });

  test('刷新按钮打 /refresh 后重新加载', async () => {
    await withPage(async (page) => {
      await page.click('#refreshBtn');
      // 服务端的 /refresh 被 stub 成 ok:true，页面应自行重载
      await page.goto(`${BASE}/index.html?p=all`);
      assert.ok(await page.count('#agentTable tbody tr') > 0);
    }, '?p=all');
  });
});

// --------------------------------------------- 时区（L6：UTC 以西）

describe('UTC 以西时区不产生日期偏移', () => {
  test('每日柱状图的日期与所选范围严格一致', async () => {
    const page = await openPage();
    try {
      await page.goto(`${BASE}/index.html?p=all`, { timezone: 'America/Los_Angeles' });
      const label = await page.text('#rangeLabel');
      const m = label.match(/(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})/);
      assert.ok(m, `范围标签应含两个日期，实际: ${label}`);
      const keys = await page.eval(`
        return [...new Set([...document.querySelectorAll('#bars rect.bar')].map((e) => e.dataset.d))];
      `);
      // 回归点：new Date('YYYY-MM-DD') 按 UTC 解析、iso() 按本地取值，
      // 在 UTC 以西时区会让整条日期轴早一天、并丢掉范围最后一天
      assert.equal(keys[0], m[1], '柱图首日应等于范围起始日');
      assert.equal(keys[keys.length - 1], m[2], '柱图末日应等于范围结束日');
    } finally {
      await page.close();
    }
  });
});
