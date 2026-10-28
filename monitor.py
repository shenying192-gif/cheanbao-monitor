# -*- coding: utf-8 -*-
"""
车安堡充电监控（GitHub Actions 云端版）
每次由定时任务触发运行一次：查询当前充电状态 -> 与 state.json 对比 ->
检测到充电开始 / 提前结束（被拔）就通过 Server酱 推送到微信。
个人信息全部来自环境变量（仓库 Secrets），本文件不含任何隐私。
"""
import json
import os
import re
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


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def api(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("api-token", API_TOKEN)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_charging():
    r = api("/api/getCurrentRechargeMobile", {"id": int(USER_ID)})
    if r.get("code") != 200:
        raise RuntimeError("接口异常: %s" % json.dumps(r, ensure_ascii=False))
    return r["data"].get("isOrderOn") == 1


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
    log("Server酱推送成功")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"charging": False}


def save_state(charging):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"charging": bool(charging)}, f, ensure_ascii=False, indent=2)


def report_end():
    time.sleep(2)
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
    charging = get_charging()
    was_charging = load_state().get("charging", False)
    log("当前充电中=%s，上次记录=%s" % (charging, was_charging))

    if charging and not was_charging:
        # 刚刚开始充电
        save_state(True)
        if NOTIFY_START:
            serverchan("电动车开始充电",
                       "云端监控已启动，万一被拔，几分钟内微信通知你。")
    elif (not charging) and was_charging:
        # 刚刚结束
        save_state(False)
        report_end()
    else:
        save_state(charging)


if __name__ == "__main__":
    main()
