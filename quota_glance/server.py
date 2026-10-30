#!/usr/bin/env python3
"""本地服务：采集 → 渲染 PNG → 同时吐一个 ES5 单页给 Kindle 浏览器。

为什么是"图片"而不是"网页排版"：Kindle 10 代的浏览器是古董 WebKit（ES5、无 fetch、
无 CSS grid、字体渲染糙）。把它当哑屏只喂一张 PNG，布局和断行全在服务端定死，
Kindle 端只做一件事：图变了才换 src。这样墨水屏就不会因为重排而闪烁。

防闪三件套：
1. 服务端算 fingerprint，数据没变就继续用旧 PNG（字节都不变）；
2. 页面每 20s 只问一个几十字节的 /api/state，hash 没变就完全不碰 DOM；
3. 图变了才换 <img src>，一次重绘。

用法：
    python3 server.py --demo          # 假数据，先看效果
    python3 server.py                 # 真实采集，读 config.json
    python3 server.py --port 8787 --bind 0.0.0.0
"""

import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import providers
from . import render

DEMO = False          # 由 cli 设置：True 时用假数据，方便先看效果

STATE = {
    "png": None,
    "hash": "",
    "updated": 0.0,        # 数据最后一次变化的时间
    "checked": 0.0,        # 最后一次成功采集的时间
    "mode": "demo",
}
LOCK = threading.Lock()


def fingerprint(rows):
    """只看"会画到屏幕上"的东西，并量化到墨水屏有意义的精度。
    62.3% → 62.4% 不该触发重绘。"""
    slim = []
    for r in rows:
        slim.append({
            "id": r.get("id"),
            "st": r.get("status"),
            "hd": str(r.get("headline") or ""),
            "pl": (r.get("plan") or "")[:24],
            "rs": (r.get("reason") or "")[:60],
            "mn": (r.get("model_note") or "")[:24],
            "m": [[m.get("label"),
                   round(float(m.get("used") or 0), 2),
                   str(m.get("value") or ""),
                   int((m.get("resets_at") or 0) // 60)]
                  for m in (r.get("metrics") or [])],
            "d": [[m.get("name"), str(m.get("value") or "")]
                  for m in (r.get("models") or [])],
        })
    return hashlib.sha1(json.dumps(slim, sort_keys=True,
                                   ensure_ascii=False).encode()).hexdigest()[:16]


def refresh(force=False, use_demo=False):
    rows, mode = providers.collect_all(use_demo=use_demo)
    fp = fingerprint(rows)
    with LOCK:
        STATE["checked"] = time.time()
        STATE["mode"] = mode
        if fp == STATE["hash"] and STATE["png"] and not force:
            return False
        note = "数据更新 " + time.strftime("%H:%M", time.localtime())
        img = render.render(rows, note=note)
        STATE["png"] = render.to_png(img)
        STATE["hash"] = fp
        STATE["updated"] = time.time()
    return True


def updater(interval, use_demo):
    while True:
        time.sleep(interval)
        try:
            changed = refresh(use_demo=use_demo)
            if changed:
                print("[%s] 数据变化，已重绘" % time.strftime("%H:%M:%S"),
                      flush=True)
        except Exception as e:                      # noqa: BLE001
            print("[%s] 刷新失败: %s" % (time.strftime("%H:%M:%S"), e),
                  flush=True)


PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=600, initial-scale=1">
<title>Token</title>
<style>
html,body{margin:0;padding:0;background:#fff;overflow:hidden;}
#wrap{width:100%;}
#d{display:block;border:0;margin:0 auto;}
</style>
</head><body>
<div id="wrap"><img id="d" src="/dash.png" alt="dashboard"></div>
<script>
(function(){
  var cur = null, fails = 0, timer = null;

  var PNG_W = 600, PNG_H = 740;

  // Kindle 浏览器工具栏会吃掉几十像素，按可视高度等比缩放，
  // 保证整张图完整可见（不裁切、不出现滚动条）。
  function fit(){
    var img = document.getElementById('d');
    if (!img) return;
    var vw = window.innerWidth || PNG_W;
    var vh = window.innerHeight || PNG_H;
    var s = Math.min(vw / PNG_W, vh / PNG_H, 1);
    img.style.width = Math.round(PNG_W * s) + 'px';
    img.style.height = Math.round(PNG_H * s) + 'px';
  }
  window.onorientationchange = function(){ setTimeout(fit, 300); };

  function onState(st){
    fails = 0;
    if (st.hash !== cur){
      cur = st.hash;
      var img = document.getElementById('d');
      var pre = new Image();
      pre.onload = function(){ img.src = '/dash.png?v=' + st.hash; fit(); };
      pre.src = '/dash.png?v=' + st.hash;
    }
  }

  function poll(){
    var x = new XMLHttpRequest();
    x.open('GET', '/api/state?t=' + (new Date().getTime()), true);
    x.timeout = 15000;
    x.onreadystatechange = function(){
      if (x.readyState !== 4) return;
      if (x.status === 200){
        try { onState(JSON.parse(x.responseText)); }
        catch(e){ fails++; }
      } else {
        fails++;
      }
      clearTimeout(timer);
      // 断线时退避，别把 Wi-Fi 和 CPU 拖死
      timer = setTimeout(poll, fails ? Math.min(120000, 20000 * fails) : 20000);
    };
    x.ontimeout = function(){ fails++; };
    x.send();
  }

  // 老 WebKit 的堆会慢慢涨，2 小时整页重载一次（只在断线/空闲时才做，避免无谓重绘）
  setTimeout(function(){ location.reload(); }, 7200000);

  fit();
  // 老 WebKit 报的 innerHeight 有时不含滚动条，等布局稳定再量一次
  setTimeout(fit, 500);
  poll();
})();
</script>
</body></html>
"""
PAGE = PAGE.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", PAGE)

        if path == "/api/state":
            with LOCK:
                h, upd, chk = STATE["hash"], STATE["updated"], STATE["checked"]
            if not h and not STATE["png"]:
                try:
                    refresh(use_demo=DEMO)
                except Exception:                   # noqa: BLE001
                    pass
                with LOCK:
                    h, upd, chk = STATE["hash"], STATE["updated"], STATE["checked"]
            body = json.dumps({
                "hash": h,
                "updated": int(upd),
                "checked": int(chk),
                "age": int(time.time() - upd) if upd else None,
            }).encode()
            return self._send(200, "application/json", body)

        if path in ("/dash.png", "/dash"):
            if "fresh" in q:
                try:
                    refresh(force=True, use_demo=DEMO)
                except Exception:                   # noqa: BLE001
                    pass
            with LOCK:
                png, h = STATE["png"], STATE["hash"]
            if not png:
                try:
                    refresh(use_demo=DEMO)
                except Exception:                   # noqa: BLE001
                    pass
                with LOCK:
                    png, h = STATE["png"], STATE["hash"]
            if not png:
                return self._send(503, "text/plain; charset=utf-8",
                                  "还没渲染好，稍等".encode())
            etag = '"%s"' % h
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, "image/png", png, {"ETag": etag})

        if path == "/refresh":
            try:
                refresh(force=True, use_demo=DEMO)
            except Exception as e:                  # noqa: BLE001
                return self._send(500, "text/plain; charset=utf-8",
                                  ("刷新失败: %s" % e).encode())
            return self._send(200, "text/plain; charset=utf-8", b"ok")

        return self._send(404, "text/plain; charset=utf-8", b"not found")


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:                               # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def serve(port=8787, bind="0.0.0.0", interval=120, demo=False):
    global DEMO
    DEMO = demo
    try:
        refresh(use_demo=demo)
        print("首次渲染完成", flush=True)
    except Exception as e:                          # noqa: BLE001
        print("首次渲染失败（先起服务，页面会稍后重试）：%s" % e, flush=True)

    threading.Thread(target=updater, args=(interval, demo),
                     daemon=True).start()

    srv = ThreadingHTTPServer((bind, port), Handler)
    ip = lan_ip()
    print("", flush=True)
    print("  本机   http://127.0.0.1:%d" % port, flush=True)
    print("  局域网 http://%s:%d      <- Kindle 浏览器里输这个" % (ip, port), flush=True)
    print("  模式   %s   采集间隔 %ds" % ("DEMO(假数据)" if demo else "真实",
                                          interval), flush=True)
    print("", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("bye", flush=True)
