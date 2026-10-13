"""
pool.py — StepFun 账号池管理器 (Account Pool)

功能：
- 管理多个 StepFun 账号凭证，支持 accounts.json 持久化与热重载
- 自动从 cookie.txt 初始化首个账号（平滑升级）
- 轮询（Round-Robin）调度分发请求，实现多账号额度聚合与速率负载均衡
- 自动故障转移：当某账号遭遇 429（限流）或 401（失效）时自动切换下一账号重试
- 提供账号列表、状态切换、删除、配额刷新、以及新账号导入与绑定接口
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from stepfun_client import StepFunClient, StepFunError

log = logging.getLogger("stepfun2api.pool")


def parse_token_info(cookie_str: str) -> dict:
    """从 cookie 字符串提取 Oasis-Token 并解析 access token 剩余寿命与 payload。"""
    m = re.search(r"Oasis-Token=([^;]+)", cookie_str)
    if not m:
        return {"ok": False, "left_min": None, "life_h": None, "expired": True, "uid": None}
    ot = m.group(1).strip()
    parts = ot.split("...")
    if not parts or not parts[0]:
        return {"ok": False, "left_min": None, "life_h": None, "expired": True, "uid": None}
    jwt_parts = parts[0].split(".")
    if len(jwt_parts) < 2:
        return {"ok": False, "left_min": None, "life_h": None, "expired": True, "uid": None}
    try:
        b64 = jwt_parts[1] + "=" * (-len(jwt_parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")))
        exp = payload.get("exp", 0)
        create_at = payload.get("create_at", exp - 7200)
        now = time.time()
        left_min = (exp - now) / 60.0
        life_h = (exp - create_at) / 3600.0 if exp > create_at else 2.0
        return {
            "ok": True,
            "left_min": round(left_min, 1),
            "life_h": round(life_h, 1),
            "expired": exp < now,
            "uid": str(payload.get("oasis_id") or ""),
            "create_at": create_at,
            "exp": exp,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "left_min": None, "life_h": None, "expired": True, "uid": None}


def mask_cookie(cookie_str: str) -> str:
    """掩码敏感凭证供前端展示。"""
    if not cookie_str:
        return ""
    parts = []
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            if len(v) > 16:
                parts.append(f"{k}={v[:8]}...{v[-4:]}")
            else:
                parts.append(f"{k}={v}")
    return "; ".join(parts)


class AccountPool:
    def __init__(self, filepath: str = "accounts.json", default_config: Optional[dict] = None):
        self.filepath = filepath
        self.config = default_config or {}
        self.accounts: List[dict] = []
        self._clients: Dict[str, StepFunClient] = {}
        self._rr_index = 0
        self._lock = asyncio.Lock()
        self.load()

    def load(self):
        """加载 accounts.json；如果不存在，自动从 cookie.txt 初始化首个账号。"""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.accounts = json.load(f)
                log.info("已加载号池 accounts.json，共有 %d 个账号", len(self.accounts))
            except Exception as e:
                log.error("读取 accounts.json 失败: %s", e)
                self.accounts = []
        else:
            # 兼容模式：首次启动若无 accounts.json，从已有 cookie.txt 自动导入
            cpath = self.config.get("cookie_file") or "cookie.txt"
            if os.path.exists(cpath):
                raw = open(cpath, encoding="utf-8").read().strip()
                if raw:
                    tinfo = parse_token_info(raw)
                    uid = tinfo.get("uid") or "default"
                    initial_acc = {
                        "uid": uid,
                        "email": f"account-{uid[:8]}",
                        "name": f"默认账号 ({uid[:8]})",
                        "cookie": raw,
                        "status": "active",
                        "free_rate": 1.0,
                        "five_hour_rate": None,
                        "weekly_rate": None,
                        "plan_desc": "无（免费版）",
                        "pay_mode": 0,
                        "requests_count": 0,
                        "tokens_count": 0,
                        "created_at": time.time(),
                        "last_used_at": 0,
                        "last_checked_at": 0,
                        "last_error": None,
                    }
                    self.accounts = [initial_acc]
                    self.save()
                    log.info("号池 accounts.json 自动初始化完成（已继承 %s，UID %s）", cpath, uid)

        # 初始化 client 映射
        self._rebuild_clients()

    def save(self):
        """原子写入 accounts.json。"""
        tmp = self.filepath + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.accounts, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.filepath)
        except Exception as e:
            log.error("保存 accounts.json 失败: %s", e)

    def _rebuild_clients(self):
        """同步 accounts 列表与 _clients 实例。"""
        cur_uids = {acc["uid"] for acc in self.accounts}
        # 移除已删除的 client
        for uid in list(self._clients.keys()):
            if uid not in cur_uids:
                asyncio.create_task(self._clients[uid].aclose())
                del self._clients[uid]
        # 更新或新增 client
        for acc in self.accounts:
            uid = acc["uid"]
            cookie = acc.get("cookie", "")
            if uid in self._clients:
                self._clients[uid].update_cookie(cookie)
            else:
                self._clients[uid] = StepFunClient(
                    cookie=cookie,
                    proxy=self.config.get("proxy"),
                    timeout=self.config.get("request_timeout", 180),
                    language=self.config.get("language", "zh-CN"),
                )

    def get_client_by_uid(self, uid: str) -> Optional[StepFunClient]:
        return self._clients.get(uid)

    def get_account_by_uid(self, uid: str) -> Optional[dict]:
        for acc in self.accounts:
            if acc["uid"] == uid:
                return acc
        return None

    def get_next_client(self, exclude_uids: Optional[set] = None) -> Tuple[Optional[dict], Optional[StepFunClient]]:
        """
        轮询调度：从 active 状态且未排除的账号中选取下一个。
        支持故障转移：传入 exclude_uids 避免重复调用出错账号。
        """
        exclude = exclude_uids or set()
        active = [acc for acc in self.accounts if acc.get("status") == "active" and acc["uid"] not in exclude]
        if not active:
            # 没有可选 active 账号时，如果还有其他 active（只是在 exclude 中），返回 None 触发上层处理
            return None, None

        self._rr_index = (self._rr_index + 1) % len(active)
        acc = active[self._rr_index]
        cli = self._clients.get(acc["uid"])
        if not cli:
            # 兜底新建
            cli = StepFunClient(
                cookie=acc["cookie"],
                proxy=self.config.get("proxy"),
                timeout=self.config.get("request_timeout", 180),
                language=self.config.get("language", "zh-CN"),
            )
            self._clients[acc["uid"]] = cli
        return acc, cli

    async def add_or_update_from_cookie(self, cookie_str: str, name: str = "") -> dict:
        """
        从新 cookie 解析用户并验证入池。
        自动请求 UserInfo、QueryStudioRateLimit、GetStepPlanStatus。
        如果 UID 已存在则更新 Cookie，否则追加新账号。
        """
        cookie_str = cookie_str.strip()
        tinfo = parse_token_info(cookie_str)
        if not tinfo.get("ok"):
            raise ValueError(f"无效的 Oasis-Token: {tinfo.get('error', '无法解析 JWT')}")

        # 创建临时 client 校验并抓取详情
        test_client = StepFunClient(
            cookie=cookie_str,
            proxy=self.config.get("proxy"),
            timeout=15,
            language=self.config.get("language", "zh-CN"),
        )
        try:
            user = await test_client.get_user_info()
            uid = str(user.get("uid") or tinfo.get("uid") or "")
            if not uid:
                raise ValueError("获取用户信息失败，无法确认账号 UID")
            email = user.get("email") or f"user_{uid[:8]}"
            pay_mode = user.get("payMode", 0)

            # 抓取配额
            free_rate, five_hour, weekly = None, None, None
            try:
                quota = await test_client.get_quota()
                free_rate = quota.get("free_rate")
                five_hour = quota.get("five_hour_usage_left_rate")
                weekly = quota.get("weekly_usage_left_rate")
            except Exception as e:
                log.warning("抓取新账号额度失败（跳过）: %s", e)

            # 抓取订阅
            plan_desc = "无（免费版）"
            try:
                plan = await test_client.get_plan_status()
                if plan.get("status") != 206:
                    plan_desc = plan.get("desc", "有效订阅")
            except Exception:
                pass

        finally:
            await test_client.aclose()

        async with self._lock:
            existing = self.get_account_by_uid(uid)
            if existing:
                existing["cookie"] = cookie_str
                existing["email"] = email
                existing["status"] = "active"
                existing["pay_mode"] = pay_mode
                existing["plan_desc"] = plan_desc
                if free_rate is not None:
                    existing["free_rate"] = free_rate
                if five_hour is not None:
                    existing["five_hour_rate"] = five_hour
                if weekly is not None:
                    existing["weekly_rate"] = weekly
                existing["last_checked_at"] = time.time()
                existing["last_error"] = None
                target = existing
                log.info("号池账号已更新: UID %s (%s)", uid, email)
            else:
                target = {
                    "uid": uid,
                    "email": email,
                    "name": name or email.split("@")[0] if "@" in email else f"账号-{uid[:8]}",
                    "cookie": cookie_str,
                    "status": "active",
                    "free_rate": free_rate if free_rate is not None else 1.0,
                    "five_hour_rate": five_hour,
                    "weekly_rate": weekly,
                    "plan_desc": plan_desc,
                    "pay_mode": pay_mode,
                    "requests_count": 0,
                    "tokens_count": 0,
                    "created_at": time.time(),
                    "last_used_at": 0,
                    "last_checked_at": time.time(),
                    "last_error": None,
                }
                self.accounts.append(target)
                log.info("新账号已绑入号池: UID %s (%s)，当前号池共 %d 个账号", uid, email, len(self.accounts))

            self.save()
            self._rebuild_clients()

        return target

    async def refresh_account_quota(self, uid: str) -> dict:
        """刷新指定账号的上游额度。"""
        acc = self.get_account_by_uid(uid)
        if not acc:
            raise ValueError(f"未找到账号 {uid}")
        cli = self.get_client_by_uid(uid)
        if not cli:
            raise ValueError(f"账号 {uid} 客户端未就绪")

        quota = await cli.get_quota()
        acc["free_rate"] = quota.get("free_rate")
        acc["five_hour_rate"] = quota.get("five_hour_usage_left_rate")
        acc["weekly_rate"] = quota.get("weekly_usage_left_rate")
        acc["last_checked_at"] = time.time()
        self.save()
        return acc

    async def refresh_all_quotas(self) -> List[dict]:
        """批量并发刷新所有账号额度。"""
        tasks = [self.refresh_account_quota(acc["uid"]) for acc in self.accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return self.to_public_list()

    def toggle_account(self, uid: str, enabled: Optional[bool] = None) -> bool:
        """切换账号启用状态。"""
        acc = self.get_account_by_uid(uid)
        if not acc:
            return False
        if enabled is None:
            acc["status"] = "disabled" if acc.get("status") == "active" else "active"
        else:
            acc["status"] = "active" if enabled else "disabled"
        self.save()
        return True

    def delete_account(self, uid: str) -> bool:
        """从号池删除指定账号。"""
        before_len = len(self.accounts)
        self.accounts = [a for a in self.accounts if a["uid"] != uid]
        if len(self.accounts) < before_len:
            if uid in self._clients:
                asyncio.create_task(self._clients[uid].aclose())
                del self._clients[uid]
            self.save()
            log.info("已从号池删除账号 UID %s", uid)
            return True
        return False

    def record_usage(self, uid: str, tokens: int = 0, latency_ms: float = 0, error: Optional[str] = None):
        """记录单次调用指标。"""
        acc = self.get_account_by_uid(uid)
        if not acc:
            return
        acc["requests_count"] = acc.get("requests_count", 0) + 1
        acc["tokens_count"] = acc.get("tokens_count", 0) + max(0, tokens)
        acc["last_used_at"] = time.time()
        if error:
            acc["last_error"] = str(error)[:200]
        self.save()

    def to_public_list(self) -> List[dict]:
        """产出供前端展示的安全数据字典列表。"""
        out = []
        for acc in self.accounts:
            tinfo = parse_token_info(acc.get("cookie", ""))
            out.append({
                "uid": acc["uid"],
                "email": acc.get("email", ""),
                "name": acc.get("name", ""),
                "status": acc.get("status", "active"),
                "free_rate": acc.get("free_rate"),
                "five_hour_rate": acc.get("five_hour_rate"),
                "weekly_rate": acc.get("weekly_rate"),
                "plan_desc": acc.get("plan_desc", "免费版"),
                "pay_mode": acc.get("pay_mode", 0),
                "requests_count": acc.get("requests_count", 0),
                "tokens_count": acc.get("tokens_count", 0),
                "created_at": acc.get("created_at"),
                "last_used_at": acc.get("last_used_at", 0),
                "last_checked_at": acc.get("last_checked_at", 0),
                "last_error": acc.get("last_error"),
                "token_left_min": tinfo.get("left_min"),
                "token_expired": tinfo.get("expired", True),
                "cookie_masked": mask_cookie(acc.get("cookie", "")),
            })
        return out
