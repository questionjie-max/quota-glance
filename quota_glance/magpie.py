#!/usr/bin/env python3
"""读 Magpie（com.yetone.magpie）本地已经归一化好的额度数据。

Magpie 会把各家的额度查询结果落到 ~/.config/magpie/：
  quotas.json        每个账号的 windows（used / resetsAt）+ balance
  credits-daily.json 积分类账号的 total / used / 逐日消耗
所以只要 Magpie 在跑并且配过那家，我们就不用自己拿 cookie / key。
"""

import datetime
import json
import os

def conf_dir():
    """Magpie 的本地数据目录，可用 QUOTA_GLANCE_MAGPIE_DIR 覆盖（测试也走这里）。"""
    return (os.environ.get("QUOTA_GLANCE_MAGPIE_DIR")
            or os.path.expanduser("~/.config/magpie"))

LAB = {"5 hours": "5h", "7 days": "周", "30 days": "月", "Month": "月",
       "Credits": "积分", "1 day": "日", "24 hours": "日", "daily": "日"}


def _iso(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _read(name):
    try:
        with open(os.path.join(conf_dir(), name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def quotas():
    """{provider: {"windows":[{label,used,resets_at}], "plan", "balance", "name"}}"""
    out = {}
    for _key, v in (_read("quotas.json") or {}).items():
        qu = v.get("quota") or {}
        pid = (qu.get("provider") or "").split("/")[0]
        if not pid:
            continue
        wins = []
        for w in qu.get("windows") or []:
            used = w.get("used")
            if used is None:
                continue
            wins.append({"label": LAB.get(w.get("name"), w.get("name") or "用量"),
                         "used": max(0.0, min(1.0, float(used) / 100.0)),
                         "resets_at": _iso(w.get("resetsAt"))})
        cur = out.get(pid)
        # 同一家多账号时，留窗口更多（信息更全）的那个
        if cur is None or len(wins) > len(cur["windows"]):
            out[pid] = {"windows": wins, "plan": qu.get("plan"),
                        "balance": qu.get("balance"), "name": qu.get("name"),
                        "at": v.get("at")}
    return out


def credits():
    """{provider: {"total","used","today","days"}}  积分类账号的逐日消耗"""
    out = {}
    today = datetime.date.today().isoformat()
    for key, v in (_read("credits-daily.json") or {}).items():
        pid = key.split("|")[0]
        last = v.get("last") or {}
        days = v.get("days") or {}
        out[pid] = {"total": last.get("total"), "used": last.get("used"),
                    "today": days.get(today, 0.0), "days": days,
                    "at": last.get("at")}
    return out


if __name__ == "__main__":
    import pprint
    print("=== quotas ===")
    pprint.pprint(quotas())
    print("=== credits ===")
    pprint.pprint(credits())
