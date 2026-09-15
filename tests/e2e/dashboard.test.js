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

  test('会话明细跟随时间范围（M2）', async () => {
    const rowCount = (page) => page.eval(`
      return [...document.querySelectorAll('#sessionTable tbody tr')]
        .filter((tr) => !tr.textContent.includes('未找到')).length;
    `);
    const allCount = await withPage(rowCount, '?p=all');
    const todayCount = await withPage(async (page) => {
      // 回归点：会话面板曾完全忽略时间范围，恒为全部条数
      assert.equal(await page.eval("return document.body.innerText.includes('{n}')"), false,
        '展开按钮不应残留 {n} 模板');
      return rowCount(page);
    }, '?p=today');
    assert.ok(allCount > todayCount, `全时段会话数(${allCount}) 应多于今日(${todayCount})`);
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
