#!/usr/bin/env python3
"""
refresh_cookie.py — 从本机 Chrome(CDP) 抓取 studio.stepfun.ai 最新 cookie，写入 cookie.txt。

用途：StepFun access token 每 2h 过期，浏览器靠 refresh token(30天) 自动轮换。
本脚本定期从浏览器同步最新 cookie，配合网关热重载实现自动续期。

前提：Chrome 以调试端口开着，且已登录 studio.stepfun.ai。
用法：
  python3 refresh_cookie.py                 # 默认 CDP 9366，写 ./cookie.txt
  CDP_PORT=9222 python3 refresh_cookie.py --out /path/cookie.txt
  python3 refresh_cookie.py --check         # 只检查 token 剩余寿命，不写文件
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

try:
    import websocket
except ImportError:
    sys.exit("需要 websocket-client：pip install websocket-client")

DEFAULT_CDP_PORT = 9366
TARGET_HOST = "studio.stepfun.ai"


def find_page(port: int):
    """找一个 studio.stepfun.ai 的 page target。"""
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        data = json.load(urllib.request.urlopen(url, timeout=5))
    except Exception as e:
        raise SystemExit(f"无法连接 CDP 127.0.0.1:{port} —— Chrome 是否以 --remote-debugging-port={port} 开着？({e})")
    pages = [t for t in data if t.get("type") == "page" and TARGET_HOST in t.get("url", "")]
    if not pages:
        raise SystemExit(f"CDP 里没有 {TARGET_HOST} 页面 —— 请先在浏览器打开并登录 https://{TARGET_HOST}/")
    return pages[0]


def get_cookies(ws_url: str):
    ws = websocket.create_connection(ws_url, max_size=None)
    ws.settimeout(15)
    ws.send(json.dumps({"id": 1, "method": "Network.getCookies",
                        "params": {"urls": [f"https://{TARGET_HOST}/"]}}))
    r = json.loads(ws.recv())
    ws.close()
    return {c["name"]: c["value"] for c in r["result"]["cookies"]}


def trigger_rotation(ws_url: str, wait_s: float = 8.0) -> bool:
    """
    access token 由 studio SPA 在页面加载/API 调用时轮换（实测：仅开页面不轮换，
    reload 后 10s 内可拿到新 token）。用 CDP Page.reload 触发轮换并等待。
    """
    try:
        ws = websocket.create_connection(ws_url, max_size=None)
        ws.settimeout(15)
        ws.send(json.dumps({"id": 1, "method": "Page.reload",
                            "params": {"ignoreCache": False}}))
        deadline = time.time() + 10
        while time.time() < deadline:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                break
        ws.close()
        time.sleep(wait_s)
        return True
    except Exception:
        return False


def decode_jwt_payload(token: str):
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def token_status(oasis_token: str):
    """返回 (access剩余分钟, access寿命小时, expired?)。"""
    now = int(time.time())
    parts = oasis_token.split("...")
    access = decode_jwt_payload(parts[0]) if parts else {}
    exp = access.get("exp")
    ca = access.get("create_at") or access.get("oasis_r_at")
    if not exp:
        return None, None, None
    left_min = (exp - now) / 60
    life_h = ((exp - ca) / 3600) if ca else None
    return left_min, life_h, exp < now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("CDP_PORT", DEFAULT_CDP_PORT)))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt"))
    ap.add_argument("--check", action="store_true", help="只检查寿命，不写文件")
    ap.add_argument("--no-rotate", action="store_true", help="token 过期时不触发页面 reload 轮换")
    args = ap.parse_args()

    try:
        page = find_page(args.port)
        cks = get_cookies(page["webSocketDebuggerUrl"])
    except SystemExit as e:
        # 浏览器未开/未登录：保留现有 cookie.txt 不动（定时任务静默失败即可）
        print(f"[warn] {e}", file=sys.stderr)
        sys.exit(0)

    if "Oasis-Token" not in cks:
        print("[warn] 浏览器 cookie 里没有 Oasis-Token，保留现有文件", file=sys.stderr)
        sys.exit(0)

    hdr = "; ".join(f"{k}={v}" for k, v in cks.items())
    left, life, expired = token_status(cks["Oasis-Token"])

    # 过期/临期（<30min）→ 触发页面轮换后重抓一次（SPA 只在页面加载/API调用时轮换 token）
    if (not args.check and not args.no_rotate and left is not None and left < 30
            and trigger_rotation(page["webSocketDebuggerUrl"])):
        cks2 = get_cookies(page["webSocketDebuggerUrl"])
        l2, lf2, ex2 = token_status(cks2.get("Oasis-Token", ""))
        if l2 is not None and l2 > left:
            cks, hdr, left, life, expired = cks2, "; ".join(
                f"{k}={v}" for k, v in cks2.items()), l2, lf2, ex2
            print("[info] 已触发页面轮换，拿到新 token", file=sys.stderr)

    if args.check:
        print(json.dumps({
            "access_left_min": round(left, 1) if left is not None else None,
            "access_life_h": round(life, 1) if life is not None else None,
            "expired": expired,
            "cookie_names": list(cks.keys()),
        }, ensure_ascii=False))
        return

    # 仍是过期 token 就别覆盖好文件
    if expired:
        print(f"[warn] 轮换后 token 仍过期(剩余{left:.0f}min)，保留现有文件", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(hdr)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{ts}] cookie 已刷新 -> {args.out} (len={len(hdr)}"
    if left is not None:
        msg += f", access 剩余 {left:.0f}min / 寿命 {life:.1f}h"
    print(msg + ")")


if __name__ == "__main__":
    main()
