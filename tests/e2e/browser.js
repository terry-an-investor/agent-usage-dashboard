'use strict';
// e2e 支撑（零依赖）：起 fixture 服务器 + 用**系统 Chrome** 的 CDP 驱动页面。
//
// 为什么不用 Playwright / Puppeteer：
//   这些断言只需要三个 CDP 能力 —— Page.navigate、Runtime.evaluate、
//   Emulation.setTimezoneOverride —— 而 Node 24 自带 node:test 与全局 WebSocket，
//   手写封装百来行即可等价；既不用往 node_modules 塞测试框架，也不必下载浏览器
//   （Playwright 在本机要占 1.1G 缓存，新机器还需再下 ~150MB）。
//
// 环境变量：CHROME_PATH 指定浏览器；E2E_PORT / E2E_CDP_PORT 换端口。
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.E2E_PORT || 8951);
const CDP_PORT = Number(process.env.E2E_CDP_PORT || 9333);
const BASE = `http://127.0.0.1:${PORT}`;

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);

function findChrome() {
  for (const p of CHROME_CANDIDATES) {
    if (fs.existsSync(p)) return p;
  }
  throw new Error('找不到 Chrome/Chromium，请用 CHROME_PATH 指定可执行文件');
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, { timeout = 15000, interval = 100, what = '条件' } = {}) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (e) {
      last = e;
    }
    await sleep(interval);
  }
  throw new Error(`等待超时：${what}${last ? `（${last.message}）` : ''}`);
}

class Page {
  constructor(ws, targetId) {
    this.ws = ws;
    this.targetId = targetId;
    this.seq = 0;
    this.pending = new Map();
    ws.addEventListener('message', (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result);
      }
    });
  }

  send(method, params = {}) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 20000);
    });
  }

  /** 打开页面并等到首屏渲染完成（index.html 的脚本同步执行） */
  async goto(url, { timezone } = {}) {
    if (timezone) await this.send('Emulation.setTimezoneOverride', { timezoneId: timezone });
    await this.send('Page.enable');
    await this.send('Page.navigate', { url });
    await waitFor(async () => (await this.eval('return document.readyState')) === 'complete',
      { what: '页面 load' });
    await waitFor(async () => (await this.eval(
      'return !!document.querySelector("#agentTable tbody tr")')) === true,
    { what: '首屏表格渲染' });
  }

  /** 在页面里求值（表达式里用 return 返回结果，支持 await） */
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', {
      expression: `(async () => { ${expression} })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      throw new Error('页面内异常: ' + (d.exception?.description || d.text));
    }
    return r.result?.value;
  }

  text(sel) {
    return this.eval(`const el = document.querySelector(${JSON.stringify(sel)});
      return el ? el.textContent.trim() : null;`);
  }

  count(sel) {
    return this.eval(`return document.querySelectorAll(${JSON.stringify(sel)}).length;`);
  }

  click(sel) {
    return this.eval(`const el = document.querySelector(${JSON.stringify(sel)});
      if (!el) throw new Error('找不到元素: ' + ${JSON.stringify(sel)});
      el.click(); return true;`);
  }

  /** 选中 <select>，派发 change（等价于用户选择） */
  select(sel, value) {
    return this.eval(`const el = document.querySelector(${JSON.stringify(sel)});
      if (!el) throw new Error('找不到 select: ' + ${JSON.stringify(sel)});
      el.value = ${JSON.stringify(value)};
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return el.value;`);
  }

  bodyText() {
    return this.eval('return document.body.innerText;');
  }

  async close() {
    try {
      await fetch(`http://127.0.0.1:${CDP_PORT}/json/close/${this.targetId}`);
    } catch { /* 忽略 */ }
    try {
      this.ws.close();
    } catch { /* 忽略 */ }
  }
}

let chromeProc = null;
let serverProc = null;
let chromeProfile = null;

async function startAll() {
  serverProc = spawn('python3', [path.join(__dirname, 'server.py'), String(PORT)],
    { stdio: 'ignore' });
  await waitFor(async () => (await fetch(`${BASE}/index.html`)).ok,
    { what: 'fixture 服务器就绪' });

  chromeProfile = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-e2e-'));
  chromeProc = spawn(findChrome(), [
    '--headless=new',
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${chromeProfile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--disable-extensions', '--disable-background-networking',
    '--disable-component-update', '--disable-sync', '--mute-audio',
    'about:blank',
  ], { stdio: 'ignore' });
  await waitFor(async () => (await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)).ok,
    { what: 'Chrome CDP 就绪' });
}

async function stopAll() {
  if (chromeProc) {
    chromeProc.kill('SIGKILL');
    await sleep(300);        // 等 Chrome 真正退出，否则 profile 目录还在被写
  }
  if (serverProc) serverProc.kill('SIGKILL');
  if (chromeProfile) {
    // 清理失败不应让整个测试套件失败（只是临时目录）
    try {
      fs.rmSync(chromeProfile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    } catch { /* 忽略 */ }
  }
  chromeProc = serverProc = chromeProfile = null;
}

/** 开一个新标签页（每个用例独立，避免相互污染） */
async function openPage(url = 'about:blank') {
  const r = await fetch(
    `http://127.0.0.1:${CDP_PORT}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  const target = await r.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', () => reject(new Error('WebSocket 连接失败')), { once: true });
  });
  return new Page(ws, target.id);
}

module.exports = { BASE, PORT, startAll, stopAll, openPage, waitFor, sleep };
