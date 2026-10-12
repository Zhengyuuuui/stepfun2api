"""
StepFun Studio 协议客户端（逆向还原，全部 curl 实测 Confirmed）。

后端形态：
  - Connect-RPC (gRPC-Web JSON)：POST /api/step.openapi.devcenter.Dashboard/<Method>
  - 流式对话：POST /api/stdhttp/v1/studio/stream  (text/event-stream, Anthropic Messages 风格)

认证：cookie(Oasis-Token / Oasis-Webid / acw_tc) + oasis-* headers。
缺 oasis 头 → 401 {"code":"unauthenticated","message":"auth failed: oasis-token is embezzled"}

关键约束（实测）：
  - stream 端点只吃单条 content 字符串，不支持 messages 数组 / system_prompt 字段
  - 服务端按 conversation_id 自动累积历史（无状态透传需每请求新建会话 + transcript 序列化）
"""

import json
import logging
import re
from typing import AsyncIterator, Optional

import httpx

log = logging.getLogger("stepfun2api.client")

BASE = "https://studio.stepfun.ai"
DASHBOARD = "/api/step.openapi.devcenter.Dashboard/"
STREAM_PATH = "/api/stdhttp/v1/studio/stream"
APPID = "20700"

# SSE type -> OpenAI 语义
EVT_REASONING = "reasoning_delta"
EVT_TEXT = "text_delta"


class StepFunError(Exception):
    def __init__(self, message: str, status: int = 502, code: str = "upstream_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def parse_cookie(raw: str) -> dict:
    """把 cookie 字符串解析成 dict。"""
    out = {}
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class StepFunClient:
    def __init__(self, cookie: str, proxy: Optional[str] = None, timeout: float = 300.0,
                 language: str = "en"):
        self.cookie = cookie.strip()
        self.language = language
        ck = parse_cookie(self.cookie)
        # oasis-webid 头取自 cookie 里的 Oasis-Webid
        self.webid = ck.get("Oasis-Webid") or ck.get("oasis-webid") or ""
        if not ck.get("Oasis-Token"):
            log.warning("cookie 中未见 Oasis-Token，鉴权可能失败")
        if not self.webid:
            log.warning("cookie 中未见 Oasis-Webid，oasis-webid 头将为空")
        self._client = httpx.AsyncClient(
            base_url=BASE, timeout=timeout, proxy=proxy,
            headers={"User-Agent": _UA()}, follow_redirects=True,
        )

    async def aclose(self):
        await self._client.aclose()

    def update_cookie(self, cookie: str) -> bool:
        """热更新 cookie（免重启）。返回是否真的变了。"""
        new = cookie.strip()
        if not new or new == self.cookie:
            return False
        self.cookie = new
        ck = parse_cookie(new)
        self.webid = ck.get("Oasis-Webid") or ck.get("oasis-webid") or ""
        if not ck.get("Oasis-Token"):
            log.warning("热更新后的 cookie 中未见 Oasis-Token")
        log.info("cookie 已热更新（webid=%s...）", self.webid[:8] if self.webid else "空")
        return True

    # ---------- headers ----------
    def _rpc_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "connect-protocol-version": "1",
            "oasis-appid": APPID,
            "oasis-platform": "web",
            "oasis-language": self.language,
            "oasis-webid": self.webid,
            "Cookie": self.cookie,
            "Origin": BASE,
            "Referer": BASE + "/",
        }

    def _stream_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Oasis-appID": APPID,
            "Oasis-Platform": "web",
            "Oasis-Language": self.language,
            "Oasis-Webid": self.webid,
            "Cookie": self.cookie,
            "Origin": BASE,
            "Referer": BASE + "/",
        }

    # ---------- RPC ----------
    async def _rpc(self, method: str, body: dict) -> dict:
        r = await self._client.post(DASHBOARD + method, headers=self._rpc_headers(),
                                    content=json.dumps(body))
        return _check_json(r, method)

    async def get_playground_config(self) -> dict:
        return await self._rpc("GetStudioPlaygroundConfig", {})

    async def get_quota(self) -> dict:
        """账号额度/用量。实测字段：
        free_rate(免费额度剩余率0-1), five_hour_usage_left_rate, five_hour_usage_reset_time,
        weekly_usage_left_rate, weekly_usage_reset_time, credit_rate_limit(付费额度,无则null)。
        """
        return await self._rpc("QueryStudioRateLimit", {})

    async def get_plan_status(self) -> dict:
        """订阅计划状态。status=206 表示无有效订阅。"""
        return await self._rpc("GetStepPlanStatus", {})

    async def get_user_info(self) -> dict:
        return await self._rpc("UserInfo", {})

    async def list_models(self) -> list[str]:
        cfg = await self.get_playground_config()
        return [m["model"] for m in cfg.get("chat", {}).get("models", []) if m.get("model")]

    async def create_conversation(self) -> str:
        d = await self._rpc("CreateConversation", {})
        cid = d.get("conversation", {}).get("conversation_id")
        if not cid:
            raise StepFunError(f"CreateConversation 无 conversation_id: {d}", 502)
        return str(cid)

    async def delete_conversation(self, conversation_id: str) -> None:
        """尽力清理，失败不抛（best-effort）。"""
        try:
            await self._rpc("DeleteConversation", {"conversation_id": int(conversation_id)})
        except Exception as e:
            log.debug("delete_conversation(%s) 忽略失败: %s", conversation_id, e)

    # ---------- streaming ----------
    async def stream_chat(self, conversation_id: str, content: str, model: str,
                          config: dict, enable_web_search: bool = False,
                          asset_ids: Optional[list] = None) -> AsyncIterator[dict]:
        """
        POST /studio/stream，逐条 yield 解析后的 SSE data dict。
        body 结构（实测 Confirmed）：
        {conversation_id, kind:"chat", chat:{model:{model_id,config:{...}},
                                             content, asset_ids, enable_web_search}}
        """
        body = {
            "conversation_id": conversation_id,
            "kind": "chat",
            "chat": {
                "model": {"model_id": model, "config": config},
                "content": content,
                "asset_ids": asset_ids or [],
                "enable_web_search": enable_web_search,
            },
        }
        try:
            async with self._client.stream("POST", STREAM_PATH,
                                           headers=self._stream_headers(),
                                           content=json.dumps(body)) as resp:
                if resp.status_code != 200:
                    txt = (await resp.aread()).decode("utf-8", "replace")
                    raise StepFunError(_extract_err(txt, resp.status_code), resp.status_code)
                async for ev in _iter_sse(resp):
                    yield ev
        except httpx.HTTPError as e:
            raise StepFunError(f"stream 网络错误: {e}", 502)


# ---------- helpers ----------
def _UA() -> str:
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


def _check_json(r: httpx.Response, method: str) -> dict:
    ct = r.headers.get("content-type", "")
    txt = r.text
    if r.status_code == 401:
        raise StepFunError(_extract_err(txt, 401) or "oasis-token 失效，需重新登录导出 cookie", 401,
                           "invalid_credentials")
    if r.status_code != 200:
        raise StepFunError(_extract_err(txt, r.status_code), r.status_code)
    if "json" not in ct:
        raise StepFunError(f"{method} 非 JSON 响应: {txt[:200]}", 502)
    try:
        return r.json()
    except Exception:
        raise StepFunError(f"{method} JSON 解析失败: {txt[:200]}", 502)


def _extract_err(txt: str, status: int) -> str:
    try:
        o = json.loads(txt)
        return o.get("message") or o.get("error", {}).get("message") or txt[:300]
    except Exception:
        return (txt[:300] or f"HTTP {status}").strip()


async def _iter_sse(resp) -> AsyncIterator[dict]:
    """解析 StepFun SSE：event: message / data: {json}。yield data 的 json dict。"""
    async for line in resp.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            log.debug("跳过非 JSON SSE 行: %s", payload[:120])
