"""渲染的两条硬约束：灰度必须踩 17 的倍数；数据没变就不能重绘。"""

import unittest

from quota_glance import providers as P
from quota_glance import render, server


class RenderTest(unittest.TestCase):
    def test_size_and_mode(self):
        rows, _ = P.collect_all(use_demo=True)
        img = render.render(rows)
        self.assertEqual(img.size, (600, 740))
        self.assertEqual(img.mode, "L")

    def test_only_16_greys(self):
        rows, _ = P.collect_all(use_demo=True)
        img = render.render(rows)
        bad = [v for v in img.tobytes() if v % 17 != 0]
        self.assertEqual(bad, [], "出现了非 17 倍数的灰度，会触发墨水屏抖动")

    def test_fingerprint_stable(self):
        rows, _ = P.collect_all(use_demo=True)
        self.assertEqual(server.fingerprint(rows), server.fingerprint(rows))

    def test_fingerprint_changes_on_usage(self):
        rows, _ = P.collect_all(use_demo=True)
        a = server.fingerprint(rows)
        rows[0]["metrics"][0]["used"] = 0.99
        self.assertNotEqual(a, server.fingerprint(rows))

    def test_fingerprint_ignores_subpercent(self):
        rows, _ = P.collect_all(use_demo=True)
        a = server.fingerprint(rows)
        rows[0]["metrics"][0]["used"] = 0.381        # 量化到 1%，不该变
        self.assertEqual(a, server.fingerprint(rows))


if __name__ == "__main__":
    unittest.main()
