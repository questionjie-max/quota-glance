"""命令行入口。

    python -m quota_glance json      # 只输出规范 JSON（可喂给任何东西）
    python -m quota_glance render    # 出一张 PNG
    python -m quota_glance serve     # 起 HTTP 服务，给 Kindle 浏览器看
"""

import argparse
import json
import sys
import time

from . import providers, render


def cmd_json(a):
    rows, mode = providers.collect_all(use_demo=a.demo)
    out = {
        "schema": "quota-glance/1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": mode,
        "providers": rows,
    }
    s = json.dumps(out, ensure_ascii=False, indent=2)
    if a.output and a.output != "-":
        with open(a.output, "w", encoding="utf-8") as f:
            f.write(s + "\n")
        print("已写入 %s（%d 个 provider）" % (a.output, len(rows)),
              file=sys.stderr)
    else:
        print(s)


def cmd_render(a):
    rows, mode = providers.collect_all(use_demo=a.demo)
    note = "数据更新 " + time.strftime("%H:%M") + ("  · DEMO" if a.demo else "")
    img = render.render(rows, height=a.height, note=note)
    with open(a.output, "wb") as f:
        f.write(render.to_png(img))
    print("已生成 %s（%dx%d）" % (a.output, img.size[0], img.size[1]))


def cmd_serve(a):
    from . import server
    server.serve(port=a.port, bind=a.bind, interval=a.interval, demo=a.demo)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="quota-glance",
        description="把 AI 订阅额度做成一块能一眼看的屏（Kindle / 任何浏览器）")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--demo", action="store_true", help="用假数据，不读真实凭据")

    p = sub.add_parser("json", help="输出规范 JSON（exporter 层的产物）")
    common(p)
    p.add_argument("-o", "--output", default="-", help="输出文件，默认 stdout")
    p.set_defaults(fn=cmd_json)

    p = sub.add_parser("render", help="渲染一张灰度 PNG")
    common(p)
    p.add_argument("-o", "--output", default="dashboard.png")
    p.add_argument("--height", type=int, default=render.H,
                   help="画布高度，默认 %d（Kindle 浏览器工具栏约占 60px）" % render.H)
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("serve", help="起 HTTP 服务")
    common(p)
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--interval", type=int, default=180, help="采集间隔（秒）")
    p.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 1
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
