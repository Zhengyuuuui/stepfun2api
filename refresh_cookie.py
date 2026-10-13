#!/usr/bin/env python3
"""
refresh_cookie.py — 从本机 Chrome(CDP) 抓取 studio.stepfun.ai 最新 cookie，写入 cookie.txt。

用途：StepFun access token 每 2h 过期，浏览器靠 refresh token(30天) 自动轮换。
本脚本定期从浏览器同步最新 cookie，配合网关热重载实现自动续期。

特性：
- 无需常驻标签页：即使用户关闭了 studio.stepfun.ai 标签页，也可直接从 Chrome Cookie Jar 读取凭证；
  若真过期需要换签，会自动静默创建一次性后台标签页（background=True），换签完成后自动销毁关闭。
- 游客态防护：若捕获到未激活/游客 token（activated=False），拒绝覆盖好凭证。
- 双向同步：刷新成功后自动同步更新 accounts.json 中对应账号的 cookie。

用法：
  python3 refresh_cookie.py                 # 默认 CDP 9366，写 ./cookie.txt
  python3 refresh_cookie.py --check         # 只检查 token 剩余寿命，不写文件
  python3 refresh_cookie.py --force-rotate  # 强制触发轮换（Console「立即续期」）
  python3 refresh_cookie.py --status        # 输出机器可读 JSON 状态
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


def get_browser_ws(port: int):
    """获取 Chrome 根级别 WebSocket 连接（无需任何标签页）。"""
    v = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5))
    ws = websocket.create_connection(v["webSocketDebuggerUrl"], max_size=None)
    ws.settimeout(15)
    return ws


def cdp_targets(port: int):
    """列出当前所有 CDP targets。"""
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))


def find_page(port: int):
    """寻找已有 studio.stepfun.ai 的页面 target（没有则返回 None，不抛异常）。"""
    try:
        data = cdp_targets(port)
        pages = [t for t in data if t.get("type") == "page" and TARGET_HOST in t.get("url", "")]
        return pages[0] if pages else None
    except Exception:
        return None


def get_cookies_from_storage(port: int) -> dict:
    """直接从 Chrome 底层 Cookie 存储读取 stepfun.ai 相关 cookies（无论是否有打开的标签页）。"""
    try:
        ws = get_browser_ws(port)
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        r = json.loads(ws.recv())
        ws.close()
        cookies = r.get("result", {}).get("cookies", [])
        return {
            c["name"]: c["value"]
            for c in cookies
            if "stepfun.ai" in c.get("domain", "") or TARGET_HOST in c.get("domain", "")
        }
    except Exception as e:
        return {}


def get_cookies_from_page(ws_url: str) -> dict:
    """从特定页面的 debugger ws 读取 cookies。"""
    try:
        ws = websocket.create_connection(ws_url, max_size=None)
        ws.settimeout(15)
        ws.send(json.dumps({"id": 1, "method": "Network.getCookies",
                            "params": {"urls": [f"https://{TARGET_HOST}/"]}}))
        r = json.loads(ws.recv())
        ws.close()
        return {c["name"]: c["value"] for c in r.get("result", {}).get("cookies", [])}
    except Exception:
        return {}


def trigger_rotation(port: int, wait_s: float = 7.0) -> bool:
    """
    触发 token 轮换：
    - 若浏览器当前已打开 studio.stepfun.ai 页面，则发送 Page.reload 触发轮换；
    - 若浏览器当前未打开任何 studio 页面，则静默在后台创建一次性目标（background=True），
      等待 SPA 完成换签后，自动销毁关闭该后台页面，用户完全无感知。
    """
    page = find_page(port)
    if page:
        try:
            ws = websocket.create_connection(page["webSocketDebuggerUrl"], max_size=None)
            ws.settimeout(15)
            ws.send(json.dumps({"id": 1, "method": "Page.reload", "params": {"ignoreCache": False}}))
            deadline = time.time() + 10
            while time.time() < deadline:
                m = json.loads(ws.recv())
                if m.get("id") == 1:
                    break
            ws.close()
            time.sleep(wait_s)
            return True
        except Exception:
            pass

    # 无现成页面：在后台静默创建临时 tab，抓完即关闭
    try:
        ws = get_browser_ws(port)
        ws.send(json.dumps({
            "id": 1,
            "method": "Target.createTarget",
            "params": {"url": f"https://{TARGET_HOST}/", "background": True}
        }))
        r = json.loads(ws.recv())
        target_id = r.get("result", {}).get("targetId")
        if not target_id:
            ws.close()
            return False

        time.sleep(wait_s)

        # 关闭静默临时标签页
        ws.send(json.dumps({
            "id": 2,
            "method": "Target.closeTarget",
            "params": {"targetId": target_id}
        }))
        ws.recv()
        ws.close()
        return True
    except Exception:
        return False


def decode_jwt_payload(token: str) -> dict:
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def token_status(oasis_token: str):
    """
    返回 (access剩余分钟, access寿命小时, expired?, is_activated)。
    is_activated 为 False 表示游客/未登录临时 token。
    """
    if not oasis_token:
        return None, None, None, False
    now = int(time.time())
    parts = oasis_token.split("...")
    access = decode_jwt_payload(parts[0]) if parts else {}
    exp = access.get("exp")
    ca = access.get("create_at") or access.get("oasis_r_at")
    activated = access.get("activated", False) is True
    mode = access.get("mode", 2)
    is_valid_account = activated and (mode != 1)

    if not exp:
        return None, None, None, False
    left_min = (exp - now) / 60
    life_h = ((exp - ca) / 3600) if ca else None
    return left_min, life_h, exp < now, is_valid_account


def status_mode(port: int):
    """机器可读状态：CDP/页面/登录/token 剩余。不写文件、不轮换。"""
    out = {"ok": False, "cdp": False, "page": False, "logged_in": False,
           "left_min": None, "expired": None, "error": None}
    try:
        ws = get_browser_ws(port)
        ws.close()
        out["cdp"] = True
    except Exception as e:
        out["error"] = f"CDP 不可用(127.0.0.1:{port}): Chrome 需以 --remote-debugging-port={port} 启动"
        print(json.dumps(out, ensure_ascii=False))
        return

    page = find_page(port)
    out["page"] = page is not None

    cks = get_cookies_from_storage(port)
    if not cks and page:
        cks = get_cookies_from_page(page["webSocketDebuggerUrl"])

    if "Oasis-Token" in cks:
        left, _, ex, is_act = token_status(cks["Oasis-Token"])
        if is_act:
            out.update(ok=True, logged_in=True, expired=ex,
                       left_min=round(left, 1) if left is not None else None)
        else:
            out["error"] = "浏览器当前为游客/未登录状态，请登录账号"
    else:
        out["error"] = "浏览器未找到 StepFun Cookie 凭证，请登录 studio.stepfun.ai"
    print(json.dumps(out, ensure_ascii=False))


def open_login_mode(port: int, url: str = "https://studio.stepfun.ai/", clean: bool = False):
    """在调试 Chrome 里打开/聚焦 studio 页面，供用户登录。clean=True 时清除旧会话以便登录新号。"""
    out = {"ok": False, "opened": False, "already": False, "cleaned": clean, "error": None}
    try:
        page = find_page(port)
        if page:
            ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
            if clean:
                try:
                    ws.send(json.dumps({"id": 1, "method": "Network.clearBrowserCookies"}))
                    ws.recv()
                    ws.send(json.dumps({"id": 2, "method": "Storage.clearDataForOrigin",
                                        "params": {"origin": "https://studio.stepfun.ai",
                                                   "storageTypes": "cookies,local_storage"}}))
                    ws.recv()
                except Exception:
                    pass
            ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {"url": url}}))
            ws.recv()
            ws.close()
            out.update(ok=True, already=True)
        else:
            ws = get_browser_ws(port)
            ws.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": url}}))
            r = json.loads(ws.recv())
            target_id = r.get("result", {}).get("targetId")
            ws.close()
            out.update(ok=True, opened=True, targetId=target_id)
    except Exception as e:
        out["error"] = str(e)
    print(json.dumps(out, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("CDP_PORT", DEFAULT_CDP_PORT)))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt"))
    ap.add_argument("--check", action="store_true", help="只检查寿命，不写文件")
    ap.add_argument("--no-rotate", action="store_true", help="token 过期时不触发页面轮换")
    ap.add_argument("--force-rotate", action="store_true",
                    help="无条件触发页面轮换（Console「立即续期」按钮用）")
    ap.add_argument("--status", action="store_true", help="输出 JSON 状态（CDP/登录/token），不写文件")
    ap.add_argument("--open-login", action="store_true", help="在调试 Chrome 打开/聚焦 studio 登录页")
    ap.add_argument("--clean", action="store_true", help="与 --open-login 一起使用：清除当前会话以便登录新号入池")
    args = ap.parse_args()

    if args.status:
        status_mode(args.port)
        return
    if args.open_login:
        open_login_mode(args.port, clean=args.clean)
        return

    # 1. 优先从底层 Cookie 存储读取（无需开启标签页）
    cks = get_cookies_from_storage(args.port)
    if not cks:
        page = find_page(args.port)
        if page:
            cks = get_cookies_from_page(page["webSocketDebuggerUrl"])

    if not cks or "Oasis-Token" not in cks:
        print("[warn] Chrome 中未检索到 Oasis-Token，保留现有 cookie 文件", file=sys.stderr)
        sys.exit(0)

    hdr = "; ".join(f"{k}={v}" for k, v in cks.items())
    left, life, expired, is_act = token_status(cks["Oasis-Token"])

    if not is_act:
        print("[warn] 浏览器当前为游客/未登录临时 token，保留现有登录文件", file=sys.stderr)
        sys.exit(0)

    # force=无条件轮换；默认=已过期才轮换
    threshold = 1e9 if args.force_rotate else 0
    if (not args.check and not args.no_rotate and left is not None and left < threshold):
        if trigger_rotation(args.port):
            cks2 = get_cookies_from_storage(args.port)
            if not cks2:
                page = find_page(args.port)
                if page:
                    cks2 = get_cookies_from_page(page["webSocketDebuggerUrl"])
            l2, lf2, ex2, is_act2 = token_status(cks2.get("Oasis-Token", ""))
            if is_act2 and l2 is not None and l2 > left:
                cks, hdr, left, life, expired = cks2, "; ".join(
                    f"{k}={v}" for k, v in cks2.items()), l2, lf2, ex2
                print("[info] 已触发页面轮换，成功拿到新 token", file=sys.stderr)
            else:
                print("[info] 已触发轮换但 SPA 未换发新 token（当前 token 仍在有效期内）", file=sys.stderr)

    if args.check:
        print(json.dumps({
            "access_left_min": round(left, 1) if left is not None else None,
            "access_life_h": round(life, 1) if life is not None else None,
            "expired": expired,
            "cookie_names": list(cks.keys()),
        }, ensure_ascii=False))
        return

    # 仍是过期 token 则不覆盖好文件
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

    # 同步写入 accounts.json 对应账号
    try:
        acc_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
        if os.path.exists(acc_file):
            with open(acc_file, "r", encoding="utf-8") as f:
                accs = json.load(f)
            tinfo = decode_jwt_payload(cks.get("Oasis-Token", "").split("...")[0])
            uid = str(tinfo.get("oasis_id") or "")
            changed = False
            for a in accs:
                if a.get("uid") == uid:
                    a["cookie"] = hdr
                    changed = True
            if changed:
                tmp = acc_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(accs, f, ensure_ascii=False, indent=2)
                os.replace(tmp, acc_file)
    except Exception:
        pass


if __name__ == "__main__":
    main()
