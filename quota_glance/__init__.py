"""quota-glance —— 把 AI 订阅额度做成一块能"一眼看"的屏。

两层设计：
  exporters 层：magpie.py 读本地已归一化的额度存储 → 规范 JSON
  render 层   ：providers.py 补上按模型明细 → render.py 出 1:1 灰度 PNG
                server.py 把 PNG 喂给 Kindle 浏览器（304 防闪）

单独用 exporter 也可以：
    python -m quota_glance json
"""

__version__ = "0.1.0"
