#!/usr/bin/env python3
"""Kimi Code 用量仪表盘本地服务：托管页面 + /refresh 接口（调用 refresh.sh 重建 data.js）"""
import json
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIR = Path(__file__).resolve().parent
PORT = 8931
_lock = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DIR), **kw)

    def end_headers(self):
        # data.js / 页面禁止缓存，保证刷新后拿到新数据和新页面（self.path 含查询串，先去掉）
        p = self.path.split("?", 1)[0]
        if p == "/data.js" or p.endswith(".html") or p == "/":
            self.send_header("Cache-Control", "no-store")
        # 防止被其它网站 iframe 嵌入（点击劫持）；资源只允许同源 + 内联（本页面全是内联 CSS/JS）
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
        )
        super().end_headers()

    def do_POST(self):
        if self.path != "/refresh":
            self.send_error(404)
            return
        # CSRF 防护：浏览器跨站 POST 必带 Origin，只放行本页面同源请求和本地无 Origin 的调用
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self.send_error(403)
            return
        with _lock:  # 防止并发重复跑
            try:
                r = subprocess.run(
                    ["bash", str(DIR / "refresh.sh")],
                    capture_output=True, text=True, timeout=300, cwd=str(DIR),
                )
                ok = r.returncode == 0
                body = {"ok": ok, "log": (r.stdout + r.stderr).strip()[-2000:]}
            except Exception as e:
                body = {"ok": False, "log": str(e)}
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # 静默访问日志


if __name__ == "__main__":
    print(f"仪表盘: http://127.0.0.1:{PORT}/index.html  (Ctrl+C 停止)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
