#!/usr/bin/env python3
"""采集四家的额度 + 按模型拆分的消耗。

每家一个 dict：
{
  "id","name","plan","status","reason",
  "headline": "12,340 积分",        # 卡片右上角大数字（额度/余额）
  "metrics": [                       # 左栏「额度」，最多 3 行
      {"label":"5h","used":0.62,"resets_at":...,"seconds":...},   # 带进度条
      {"label":"今日","value":"8.4M"}                              # 纯文字行
  ],
  "models": [{"name":"GLM-5.3-Flash","value":"3.1M"}]            # 右栏「模型消耗」，最多 3 行
}

两个来源分开：
- 额度（5h/周/月、积分余额）→ 官方接口，要 key/cookie
- 模型消耗 → 尽量读本地已落盘的数据（WorkBuddy 的 sqlite、Codex 的 rollout、
  ZCode 的 model_usage 表）。所以即使一家没配 key，右栏照样有内容。
"""

import glob
import json
import os
import re
import sqlite3
import ssl
import time
import urllib.request

from . import magpie

def config_candidates():
    """配置文件的查找顺序，谁先命中用谁。"""
    out = []
    env = os.environ.get("QUOTA_GLANCE_CONFIG")
    if env:
        out.append(os.path.expanduser(env))
    out.append(os.path.join(os.getcwd(), "config.json"))
    out.append(os.path.expanduser("~/.config/quota-glance/config.json"))
    return out


def load_config():
    for p in config_candidates():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            continue
    return {}


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)


# ---------------------------------------------------------------- 数字格式

def fmt_num(n, unit=""):
    if n is None:
        return "-"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1e9:
        s = "%.1fB" % (n / 1e9)
    elif n >= 1e6:
        s = "%.1fM" % (n / 1e6)
    elif n >= 1e3:
        s = "%.0fK" % (n / 1e3)
    elif n >= 10:
        s = "%.0f" % n
    else:
        s = "%.1f" % n
    return s + unit


def fmt_comma(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    return "{:,.0f}".format(n) if abs(n) >= 100 else "{:,.1f}".format(n)


def today_start():
    d = time.localtime()
    return time.mktime((d.tm_year, d.tm_mon, d.tm_mday, 0, 0, 0, 0, 0, -1))


def _top(d, n=3):
    """{name: value} → 排好序的前 n 个 models。"""
    items = sorted(d.items(), key=lambda kv: -kv[1])[:n]
    return [{"name": k, "value": v} for k, v in items if v > 0]


# ---------------------------------------------------------------- 本地模型明细

def local_workbuddy():
    """WorkBuddy: 每个 session 的模型 × credit_json 里的积分。"""
    p = os.path.expanduser("~/.workbuddy/workbuddy.db")
    if not os.path.exists(p):
        return {}, 0.0, 0.0
    t0 = today_start()
    per_model, today, total = {}, 0.0, 0.0
    try:
        c = _ro(p)
        q = """select s.model, u.credit_json, u.updated_at
               from session_usage u left join sessions s on s.id = u.session_id"""
        for model, cj, upd in c.execute(q):
            try:
                credits = sum(float(v) for v in json.loads(cj or "{}").values())
            except Exception:
                continue
            if credits <= 0:
                continue
            name = model or "unknown"
            per_model[name] = per_model.get(name, 0.0) + credits
            total += credits
            if upd and upd / 1000.0 >= t0:
                today += credits
        c.close()
    except Exception:
        return {}, 0.0, 0.0
    out = {k: v for k, v in per_model.items()}
    return out, today, total


def local_codex():
    """Codex: 遍历 rollout jsonl，用 token_count 的累计值算增量，按当前模型归类。"""
    root = os.path.expanduser("~/.codex/sessions")
    if not os.path.isdir(root):
        return {}, 0.0, 0.0
    t0 = today_start()
    per_model, today, total = {}, 0.0, 0.0
    for fp in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(fp) < t0 - 86400:      # 昨天之前没动过的跳过
                continue
        except OSError:
            continue
        cur, prev = None, None
        try:
            for line in open(fp, encoding="utf-8", errors="ignore"):
                if '"turn_context"' not in line and '"token_count"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                p = ev.get("payload") or {}
                kind = p.get("type") or ev.get("type")
                if kind == "turn_context":
                    cur = (p.get("model") or p.get("model_name")
                           or p.get("model_id") or cur)
                elif kind == "token_count":
                    tot = ((p.get("info") or {}).get("total_token_usage") or {})
                    if not tot:
                        continue
                    if prev is None:
                        delta = float(tot.get("total_tokens") or 0)
                    else:
                        delta = float(tot.get("total_tokens") or 0) - float(prev.get("total_tokens") or 0)
                    prev = tot
                    if delta <= 0:
                        continue
                    name = cur or "gpt"
                    per_model[name] = per_model.get(name, 0.0) + delta
                    total += delta
                    ts = ev.get("timestamp") or ""
                    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", ts)
                    if m:
                        y, mo, dd, hh, mi, ss = map(int, m.groups())
                        # 时间戳是 UTC
                        if time.time() - time.mktime((y, mo, dd, hh, mi, ss, 0, 0, 0)) < 86400 * 400:
                            got = calendar_ts_utc(y, mo, dd, hh, mi, ss)
                            if got >= t0:
                                today += delta
        except Exception:
            continue
    return per_model, today, total


def calendar_ts_utc(y, mo, dd, hh, mi, ss):
    import calendar
    return calendar.timegm((y, mo, dd, hh, mi, ss, 0, 0, 0))


def local_zcode():
    """ZCode: model_usage 表，model_id + provider_total_tokens + started_at(ms)。"""
    p = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
    if not os.path.exists(p):
        return {}, 0.0, 0.0
    t0 = today_start()
    now = time.time()
    per_model, today, total = {}, 0.0, 0.0
    win5h, win7d = 0.0, 0.0
    try:
        c = _ro(p)
        for mid, toks, started in c.execute(
                "select model_id, provider_total_tokens, started_at from model_usage"):
            toks = float(toks or 0)
            if toks <= 0:
                continue
            name = mid or "unknown"
            per_model[name] = per_model.get(name, 0.0) + toks
            total += toks
            if not started:
                continue
            ts = started / 1000.0
            if ts >= t0:
                today += toks
            if now - ts <= 5 * 3600:
                win5h += toks
            if now - ts <= 7 * 86400:
                win7d += toks
        c.close()
    except Exception:
        return {}, 0.0, 0.0
    return per_model, today, total, win5h, win7d


# ---------------------------------------------------------------- 额度接口解析

PCT_KEYS = ("used_percent", "usedPercent", "used_pct", "utilization",
            "usage_percent", "usagePercent", "percent_used", "percent")
LIMIT_KEYS = ("limit", "total", "quota", "total_credits", "monthly_credits",
              "entitlement", "total_quota")
LEFT_KEYS = ("remaining", "left", "balance", "remaining_credits",
             "credits_remaining", "available", "remain")
SEC_KEYS = ("limit_window_seconds", "window_seconds", "windowSeconds",
            "period_seconds", "duration_seconds")
RESET_KEYS = ("resets_at", "reset_at", "resetsAt", "resetAt", "reset_time",
              "resetsAtMs", "reset_at_ms")
RESET_IN_KEYS = ("reset_after_seconds", "reset_in_seconds", "resets_in_seconds",
                 "resetsInSeconds", "reset_after")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _reset_at(d):
    for k in RESET_KEYS:
        if k in d:
            v = _num(d[k])
            if v is None:
                continue
            return v / 1000.0 if v > 1e11 else v
    for k in RESET_IN_KEYS:
        if k in d:
            v = _num(d[k])
            if v is not None:
                return time.time() + v
    return None


def _secs(d):
    for k in SEC_KEYS:
        if k in d:
            v = _num(d[k])
            if v and v > 0:
                return v
    for k in RESET_IN_KEYS:
        if k in d:
            v = _num(d[k])
            if v and v > 0:
                return v
    return None


def label_from_seconds(sec):
    if not sec:
        return None
    sec = float(sec)
    for s, lab in ((18000, "5h"), (21600, "6h"), (43200, "12h"),
                   (86400, "日"), (604800, "周"), (2592000, "月")):
        if abs(sec - s) / s < 0.12:
            return lab
    if sec < 86400:
        return "%d小时" % round(sec / 3600)
    return "%d天" % round(sec / 86400)


LBL = {"rolling": "滚动", "weekly": "周", "monthly": "月", "week": "周",
       "month": "月", "block": "5h", "five_hour": "5h", "session": "5h",
       "day": "日", "daily": "日",
       "primary_window": None, "secondary_window": None, "primary": None,
       "secondary": None, "quota": None, "usage": None, "limits": None,
       "limit": None}


def _windows_from(obj, label=""):
    out = []
    if isinstance(obj, dict):
        used = None
        for k in PCT_KEYS:
            if k in obj:
                v = _num(obj[k])
                if v is not None:
                    used = v / 100.0 if v > 1.0 else v
                    break
        if used is None:
            lim = next((_num(obj[k]) for k in LIMIT_KEYS if k in obj and _num(obj[k])), None)
            left = next((_num(obj[k]) for k in LEFT_KEYS if k in obj and _num(obj[k]) is not None), None)
            if lim and left is not None:
                used = max(0.0, min(1.0, 1.0 - left / lim))
        if used is not None:
            out.append({"label": label or "用量",
                        "used": max(0.0, min(1.0, used)),
                        "resets_at": _reset_at(obj), "seconds": _secs(obj)})
            return out
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out.extend(_windows_from(v, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_windows_from(v, label or ("#%d" % (i + 1))))
    return out


def _polish(windows):
    seen, out = set(), []
    for w in windows:
        lab = label_from_seconds(w.get("seconds")) or LBL.get(w["label"], w["label"])
        if lab is None or lab in seen:
            continue
        seen.add(lab)
        w["label"] = lab
        out.append(w)
    order = {"5h": 0, "6h": 0, "12h": 0, "滚动": 0, "日": 1, "周": 2, "月": 3}
    out.sort(key=lambda w: (order.get(w["label"], 4), w.get("seconds") or 0))
    return out


def _dig(obj, *keys):
    """在嵌套 JSON 里找第一个存在的 key。"""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = _dig(v, *keys)
                if r is not None:
                    return r
    elif isinstance(obj, list):
        for v in obj:
            r = _dig(v, *keys)
            if r is not None:
                return r
    return None


# ---------------------------------------------------------------- HTTP

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_SSL = None


def _ctx():
    global _SSL
    if _SSL is not None:
        return _SSL
    cands = []
    try:
        import certifi
        cands.append(certifi.where())
    except Exception:
        pass
    cands += ["/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"]
    for c in cands:
        if c and os.path.exists(c):
            try:
                _SSL = ssl.create_default_context(cafile=c)
                return _SSL
            except Exception:
                continue
    _SSL = ssl.create_default_context()
    return _SSL


def _http(url, method="GET", headers=None, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise RuntimeError("返回不是 JSON")


# ---------------------------------------------------------------- 四家

def collect_workbuddy(cfg):
    """腾讯 WorkBuddy。额度优先用 Magpie 已归一化的积分数据，模型消耗读本地 sqlite。"""
    per_model, _today, _total = local_workbuddy()
    row = {"id": "workbuddy", "name": "WorkBuddy", "plan": "CodeBuddy 订阅",
           "status": "ok", "headline": "--",
           "metrics": [], "models": _top(per_model), "model_unit": "积分",
           "models_label": "模型消耗（累计）"}
    for m in row["models"]:
        m["value"] = "%.1f" % m["value"]

    cr = magpie.credits().get("workbuddy") or {}
    total, used = cr.get("total"), cr.get("used")
    if total and used is not None:
        left = max(0.0, float(total) - float(used))
        row["metrics"].append({"label": "已用", "used": max(0.0, min(1.0, float(used) / float(total)))})
        row["metrics"].append({"label": "今日", "value": "%.1f" % (cr.get("today") or 0)})
        row["metrics"].append({"label": "剩余", "value": fmt_comma(left)})
        row["headline"] = "%s / %s" % (fmt_comma(left), fmt_comma(total))
        row["plan"] = "CodeBuddy · 已用 %s 积分" % fmt_comma(used)
    else:
        if _today or _total:
            row["metrics"] = [{"label": "今日", "value": "%s 积分" % fmt_num(_today)},
                              {"label": "累计", "value": "%s 积分" % fmt_num(_total)}]
        else:
            row["metrics"] = [{"label": "积分", "value": "无数据"}]
    if not row["models"]:
        row["model_note"] = "本地无用量记录"
    return row


def collect_opencode(cfg):
    """OpenCode Go。额度优先用 Magpie 的 windows，其次官方 zen usage。"""
    row = {"id": "opencode", "name": "OpenCode Go", "plan": "Go 订阅",
           "status": "ok", "headline": "--", "metrics": [], "models": [],
           "model_unit": "tokens", "model_note": "需要本地 session 库"}
    q = (magpie.quotas().get("opencode-go") or {})
    wins = q.get("windows") or []
    if wins:
        row["metrics"] = [{"label": w["label"], "used": w["used"],
                           "resets_at": w.get("resets_at")} for w in wins[:3]]
        if q.get("plan"):
            row["plan"] = "Go 订阅 · " + str(q["plan"])
        row["headline"] = "%d%%" % round(wins[0]["used"] * 100)
        return row

    key = cfg.get("opencode_api_key") or os.environ.get("OPENCODE_API_KEY")
    if key:
        obj = _http("https://opencode.ai/zen/go/v1/usage",
                    headers={"Authorization": "Bearer " + key,
                             "User-Agent": UA, "Accept": "application/json"})
        for w in _polish(_windows_from(obj))[:3]:
            row["metrics"].append({"label": w["label"], "used": w["used"],
                                   "resets_at": w.get("resets_at"),
                                   "seconds": w.get("seconds")})
        if row["metrics"]:
            row["headline"] = "%d%%" % round(row["metrics"][0]["used"] * 100)
            return row
    row["metrics"] = [{"label": "额度", "value": "未配置"}]
    return row


def collect_codex(cfg):
    """Codex。额度优先用 Magpie（和官方接口读出来的值一致），模型消耗读本地 rollout。"""
    per_model, today, total = local_codex()
    row = {"id": "codex", "name": "Codex", "plan": "", "status": "ok",
           "headline": "--", "metrics": [], "models": _top(per_model),
           "model_unit": "tokens"}
    for m in row["models"]:
        m["value"] = fmt_num(m["value"])

    q = (magpie.quotas().get("codex") or {})
    wins = q.get("windows") or []
    if wins:
        row["metrics"] = [{"label": w["label"], "used": w["used"],
                           "resets_at": w.get("resets_at")} for w in wins[:2]]
        row["headline"] = "%d%%" % round(wins[0]["used"] * 100)
        row["plan"] = ("ChatGPT " + str(q.get("plan"))).strip() if q.get("plan") else ""

    if not row["metrics"]:
        auth_path = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
        try:
            auth = json.load(open(auth_path, encoding="utf-8"))
            tok = (auth.get("tokens") or {}).get("access_token") or auth.get("access_token")
            obj = _http("https://chatgpt.com/backend-api/wham/usage",
                        headers={"Authorization": "Bearer " + tok,
                                 "User-Agent": UA, "Accept": "application/json"})
            for w in _polish(_windows_from(obj))[:2]:
                row["metrics"].append({"label": w["label"], "used": w["used"],
                                       "resets_at": w.get("resets_at"),
                                       "seconds": w.get("seconds")})
            if row["metrics"]:
                row["headline"] = "%d%%" % round(row["metrics"][0]["used"] * 100)
            plan = str(obj.get("plan_type") or "")
            if plan:
                row["plan"] = "ChatGPT " + plan
        except Exception as e:
            row["reason"] = "额度接口失败：%s" % str(e)[:40]

    if today:
        row["metrics"].append({"label": "今日", "value": fmt_num(today)})
    if total and abs(total - today) > 1:
        row["metrics"].append({"label": "累计", "value": fmt_num(total)})
    row["metrics"] = row["metrics"][:3]
    return row


def collect_zcode(cfg):
    """ZCode = 智谱 Z.ai Coding Plan。
    没有 API key、Magpie 也不支持这家，订阅额度只能问它自己的登录态（拿不到）。
    所以左栏用本地 model_usage 按时间戳重建"近 5 小时 / 近 7 天"，给绝对值。"""
    per_model, today, total, win5h, win7d = local_zcode()
    row = {"id": "zcode", "name": "ZCode", "plan": "Z.ai Coding Plan",
           "status": "ok", "headline": "--", "metrics": [],
           "models": _top(per_model), "model_unit": "tokens"}
    for m in row["models"]:
        m["value"] = fmt_num(m["value"])

    key = cfg.get("zcode_api_key") or os.environ.get("ZCODE_API_KEY") \
        or os.environ.get("ZAI_API_KEY")
    if key:
        host = "https://open.bigmodel.cn" if cfg.get("zcode_cn") else "https://api.z.ai"
        try:
            obj = _http(host + "/api/monitor/usage/quota/limit",
                        headers={"Authorization": "Bearer " + key,
                                 "User-Agent": UA, "Accept": "application/json"})
            for w in _polish(_windows_from(obj))[:2]:
                row["metrics"].append({"label": w["label"], "used": w["used"],
                                       "resets_at": w.get("resets_at"),
                                       "seconds": w.get("seconds")})
            if row["metrics"]:
                row["headline"] = "%d%%" % round(row["metrics"][0]["used"] * 100)
        except Exception as e:
            row["reason"] = "额度接口失败：%s" % str(e)[:40]

    if win5h or win7d:
        row["metrics"].append({"label": "近5h", "value": fmt_num(win5h)})
        row["metrics"].append({"label": "近7天", "value": fmt_num(win7d)})
    if today:
        row["metrics"].append({"label": "今日", "value": fmt_num(today)})
    row["metrics"] = row["metrics"][:3]
    row["plan"] = "Z.ai Coding Plan · 额度需 App 内查看"
    return row


COLLECTORS = [("workbuddy", "WorkBuddy", collect_workbuddy),
              ("opencode", "OpenCode Go", collect_opencode),
              ("codex", "Codex", collect_codex),
              ("zcode", "ZCode", collect_zcode)]

DEMO_BASE = time.time()


def demo():
    now = DEMO_BASE
    return [
        {"id": "workbuddy", "name": "WorkBuddy", "plan": "CodeBuddy 订阅",
         "status": "ok", "headline": "12,340 积分",
         "metrics": [{"label": "月度", "used": 0.38, "resets_at": now + 9 * 86400,
                      "seconds": 30 * 86400},
                     {"label": "今日", "value": "8.4 积分"},
                     {"label": "累计", "value": "63 积分"}],
         "models": [{"name": "deepseek-v4.1-flash", "value": "6.2"},
                    {"name": "glm-5.3", "value": "1.9"},
                    {"name": "kimi-k2.6", "value": "0.3"}]},
        {"id": "opencode", "name": "OpenCode Go", "plan": "Go 订阅",
         "status": "ok", "headline": "62%",
         "metrics": [{"label": "5h", "used": 0.62,
                      "resets_at": now + 2 * 3600 + 13 * 60, "seconds": 5 * 3600},
                     {"label": "周", "used": 0.31,
                      "resets_at": now + 3 * 86400, "seconds": 7 * 86400},
                     {"label": "今日", "value": "4.1M"}],
         "models": [{"name": "glm-5.3", "value": "2.4M"},
                    {"name": "qwen3-coder", "value": "1.2M"},
                    {"name": "kimi-k2.6", "value": "0.5M"}]},
        {"id": "codex", "name": "Codex", "plan": "ChatGPT free",
         "status": "ok", "headline": "50%",
         "metrics": [{"label": "月", "used": 0.50,
                      "resets_at": now + 25 * 86400, "seconds": 30 * 86400},
                     {"label": "今日", "value": "3.8M"},
                     {"label": "累计", "value": "41M"}],
         "models": [{"name": "gpt-5.6-terra", "value": "2.6M"},
                    {"name": "deepseek-v4.1-flash", "value": "0.9M"},
                    {"name": "mimo-v2.6-flash", "value": "0.3M"}]},
        {"id": "zcode", "name": "ZCode", "plan": "Z.ai Coding Plan",
         "status": "ok", "headline": "24%",
         "metrics": [{"label": "5h", "used": 0.24,
                      "resets_at": now + 3 * 3600 + 20 * 60, "seconds": 5 * 3600},
                     {"label": "周", "used": 0.41,
                      "resets_at": now + 5 * 86400, "seconds": 7 * 86400},
                     {"label": "今日", "value": "9.1M"}],
         "models": [{"name": "GLM-5.3-Flash", "value": "7.2M"},
                    {"name": "GLM-5.3", "value": "1.8M"},
                    {"name": "GLM-4.6", "value": "0.1M"}]},
    ]


def collect_all(cfg=None, use_demo=False):
    if use_demo:
        return demo(), "demo"
    cfg = cfg if cfg is not None else load_config()
    out = []
    for pid, pretty, fn in COLLECTORS:
        try:
            r = fn(cfg)
            # 同一行标签只留一个（重建窗口和接口窗口可能撞名）
            seen, ms = set(), []
            for m in r.get("metrics") or []:
                if m.get("label") in seen:
                    continue
                seen.add(m.get("label"))
                ms.append(m)
            r["metrics"] = ms[:3]
            out.append(r)
        except Exception as e:
            out.append({"id": pid, "name": pretty, "plan": "",
                        "status": "unavailable", "reason": str(e),
                        "headline": "--", "metrics": [], "models": []})
    return out, "live"
