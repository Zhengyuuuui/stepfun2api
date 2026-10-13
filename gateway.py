"""
stepfun2api — 把 StepFun AI Studio 私有聊天协议还原为 OpenAI 兼容网关。

启动：
  python gateway.py                       # 读 config.json
  PORT=8391 API_KEY=sk-xx python gateway.py   # 环境变量覆盖

端点：
  GET  /v1/models            动态返回该账号可用 chat 模型
  POST /v1/chat/completions  OpenAI 格式（流式 + 非流式）
  GET  /healthz              健康检查

逆向依据见 sessions/2026-10-11-stepfun-studio/RECON.md（全部 curl 实测 Confirmed）。
"""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse


from stepfun_client import StepFunClient, StepFunError
from transcript import serialize_messages
from tools import (build_tool_prompt, parse_tool_calls,
                   detect_fabrication, detect_refusal,
                   CORRECTION, CORRECTION_REFUSAL)

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("stepfun2api")

# ============================================================
# Console 观测：请求指标 + 日志环形缓冲
# ============================================================
from collections import deque

METRICS = {
    "started_at": time.time(),
    "requests_total": 0,
    "requests_stream": 0,
    "tool_mode_requests": 0,
    "two_stage": 0,
    "fabrication_hits": 0,
    "refusal_hits": 0,
    "retries": 0,
    "errors": 0,
    "tokens_in": 0,
    "tokens_out": 0,
    "latency_ms_total": 0,
    "latency_n": 0,
    "by_model": {},
    "recent": deque(maxlen=50),
}
METRICS_LOCK = __import__("threading").Lock()


class _RingLogHandler(logging.Handler):
    def __init__(self, cap=300):
        super().__init__()
        self.buf = deque(maxlen=cap)
        self.seq = 0

    def emit(self, record):
        try:
            self.seq += 1
            self.buf.append({
                "id": self.seq,
                "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "msg": record.getMessage()[:400],
            })
        except Exception:
            pass


RING = _RingLogHandler()
log.addHandler(RING)
logging.getLogger("uvicorn.access").addHandler(RING)


def _bump(**kw):
    with METRICS_LOCK:
        for k, v in kw.items():
            if isinstance(v, (int, float)) and isinstance(METRICS.get(k), (int, float)):
                METRICS[k] = METRICS.get(k, 0) + v


def _bump_model(model: str):
    with METRICS_LOCK:
        METRICS["by_model"][model] = METRICS["by_model"].get(model, 0) + 1


def _record_req(model: str, stream: bool, tool_mode: bool, usage: dict, ms: float, err: bool):
    with METRICS_LOCK:
        METRICS["requests_total"] += 1
        if stream:
            METRICS["requests_stream"] += 1
        if tool_mode:
            METRICS["tool_mode_requests"] += 1
        if err:
            METRICS["errors"] += 1
        METRICS["by_model"][model] = METRICS["by_model"].get(model, 0) + 1
        METRICS["latency_ms_total"] += ms
        METRICS["latency_n"] += 1
        u = usage or {}
        METRICS["tokens_in"] += int(u.get("prompt_tokens") or 0)
        METRICS["tokens_out"] += int(u.get("completion_tokens") or 0)
        METRICS["recent"].appendleft({
            "ts": time.strftime("%H:%M:%S"), "model": model,
            "stream": stream, "tool": tool_mode, "ms": round(ms),
            "err": err, "tok": int((usage or {}).get("total_tokens") or 0),
        })


def _token_status_now() -> dict:
    try:
        from refresh_cookie import token_status
    except Exception:
        return {}
    ck = CONFIG.get("cookie") or ""
    from stepfun_client import parse_cookie
    val = parse_cookie(ck).get("Oasis-Token", "")
    left, life, expired = token_status(val) if val else (None, None, None)
    return {"left_min": round(left, 1) if left is not None else None,
            "life_h": round(life, 1) if life is not None else None,
            "expired": expired}

# ============================================================
# 配置加载
# ============================================================
def load_config() -> dict:
    cfg = {}
    path = os.environ.get("CONFIG", "config.json")
    if os.path.exists(path):
        try:
            cfg = json.load(open(path, encoding="utf-8"))
            log.info("已加载配置 %s", path)
        except Exception as e:
            log.error("配置 %s 解析失败: %s", path, e)

    def env(k, default):
        # 环境变量优先（大写），其次 config.json（小写键），最后 default
        v = os.environ.get(k.upper())
        if v in (None, ""):
            v = cfg.get(k.lower(), default)
        return v if v not in (None, "") else default

    def env_bool(k, default):
        v = env(k, default)
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("1", "true", "yes", "on")

    # api_keys 支持数组或逗号分隔字符串
    raw_keys = env("API_KEYS", "")
    if isinstance(raw_keys, str):
        api_keys = [k for k in raw_keys.split(",") if k.strip()]
    else:
        api_keys = [str(k).strip() for k in raw_keys if str(k).strip()]

    return {
        "host": env("HOST", "0.0.0.0"),
        "port": int(env("PORT", 8390)),
        "api_keys": api_keys,
        "cookie": env("COOKIE", "") or _read_cookie_file(env("COOKIE_FILE", "")),
        "cookie_file": env("COOKIE_FILE", "cookie.txt"),
        "proxy": env("PROXY", None),
        "default_model": env("DEFAULT_MODEL", "step-5-preview"),
        "language": env("LANGUAGE", "en"),
        "log_requests": env_bool("LOG_REQUESTS", True),
        "auto_delete_conversation": env_bool("AUTO_DELETE", True),
        "request_timeout": float(env("REQUEST_TIMEOUT", 300)),
        # 采样参数默认值（可被请求覆盖）
        "default_temperature": float(env("DEFAULT_TEMPERATURE", 1.0)),
        "default_top_p": float(env("DEFAULT_TOP_P", 0.95)),
        "default_max_tokens": int(env("DEFAULT_MAX_TOKENS", 8000)),
        "default_thinking": env_bool("DEFAULT_THINKING", True),
        "default_effort": env("DEFAULT_EFFORT", "medium"),
        # 工具模式：模型谎报"已完成"却没发 JSON 时，自动纠正重试的最大次数
        "tool_max_retries": int(env("TOOL_MAX_RETRIES", 2)),
    }


def _read_cookie_file(path: str) -> str:
    if path and os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    return ""


CONFIG = load_config()
if not CONFIG["cookie"]:
    log.warning("未配置 cookie！请用 COOKIE_FILE 或 config.json 提供 studio.stepfun.ai 的 cookie")

_client: Optional["StepFunClient"] = None
_watcher: Optional["asyncio.Task"] = None


async def _cookie_watcher():
    """轮询 cookie.txt，mtime/内容变化时热更新到 client（免重启）。"""
    import asyncio
    path = CONFIG.get("cookie_file") or "cookie.txt"
    if not path or not os.path.exists(path):
        log.info("cookie 热重载：未找到 %s，跳过监视", path)
        return
    last_mtime = 0.0
    log.info("cookie 热重载：监视 %s（每 5s）", path)
    while True:
        try:
            await asyncio.sleep(5)
            if not os.path.exists(path):
                continue
            mt = os.path.getmtime(path)
            if mt == last_mtime:
                continue
            last_mtime = mt
            new = open(path, encoding="utf-8").read().strip()
            if not new:
                continue
            cli = get_client()
            if cli.update_cookie(new):
                CONFIG["cookie"] = new
                log.info("已从 %s 热重载 cookie", path)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("cookie 监视异常（忽略）: %s", e)


@asynccontextmanager
async def _lifespan(_app):
    import asyncio
    global _watcher
    _watcher = asyncio.create_task(_cookie_watcher())
    yield
    if _watcher:
        _watcher.cancel()
    if _client:
        await _client.aclose()


import os
# ...
app = FastAPI(title="stepfun2api", docs_url=None, redoc_url=None, lifespan=_lifespan)
base_dir = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(base_dir, "static")), name="static")

@app.get("/")
async def root():
    return HTMLResponse(open(os.path.join(base_dir, "static", "index.html"), encoding="utf-8").read())

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def get_client() -> StepFunClient:
    global _client
    if _client is None:
        _client = StepFunClient(
            cookie=CONFIG["cookie"], proxy=CONFIG["proxy"],
            timeout=CONFIG["request_timeout"], language=CONFIG["language"],
        )
    return _client


# ============================================================
# 鉴权
# ============================================================
def check_auth(request: Request) -> Optional[JSONResponse]:
    if not CONFIG["api_keys"]:
        return None  # 未配置 = 不鉴权（本地用）
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    if key not in CONFIG["api_keys"]:
        return _openai_err("invalid_api_key", "Incorrect API key provided", 401)
    return None


def _openai_err(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "invalid_request_error",
                           "param": None, "code": code}},
    )


# ============================================================
# 参数映射：OpenAI -> StepFun config
# ============================================================
EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high"}


def build_stepfun_config(req: dict) -> dict:
    """OpenAI 请求参数 -> StepFun chat.config（字段名与实测请求体一致）。"""
    cfg = CONFIG
    temperature = req.get("temperature", cfg["default_temperature"])
    top_p = req.get("top_p", cfg["default_top_p"])
    max_tokens = req.get("max_tokens") or req.get("max_completion_tokens") or cfg["default_max_tokens"]

    # thinking / effort：支持 OpenAI 的 reasoning_effort，或 extra_body.thinking
    effort = req.get("reasoning_effort") or cfg["default_effort"]
    effort = EFFORT_MAP.get(str(effort).lower(), "medium")
    thinking = req.get("thinking", cfg["default_thinking"])
    if isinstance(thinking, str):
        thinking = thinking.lower() in ("1", "true", "yes")

    return {
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "top_p": float(top_p),
        "frequency_penalty": float(req.get("frequency_penalty", 0)),
        "thinking": bool(thinking),
        "effort": effort,
    }


# ============================================================
# GET /v1/models
# ============================================================
@app.get("/v1/models")
async def list_models(request: Request):
    err = check_auth(request)
    if err:
        return err
    try:
        cfg = await get_client().get_playground_config()
    except StepFunError as e:
        return _openai_err(e.code, e.message, e.status)
    except Exception as e:
        return _openai_err("upstream_error", str(e), 502)

    now = int(time.time())
    data = []
    for m in cfg.get("chat", {}).get("models", []):
        mid = m.get("model")
        if mid:
            data.append({"id": mid, "object": "model", "created": now, "owned_by": "stepfun"})
    return JSONResponse({"object": "list", "data": data})


# ============================================================
# POST /v1/chat/completions
# ============================================================
_WEB_SEARCH_TOOL_NAMES = {"web_search", "websearch", "web-search"}
import re
_SEARCH_INTENT_RE = re.compile(
    r"(联网|在网上|搜索引擎|搜[索一下]{1,2}今[天日]|搜[索]?最[新近]|搜索|查一下今天|查最新"
    r"|web\s*search|search\s*the\s*web|search\s*online|latest\s*news|news\s*(today|now)"
    r"|今天的?新闻|最新(新闻|消息|资讯)|新闻)", re.I)


def _is_web_search_tool(t: dict) -> bool:
    if not isinstance(t, dict):
        return False
    if t.get("type") == "web_search":
        return True
    fn = t.get("function") or {}
    name = (fn.get("name") or t.get("name") or "").lower()
    return name in _WEB_SEARCH_TOOL_NAMES


def _search_intent(messages: list) -> bool:
    """最后一条 user 消息含联网搜索意图 -> 自动启用上游原生 web_search。"""
    for m in reversed(messages):
        if (m.get("role") or "").lower() == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return bool(c and _SEARCH_INTENT_RE.search(str(c)))
    return False


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    err = check_auth(request)
    if err:
        return err
    try:
        req = await request.json()
    except Exception:
        return _openai_err("invalid_request", "request body must be JSON", 400)

    messages = req.get("messages")
    if not messages or not isinstance(messages, list):
        return _openai_err("invalid_request", "messages is required", 400)

    model = req.get("model") or CONFIG["default_model"]
    stream = bool(req.get("stream"))
    sf_config = build_stepfun_config(req)
    web_search = bool(req.get("web_search") or req.get("enable_web_search")
                      or req.get("web_search_options"))

    # 工具层：tools[] -> 协议指令注入 transcript；解析模型 JSON -> 标准 tool_calls
    tools = req.get("tools") or None
    # OpenAI 风格内置搜索工具（{"type":"web_search"} 或同名 function）-> 走上游原生搜索，剔除同名工具
    if tools:
        if any(_is_web_search_tool(t) for t in tools):
            web_search = True
            tools = [t for t in tools if not _is_web_search_tool(t)] or None
    tool_choice = req.get("tool_choice")
    has_tools = bool(tools)
    last_role = (messages[-1].get("role") or "").lower() if messages else None
    if not web_search and not last_role == "tool" and _search_intent(messages):
        web_search = True
        log.info("检测到联网搜索意图，自动启用上游原生 web_search")
    tool_prompt = build_tool_prompt(tools, tool_choice, last_role) if has_tools else None
    content = serialize_messages(messages, tool_prompt=tool_prompt)

    # 有工具时强制开 thinking：实测 step-5-preview+thinking 才能可靠遵循工具协议
    if has_tools and not req.get("thinking") is False:
        sf_config["thinking"] = True

    if CONFIG["log_requests"]:
        log.info("chat model=%s stream=%s msgs=%d chars=%d thinking=%s effort=%s tools=%d web_search=%s",
                 model, stream, len(messages), len(content),
                 sf_config["thinking"], sf_config["effort"], len(tools) if has_tools else 0,
                 web_search)

    client = get_client()
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    # 声明的有效工具名：用于把模型「工具名幻觉」(file_write 等) 映射回真实名
    tool_names = [((t.get("function") or t).get("name") or "") for t in (tools or [])]
    tool_names = [n for n in tool_names if n]

    # 两段式编排：客户端带模拟工具 + 搜索意图时，单轮里「原生搜索」与「注入的JSON工具协议」
    # 互相干扰（模型会假调 web_search / 谎称没联网工具）。改为：
    #   第一段=中立纯检索（无 agent 人设动作），拿真实搜索结果；
    #   第二段=注入结果后进工具模式，强制只发 tool_calls JSON。
    if web_search and has_tools:
        log.info("两段式：先原生搜索，后工具模式")
        _bump(two_stage=1)
        ctx = serialize_messages(messages)
        plain_first = ctx + (
            "\n\n<system>\n联网检索任务（与对话协议无关，只输出事实）："
            "请用联网搜索获取上条用户请求所需的事实信息（新闻/数据/日期等），"
            "只输出检索到的事实要点与来源URL。"
            "不要声称已将任何内容写入文件，不要执行任何动作，不要寒暄。\n</system>")
        try:
            res = await _collect_attempt(client, plain_first, model, sf_config, True)
        except StepFunError as e:
            return _openai_err(e.code, e.message, e.status)
        digest = (res.get("text") or "").strip()
        if digest and len(digest) >= 80:
            content = (content.rstrip()
                       + "\n\n<web_search_results>\n" + digest[:6000]
                       + "\n</web_search_results>\n"
                       + "The live web search results above were ALREADY fetched by the "
                         "system and are factual. Do NOT re-search and do NOT claim you "
                         "searched or already wrote anything. Your next reply MUST be ONLY "
                         "a tool_calls JSON using ONLY the tools listed in the protocol "
                         "(exact names), incorporating the material above.")
        else:
            log.warning("两段式第一段无有效结果(len=%d)，退回单轮原生搜索", len(digest))
            content = ctx
            web_search = True
            has_tools = False
            tool_prompt = None

    if stream:
        return StreamingResponse(
            _metered_stream(
                _stream_response(client, completion_id, created, model, content, sf_config,
                                 web_search, has_tools, last_role, tool_names),
                model, has_tools, time.time()),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )
    else:
        t0 = time.time()
        resp = await _non_stream_response(client, completion_id, created, model, content,
                                          sf_config, web_search, has_tools, last_role,
                                          tool_names)
        usage = {}
        try:
            body = json.loads(resp.body)
            usage = body.get("usage") or {}
            err = "error" in body
        except Exception:
            err = resp.status_code != 200
        _record_req(model, False, has_tools, usage, (time.time() - t0) * 1000, err)
        return resp


async def _metered_stream(gen, model: str, tool_mode: bool, t0: float):
    usage = {}
    err = False
    try:
        async for piece in gen:
            if piece.startswith("data: ") and '"usage"' in piece:
                try:
                    j = json.loads(piece[6:])
                    if j.get("usage"):
                        usage = j["usage"]
                except Exception:
                    pass
            if '"error"' in piece:
                err = True
            yield piece
    finally:
        _record_req(model, True, tool_mode, usage, (time.time() - t0) * 1000, err)


async def _run_stream(client, conversation_id, content, model, sf_config, web_search):
    """驱动上游 SSE，yield 原始事件 dict；结束后按配置清理会话。"""
    try:
        async for ev in client.stream_chat(conversation_id, content, model, sf_config,
                                           enable_web_search=web_search):
            yield ev
    finally:
        if CONFIG["auto_delete_conversation"]:
            await client.delete_conversation(conversation_id)


async def _collect_attempt(client, content, model, sf_config, web_search) -> dict:
    """跑完整一轮上游流（自建并清理会话），收集 reasoning/text/usage。工具模式与重试共用。"""
    conversation_id = await client.create_conversation()
    reasoning_parts, text_parts = [], []
    usage = {}
    try:
        async for ev in _run_stream(client, conversation_id, content, model, sf_config, web_search):
            t = ev.get("type")
            d = ev.get("data", {}) or {}
            if t == "reasoning_delta":
                reasoning_parts.append(d.get("delta", ""))
            elif t == "text_delta":
                text_parts.append(d.get("delta", ""))
            elif t == "final":
                usage = _map_usage(d.get("usage", {}) or {})
    finally:
        pass
    return {"reasoning": "".join(reasoning_parts), "text": "".join(text_parts), "usage": usage}


def _build_retry_content(content: str, bad_text: str, kind: str = "fabrication") -> str:
    """谎报/拒绝重试：把模型上一轮的错误输出作为 <assistant> 回放 + 追加纠正指令。"""
    bad = (bad_text or "").strip()[:1500]
    corr = CORRECTION_REFUSAL if kind == "refusal" else CORRECTION
    return (content.rstrip()
            + f"\n\n<assistant>\n{bad}\n</assistant>"
            + corr)


async def _tool_mode_attempts(client, content, model, sf_config, web_search, last_role,
                              tool_names=None):
    """
    工具模式核心：最多 1+tool_max_retries 轮。
    每轮解析 tool_calls；若解析不出且检测到「谎报完成」或「谎称工具不可用而拒绝」，
    且本轮非 tool_result 回传，则带对应纠正指令重试。
    返回 (calls|None, text, reasoning, usage, finish_reason)。
    """
    max_attempts = 1 + max(0, CONFIG.get("tool_max_retries", 2))
    content_try = content
    last = {"text": "", "reasoning": "", "usage": {}}
    for attempt in range(max_attempts):
        last = await _collect_attempt(client, content_try, model, sf_config, web_search)
        text = last["text"]
        calls = parse_tool_calls(text, tool_names)
        if calls:
            return calls, text, last["reasoning"], last["usage"], "tool_calls"
        # 解析不出工具调用：判断是谎报完成还是谎称工具不可用而拒绝
        is_tool_result_turn = (last_role == "tool")
        kind = None
        if detect_fabrication(text):
            kind = "fabrication"
        elif detect_refusal(text):
            kind = "refusal"
        if (attempt < max_attempts - 1 and not is_tool_result_turn and kind):
            log.warning("工具模式%s检测命中，纠正重试 %d/%d（原文前40字: %r）",
                        "谎报" if kind == "fabrication" else "拒绝",
                        attempt + 1, max_attempts - 1, text[:40])
            _bump(**{"fabrication_hits" if kind == "fabrication" else "refusal_hits": 1,
                     "retries": 1})
            content_try = _build_retry_content(content_try, text, kind)
            continue
        # 正常纯文本或已达重试上限
        return None, text, last["reasoning"], last["usage"], "stop"
    return None, last["text"], last["reasoning"], last["usage"], "stop"


async def _non_stream_response(client, cid_prefix, created, model, content, sf_config,
                               web_search, has_tools=False, last_role=None, tool_names=None):
    try:
        if has_tools:
            # 工具模式：带谎报检测重试的收集式处理
            calls, full_text, reasoning, usage, finish_reason = await _tool_mode_attempts(
                client, content, model, sf_config, web_search, last_role, tool_names)
            if calls:
                message = {"role": "assistant", "content": None, "tool_calls": calls}
                if reasoning:
                    message["reasoning_content"] = reasoning
                return JSONResponse({
                    "id": cid_prefix, "object": "chat.completion", "created": created, "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
                    "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })
            message = {"role": "assistant", "content": full_text}
            if reasoning:
                message["reasoning_content"] = reasoning
            return JSONResponse({
                "id": cid_prefix, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        # 普通模式：单轮收集
        conversation_id = await client.create_conversation()
        reasoning_parts, text_parts = [], []
        usage = {}
        finish_reason = "stop"
        async for ev in _run_stream(client, conversation_id, content, model, sf_config, web_search):
            t = ev.get("type")
            d = ev.get("data", {}) or {}
            if t == "reasoning_delta":
                reasoning_parts.append(d.get("delta", ""))
            elif t == "text_delta":
                text_parts.append(d.get("delta", ""))
            elif t == "final":
                usage = _map_usage(d.get("usage", {}) or {})
    except StepFunError as e:
        return _openai_err(e.code, e.message, e.status)
    except Exception as e:
        log.exception("non-stream error")
        return _openai_err("upstream_error", str(e), 502)

    message = {"role": "assistant", "content": "".join(text_parts)}
    reasoning = "".join(reasoning_parts)
    if reasoning:
        message["reasoning_content"] = reasoning
    if not usage:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    return JSONResponse({
        "id": cid_prefix, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    })


async def _stream_response(client, completion_id, created, model, content, sf_config,
                           web_search, has_tools=False, last_role=None, tool_names=None):
    def chunk(delta: dict, finish: Optional[str] = None, usage: Optional[dict] = None) -> str:
        obj = {
            "id": completion_id, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage:
            obj["usage"] = usage
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    # ---------- 工具模式：收集式（谎报检测需完整输出，重试会重复，故不能边流边发） ----------
    if has_tools:
        yield chunk({"role": "assistant", "content": ""})
        try:
            calls, text, reasoning, usage, finish_reason = await _tool_mode_attempts(
                client, content, model, sf_config, web_search, last_role, tool_names)
        except StepFunError as e:
            yield f"data: {json.dumps({'error': {'message': e.message, 'code': e.code}})}\n\n"
            yield "data: [DONE]\n\n"
            return
        except Exception as e:
            log.exception("stream tool-mode error")
            yield f"data: {json.dumps({'error': {'message': str(e), 'code': 'upstream_error'}})}\n\n"
            yield "data: [DONE]\n\n"
            return
        if reasoning:
            yield chunk({"reasoning_content": reasoning})
        if calls:
            for idx, c in enumerate(calls):
                first = {"index": idx, "id": c["id"], "type": "function",
                         "function": {"name": c["function"]["name"], "arguments": ""}}
                yield chunk({"tool_calls": [first]})
                yield chunk({"tool_calls": [{"index": idx,
                                             "function": {"arguments": c["function"]["arguments"]}}]})
            finish_reason = "tool_calls"
        elif text:
            yield chunk({"content": text})
        yield chunk({}, finish=finish_reason, usage=usage or None)
        yield "data: [DONE]\n\n"
        return

    # ---------- 普通模式：实时流式 ----------
    try:
        conversation_id = await client.create_conversation()
    except StepFunError as e:
        yield f"data: {json.dumps({'error': {'message': e.message, 'code': e.code}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    yield chunk({"role": "assistant", "content": ""})

    usage = {}
    try:
        async for ev in _run_stream(client, conversation_id, content, model, sf_config, web_search):
            t = ev.get("type")
            d = ev.get("data", {}) or {}
            if t == "reasoning_delta":
                rd = d.get("delta", "")
                if rd:
                    yield chunk({"reasoning_content": rd})
            elif t == "text_delta":
                delta = d.get("delta", "")
                if delta:
                    yield chunk({"content": delta})
            elif t == "final":
                usage = _map_usage(d.get("usage", {}) or {})
    except StepFunError as e:
        yield f"data: {json.dumps({'error': {'message': e.message, 'code': e.code}})}\n\n"
        yield "data: [DONE]\n\n"
        return
    except Exception as e:
        log.exception("stream error")
        yield f"data: {json.dumps({'error': {'message': str(e), 'code': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    yield chunk({}, finish="stop", usage=usage or None)
    yield "data: [DONE]\n\n"


def _map_usage(u: dict) -> dict:
    def i(x):
        try:
            return int(x)
        except Exception:
            return 0
    prompt = i(u.get("tokens_in"))
    completion = i(u.get("tokens_out"))
    out = {"prompt_tokens": prompt, "completion_tokens": completion,
           "total_tokens": prompt + completion}
    cached = i(u.get("cached_tokens"))
    if cached:
        out["prompt_tokens_details"] = {"cached_tokens": cached}
    return out


# ============================================================
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "cookie_loaded": bool(CONFIG["cookie"]),
            "default_model": CONFIG["default_model"]}


# ============================================================
# Console 管理 API（用网关 api_key 鉴权）
# ============================================================
@app.get("/v1/console/quota")
async def console_quota(request: Request):
    err = check_auth(request)
    if err:
        return err
    client = get_client()
    out = {}
    try:
        out["quota"] = await client.get_quota()
    except StepFunError as e:
        out["quota_error"] = e.message
    try:
        out["plan"] = await client.get_plan_status()
        out["user"] = await client.get_user_info()
    except StepFunError as e:
        out["plan_error"] = e.message
    return JSONResponse(out)


@app.get("/v1/console/overview")
async def console_overview(request: Request):
    err = check_auth(request)
    if err:
        return err
    with METRICS_LOCK:
        snap = {k: v for k, v in METRICS.items() if k not in ("recent",)}
        recent = list(METRICS["recent"])[:20]
    ts = _token_status_now()
    import asyncio
    loop = asyncio.get_event_loop()
    cookie_age = None
    try:
        path = CONFIG.get("cookie_file") or "cookie.txt"
        if os.path.exists(path):
            cookie_age = int(time.time() - os.path.getmtime(path))
    except Exception:
        pass
    return JSONResponse({
        "metrics": snap,
        "recent": recent,
        "uptime_s": int(time.time() - METRICS["started_at"]),
        "avg_latency_ms": round(snap["latency_ms_total"] / snap["latency_n"]) if snap["latency_n"] else 0,
        "token": ts,
        "cookie_age_s": cookie_age,
        "config": {
            "default_model": CONFIG["default_model"],
            "port": CONFIG["port"],
            "tool_max_retries": CONFIG.get("tool_max_retries"),
            "auto_delete_conversation": CONFIG["auto_delete_conversation"],
            "api_key_configured": bool(CONFIG["api_keys"]),
        },
    })


@app.get("/v1/console/logs")
async def console_logs(request: Request, since: int = 0, limit: int = 100):
    err = check_auth(request)
    if err:
        return err
    rows = [r for r in RING.buf if r["id"] > since]
    return JSONResponse({"logs": rows[-max(1, min(limit, 300)):]})


@app.get("/v1/console/conversations")
async def console_conversations(request: Request):
    err = check_auth(request)
    if err:
        return err
    try:
        d = await get_client()._rpc("ListConversations", {"cursor": "", "limit": 30})
    except StepFunError as e:
        return _openai_err(e.code, e.message, e.status)
    items = d.get("items") or []
    return JSONResponse({"conversations": [
        {"id": str(x.get("conversation_id")), "title": x.get("title"),
         "last_message_at": x.get("last_message_at")} for x in items]})


async def _run_refresh_script(*flags: str, timeout: int = 60):
    import asyncio as _a
    import subprocess as _sp
    script = os.path.join(base_dir, "refresh_cookie.py")
    try:
        proc = await _a.create_subprocess_exec(
            "python3", script, *flags, cwd=base_dir,
            stdout=_sp.PIPE, stderr=_sp.PIPE)
        out, outerr = await _a.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace").strip(), \
            outerr.decode("utf-8", "replace").strip()
    except Exception as e:
        return -1, "", str(e)


@app.post("/v1/console/refresh-cookie")
async def console_refresh_cookie(request: Request):
    err = check_auth(request)
    if err:
        return err
    before = _token_status_now()
    rc, out, outerr = await _run_refresh_script("--force-rotate")
    # 强刷后主动热更新（不依赖 watcher 轮询间隔）
    try:
        path = CONFIG.get("cookie_file") or "cookie.txt"
        new = open(path, encoding="utf-8").read().strip()
        if new:
            get_client().update_cookie(new)
            CONFIG["cookie"] = new
    except Exception:
        pass
    after = _token_status_now()
    bl, al = before.get("left_min"), after.get("left_min")
    if rc != 0:
        state, note = "failed", "刷新脚本执行失败（浏览器未开/未登录？）"
    elif al is not None and bl is not None and al > bl + 1:
        state, note = "rotated", "SPA 已换发新 token（原 token 已过期/临期）"
    elif al is not None and al <= 0:
        state, note = "expired", "浏览器 token 也已过期且 SPA 未换发——请在打开的 studio 页面里操作一下或重新登录"
    else:
        state, note = "synced", ("浏览器与 cookie.txt 已同步；当前 token 仍在有效期内，"
                                 "上游只会在真过期瞬间换发新 token（launchd 每 60s 自动兜底）")
    return JSONResponse({
        "ok": rc == 0, "state": state, "note": note,
        "before": before, "token": after,
        "stdout": out[-500:], "stderr": outerr[-500:],
    })


@app.post("/v1/console/open-login")
async def console_open_login(request: Request):
    err = check_auth(request)
    if err:
        return err
    rc, out, outerr = await _run_refresh_script("--open-login", timeout=15)
    try:
        data = json.loads(out or "{}")
    except Exception:
        data = {}
    return JSONResponse({"ok": rc == 0 and data.get("ok", False), **data,
                         "stderr": outerr[-300:]})


@app.get("/v1/console/login-status")
async def console_login_status(request: Request):
    err = check_auth(request)
    if err:
        return err
    rc, out, outerr = await _run_refresh_script("--status", timeout=15)
    try:
        data = json.loads(out or "{}")
    except Exception:
        data = {"ok": False, "error": outerr or out or "no output"}
    return JSONResponse(data)


@app.post("/v1/console/capture-cookie")
async def console_capture_cookie(request: Request):
    """登录完成后直接抓浏览器 cookie 覆盖 cookie.txt（不触发轮换）。"""
    err = check_auth(request)
    if err:
        return err
    rc, out, outerr = await _run_refresh_script("--no-rotate")
    if rc != 0:
        return JSONResponse({"ok": False, "stdout": out[-300:], "stderr": outerr[-300:]},
                            status_code=502)
    try:
        path = CONFIG.get("cookie_file") or "cookie.txt"
        new = open(path, encoding="utf-8").read().strip()
        if new:
            get_client().update_cookie(new)
            CONFIG["cookie"] = new
    except Exception:
        pass
    return JSONResponse({"ok": True, "token": _token_status_now(), "stdout": out[-300:]})


if __name__ == "__main__":
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"], log_level="info")
