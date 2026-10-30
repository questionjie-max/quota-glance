#!/usr/bin/env python3
"""把额度 + 模型消耗渲染成 Kindle 能 1:1 显示的灰度 PNG。

版式（每张卡左右分栏）：
    左栏 = 额度（5h / 周 / 月，带进度条 + 时间进度尺）
    右栏 = 模型消耗（哪个模型、消耗多少）
右上角是该家最要紧的那个数字（额度百分比 / 积分余额）。

Kindle 侧约束：
- 600×800 屏，浏览器工具条约 60px → 画 600×740
- 只输出 17 的倍数灰度，1:1 映射 4-bit framebuffer，不抖动
- 2× 超采样后缩小，PIL 自身不做抗锯齿
"""

import io
import os
import time

from PIL import Image, ImageDraw, ImageFont

SS = 2
W, H = 600, 740
M = 22
DIV = 344                      # 左右分栏竖线

G0, G6, G8, G9 = 0, 102, 136, 153

_CJK = ["/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]
_fonts = {}


def font(size):
    if size in _fonts:
        return _fonts[size]
    path = next((p for p in _CJK if os.path.exists(p)), None)
    f = ImageFont.truetype(path, int(size * SS)) if path else ImageFont.load_default()
    _fonts[size] = f
    return f


def txt(d, xy, s, size, fill=G0, bold=False, anchor="la"):
    d.text((xy[0] * SS, xy[1] * SS), s, font=font(size), fill=fill, anchor=anchor,
           stroke_width=(1 * SS if bold else 0), stroke_fill=fill)


def rule(d, x0, y, x1, fill=G9, h=1):
    d.rectangle([x0 * SS, y * SS, x1 * SS, (y + h) * SS - 1], fill=fill)


def fmt_reset(epoch):
    if not epoch:
        return ""
    sec = int(epoch - time.time())
    if sec <= 0:
        return "已重置"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return "%d天%d时" % (d, h)
    if h:
        return "%d时%d分" % (h, m)
    return "%d分" % m


def elapsed_ratio(w):
    rt, secs = w.get("resets_at"), w.get("seconds")
    if not rt or not secs:
        return None
    el = 1.0 - (rt - time.time()) / float(secs)
    return max(0.0, min(1.0, el))


def draw_left_row(d, y0, ry, m):
    label = str(m.get("label", ""))[:4]
    txt(d, (34, ry + 4), label, 12, G6)
    if "used" in m:
        bx0, bx1 = 78, 272
        by0, by1 = ry + 8, ry + 17
        d.rectangle([bx0 * SS, by0 * SS, bx1 * SS, by1 * SS], outline=G0, width=1 * SS)
        iw = (bx1 - bx0) - 4
        used = max(0.0, min(1.0, float(m["used"])))
        fx = int(bx0 + 2 + iw * used)
        if fx > bx0 + 2:
            d.rectangle([(bx0 + 2) * SS, (by0 + 2) * SS, fx * SS, (by1 - 2) * SS], fill=G0)
        el = elapsed_ratio(m)
        if el is not None:
            mx = int(bx0 + 2 + iw * el)
            col = 255 if mx <= fx else G6
            d.rectangle([mx * SS, (by0 + 2) * SS, (mx + 1) * SS, (by1 - 2) * SS], fill=col)
        txt(d, (334, ry + 2), "%d%%" % round(used * 100), 15, G0, bold=True, anchor="ra")
    else:
        txt(d, (334, ry + 2), str(m.get("value", "-"))[:12], 12, G0, bold=True, anchor="ra")


def draw_right_row(d, ry, m):
    name = str(m.get("name", ""))[:26]
    txt(d, (356, ry + 4), name, 11, G0)
    txt(d, (566, ry + 2), str(m.get("value", "")), 13, G0, bold=True, anchor="ra")


def draw_card(d, y0, y1, row):
    x0, x1 = M, W - M
    d.rectangle([x0 * SS, y0 * SS, x1 * SS, y1 * SS], outline=G0, width=2 * SS)

    txt(d, (x0 + 12, y0 + 9), row.get("name", "")[:16], 21, G0, bold=True)
    head = str(row.get("headline") or "--")
    txt(d, (x1 - 12, y0 + 12), head[:13], 19, G0, bold=True, anchor="ra")
    sub = str(row.get("plan") or "")
    ms = row.get("metrics") or []
    if ms and ms[0].get("resets_at"):
        rt = fmt_reset(ms[0]["resets_at"])
        if rt:
            sub = (sub + "   ·   " if sub else "") + "%s 重置 %s" % (ms[0].get("label", ""), rt)
    txt(d, (x0 + 12, y0 + 36), sub[:42], 11, G6)

    rule(d, x0 + 12, y0 + 53, x1 - 12, G9, 1)
    d.rectangle([DIV * SS, (y0 + 6) * SS, DIV * SS + 1, (y1 - 6) * SS], fill=G9)
    txt(d, (x0 + 12, y0 + 58), "额度", 10, G9)
    txt(d, (DIV + 12, y0 + 58), str(row.get("models_label") or "模型消耗"), 10, G9)

    metrics = (row.get("metrics") or [])[:3]
    models = (row.get("models") or [])[:3]
    for i in range(3):
        ry = y0 + 76 + i * 24
        if i < len(metrics):
            draw_left_row(d, y0, ry, metrics[i])
        if i < len(models):
            draw_right_row(d, ry, models[i])
    if not models:
        txt(d, (DIV + 12, y0 + 76), row.get("model_note") or "暂无数据", 11, G9)


def render(rows, height=H, note=""):
    img = Image.new("L", (W * SS, height * SS), 255)
    d = ImageDraw.Draw(img)

    txt(d, (M, 11), "TOKEN 监控", 24, G0, bold=True)
    txt(d, (W - M, 17), time.strftime("%Y-%m-%d"), 15, G6, anchor="ra")
    rule(d, M, 48, W - M, G0, 2)

    top, footer, gap = 60, 34, 8
    avail = height - top - footer
    n = max(1, len(rows))
    ch = (avail - gap * (n - 1)) // n
    for i, row in enumerate(rows):
        y0 = top + i * (ch + gap)
        draw_card(d, y0, y0 + ch, row)

    txt(d, (M, height - 24), note or ("更新 " + time.strftime("%H:%M")), 11, G9)
    txt(d, (W - M, height - 24), "kindle-token-dash", 11, G9, anchor="ra")

    img = img.resize((W, height), Image.LANCZOS)
    img = img.point(lambda v: min(255, int(round(v / 17.0)) * 17))
    return img


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
