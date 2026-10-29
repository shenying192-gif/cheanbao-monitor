# -*- coding: utf-8 -*-
"""
车安堡充电监控（GitHub Actions 常驻版）
- 每次 job 启动后进入常驻循环：每 INTERVAL 秒（默认180秒）查一次，
  最多跑 MAX_RUN 秒（默认5小时，低于 GitHub 单 job 6小时上限）。
- 定时任务每 4 小时启动一个新 job，与旧 job 重叠约 1 小时，
  即使定时被 GitHub 延迟，也能无缝衔接，绝大多数时间都有 job 在密集监控。
- 状态机检测充电开始 / 提前结束（被拔），通过 Server酱 推送到微信。
- 通知去重：发通知前先在远程 state.json 里“认领”该订单（git push 成功为准），
  保证重叠的两个 job 不会重复发同一条通知。
个人信息全部来自环境变量（仓库 Secrets），本文件不含任何隐私。
"""
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
BASE = "https://api.cheanbao.cn/api/public/index.php"

USER_ID = os.environ.get("USER_ID", "0")
API_TOKEN = os.environ.get("API_TOKEN", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
MIN_FULL_MINUTES = int(os.environ.get("MIN_FULL_MINUTES", "330"))
NOTIFY_START = os.environ.get("NOTIFY_START", "1") == "1"
INTERVAL = int(os.environ.get("INTERVAL", "180"))          # 每3分钟查一次
MAX_RUN = int(os.environ.get("MAX_RUN", str(5 * 3600)))    # 单次最多跑5小时
KEEP = 20                                                   # 去重列表最多保留条数


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


def api(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("api-token", API_TOKEN)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_status():
    """返回 (是否充电中, 当前订单号)。"""
    r = api("/api/getCurrentRechargeMobile", {"id": int(USER_ID)})
    if r.get("code") != 200:
        raise RuntimeError("接口异常: %s" % json.dumps(r, ensure_ascii=False))
    d = r["data"]
    charging = d.get("isOrderOn") == 1
    order_id = ""
    if charging and isinstance(d.get("orderInfo"), dict):
        order_id = d["orderInfo"].get("order_id", "")
    return charging, order_id


def get_latest_order():
    r = api("/api/getRechargeMobile",
            {"page": 1, "size": 1, "query_columns": {"uid": int(USER_ID)}})
    for group in r.get("data", {}).get("list", []):
        if group.get("datas"):
            return group["datas"][0]
    return None


def parse_minutes(text):
    text = text or ""
    h = re.search(r"(\d+)\s*小时", text)
    m = re.search(r"(\d+)\s*分钟", text)
    return (int(h.group(1)) if h else 0) * 60 + (int(m.group(1)) if m else 0)


def serverchan(title, desp):
    if not SERVERCHAN_KEY:
        log("未配置 SERVERCHAN_KEY，跳过推送")
        return
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    req = urllib.request.Request(
        "https://sctapi.ftqq.com/%s.send" % SERVERCHAN_KEY, data=data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
    log("Server酱推送成功：%s" % title)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("charging", False)
            s.setdefault("active_order", "")
            s.setdefault("notified_start", [])
            s.setdefault("notified_end", [])
            return s
        except Exception:
            pass
    return {"charging": False, "active_order": "",
            "notified_start": [], "notified_end": []}


def save_state(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def trim(lst):
    return lst[-KEEP:]


# ---------------- git 操作 ----------------
def git(*args):
    try:
        r = subprocess.run(["git", *args], capture_output=True,
                           text=True, timeout=90)
        return r.returncode == 0, (r.stdout + r.stderr)
    except Exception as e:
        return False, str(e)


def try_claim(order_id, kind):
    """
    为订单 order_id 认领 kind（start / end）通知。
    以远程仓库最新 state 为准，git push 成功即认领成功（原子），
    重叠的另一个 job 会因订单已被认领而返回 False。
    """
    if not order_id:
        return False
    key = "notified_" + kind
    for attempt in range(4):
        git("fetch", "origin")
        git("reset", "--hard", "origin/main")   # 以远程最新状态为准
        s = load_state()
        if order_id in s.get(key, []):
            log("订单 %s 的 %s 通知已被认领，跳过" % (order_id, kind))
            return False
        s[key] = trim(s.get(key, []) + [order_id])
        save_state(s)
        git("add", "state.json")
        git("commit", "-m", "claim %s %s [skip ci]" % (kind, order_id))
        ok, _ = git("push")
        if ok:
            log("已认领订单 %s 的 %s 通知" % (order_id, kind))
            return True
        log("push 冲突，重试认领（第%d次）" % (attempt + 1))
        time.sleep(1.5)
    return False


def report_end():
    time.sleep(3)  # 等结束订单在服务端落库
    try:
        order = get_latest_order()
    except Exception as e:
        log("拉取订单详情失败: %s" % e)
        order = None
    if not order:
        serverchan("⚠️ 充电已结束", "未获取到订单详情，请立即查看。")
        return
    mins = parse_minutes(order.get("p_minutes"))
    desp = (
        "订单号：%s\n\n"
        "已充电：%s（%d分钟）\n\n"
        "端口：%s\n\n"
        "地点：%s\n\n"
        "结束时间：%s\n\n"
        "费用：%s 元"
        % (order.get("order_no"),
           order.get("p_minutes") or "无记录", mins,
           order.get("port"), order.get("address"),
           order.get("end_time"), order.get("money"))
    )
    if mins < MIN_FULL_MINUTES:
        serverchan("⚠️ 充电可能被拔！只充了%s"
                   % (order.get("p_minutes") or "很短时间"), desp)
    else:
        serverchan("充电完成（%s）" % order.get("p_minutes"), desp)


def main():
    s = load_state()
    # 冷启动兜底：若远程记录显示在充电，就以“充电中”起步，避免漏判
    charging, order_id = False, ""
    try:
        charging, order_id = get_status()
    except Exception as e:
        log("首次查询失败，沿用本地状态: %s" % e)
        charging = s.get("charging", False)
    s["charging"] = charging
    if charging and order_id:
        s["active_order"] = order_id
    log("常驻监控启动：充电中=%s，订单=%s，间隔=%ds，最长跑%ds"
        % (charging, s.get("active_order"), INTERVAL, MAX_RUN))

    run_start = time.time()
    while time.time() - run_start < MAX_RUN:
        try:
            charging, order_id = get_status()
            was = s["charging"]
            if charging and not was:
                # 开始充电
                oid = order_id or ("T" + datetime.now().strftime("%Y%m%d%H%M"))
                s["active_order"] = oid
                s["charging"] = True
                if try_claim(oid, "start"):
                    if NOTIFY_START:
                        serverchan("电动车开始充电",
                                   "云端监控已启动，万一被拔，几分钟内微信通知你。")
            elif (not charging) and was:
                # 充电结束
                oid = s.get("active_order", "")
                s["charging"] = False
                s["active_order"] = ""
                if try_claim(oid, "end"):
                    report_end()
            elif charging and order_id:
                s["active_order"] = order_id
        except Exception as e:
            log("本轮异常（下一轮继续）: %s" % e)
        time.sleep(INTERVAL)

    log("本次常驻时长已到（%ds），job 结束，等待下一个 job 接管" % MAX_RUN)


if __name__ == "__main__":
    main()
