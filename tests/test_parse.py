"""宽容解析：这些接口都没文档、字段名会漂，解析器必须能扛住常见写法。"""

import unittest

from quota_glance import providers as P


class ParseTest(unittest.TestCase):
    def test_codex_like_payload(self):
        obj = {"rate_limit": {"primary_window": {
            "used_percent": 47, "limit_window_seconds": 2592000,
            "reset_after_seconds": 2199829, "reset_at": 1795533209}}}
        w = P._polish(P._windows_from(obj))
        self.assertEqual(len(w), 1)
        self.assertAlmostEqual(w[0]["used"], 0.47)
        self.assertEqual(w[0]["seconds"], 2592000)
        self.assertIsNotNone(w[0]["resets_at"])

    def test_limit_minus_remaining(self):
        obj = {"total": 1000, "remaining": 750}
        w = P._windows_from(obj)
        self.assertAlmostEqual(w[0]["used"], 0.25)

    def test_underivable_label_kept(self):
        obj = {"foo_window": {"used_percent": 10}}
        w = P._polish(P._windows_from(obj))
        self.assertTrue(w and w[0]["label"])

    def test_seconds_to_label(self):
        self.assertEqual(P.label_from_seconds(18000), "5h")
        self.assertEqual(P.label_from_seconds(604800), "周")
        self.assertEqual(P.label_from_seconds(2592000), "月")
        self.assertIsNone(P.label_from_seconds(None))

    def test_number_format(self):
        self.assertEqual(P.fmt_num(136036929), "136.0M")
        self.assertEqual(P.fmt_num(229528047), "229.5M")
        self.assertEqual(P.fmt_comma(6900), "6,900")

    def test_demo_has_all_fields(self):
        rows, mode = P.collect_all(use_demo=True)
        self.assertEqual(mode, "demo")
        self.assertEqual(len(rows), 4)
        for r in rows:
            self.assertIn("headline", r)
            self.assertIn("metrics", r)
            self.assertIn("models", r)


if __name__ == "__main__":
    unittest.main()
