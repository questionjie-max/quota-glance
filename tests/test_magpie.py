"""Magpie 本地存储的解析：字段名/时间格式/单位换算都容易出错，这里钉住。"""

import json
import os
import shutil
import tempfile
import unittest

from quota_glance import magpie

QUOTAS = {
    "codex/a@b.c": {
        "at": "2026-10-30T16:05:35+08:00",
        "quota": {
            "provider": "codex", "name": "Codex", "plan": "free",
            "windows": [{"name": "30 days", "used": 50,
                         "resetsAt": "2026-11-24T23:13:29+08:00"}],
        },
    },
    "opencode-go/plan x": {
        "at": "2026-10-30T16:05:30+08:00",
        "quota": {
            "provider": "opencode-go", "name": "OpenCode Go",
            "windows": [
                {"name": "5 hours", "used": 1, "resetsAt": "2026-10-30T11:39:53Z"},
                {"name": "7 days", "used": 11, "resetsAt": "2026-11-02T00:00:00Z"},
                {"name": "Month", "used": 29, "resetsAt": "2026-11-16T09:04:15Z"},
            ],
        },
    },
}

CREDITS = {
    "workbuddy|someone|Credits": {
        "since": "2026-10-19",
        "last": {"used": 1491.73, "total": 6900,
                 "at": "2026-10-30T16:05:36+08:00"},
        "days": {"2026-10-29": 11.45, "2026-10-30": 340.33},
    }
}


class MagpieTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["QUOTA_GLANCE_MAGPIE_DIR"] = self.dir
        with open(os.path.join(self.dir, "quotas.json"), "w", encoding="utf-8") as f:
            json.dump(QUOTAS, f)
        with open(os.path.join(self.dir, "credits-daily.json"), "w",
                  encoding="utf-8") as f:
            json.dump(CREDITS, f)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("QUOTA_GLANCE_MAGPIE_DIR", None)

    def test_windows_normalised(self):
        q = magpie.quotas()
        self.assertEqual(sorted(q), ["codex", "opencode-go"])
        codex = q["codex"]["windows"]
        self.assertEqual(len(codex), 1)
        self.assertEqual(codex[0]["label"], "月")          # 30 days → 月
        self.assertAlmostEqual(codex[0]["used"], 0.50)     # 50 → 0.5
        self.assertIsNotNone(codex[0]["resets_at"])

    def test_iso_with_z_is_parsed(self):
        w = magpie.quotas()["opencode-go"]["windows"]
        self.assertEqual([x["label"] for x in w], ["5h", "周", "月"])
        for x in w:
            self.assertIsNotNone(x["resets_at"], "带 Z 的时间没解析出来")

    def test_credits_and_today(self):
        c = magpie.credits()["workbuddy"]
        self.assertEqual(c["total"], 6900)
        self.assertAlmostEqual(c["used"], 1491.73)
        self.assertIn("today", c)

    def test_missing_dir_degrades(self):
        os.environ["QUOTA_GLANCE_MAGPIE_DIR"] = "/nonexistent/x"
        self.assertEqual(magpie.quotas(), {})
        self.assertEqual(magpie.credits(), {})


if __name__ == "__main__":
    unittest.main()
