"""
工具转换器：把 OpenAI function-calling 协议「模拟」到只吃纯文本 content 的 StepFun 上游。

为什么需要：
  上游 /studio/stream 端点只接受单条 content 字符串，没有原生 tools/function 协议。
  但 step-5-preview(+thinking) 实测能可靠遵循「输出工具调用 JSON」的提示词协议。
  所以本层：
    1) 把 OpenAI tools[] schema 渲染成严格协议指令，注入 transcript（build_tool_prompt）
    2) 解析模型输出的 JSON，还原成标准 OpenAI tool_calls（parse_tool_calls）
    3) 流式时按首字符启发式缓冲，判定是工具调用还是普通文本（is_tool_candidate）

一套标准实现即可对接所有遵循 OpenAI 协议的 agent（Cline/Continue/Roo/LangChain/OpenAI SDK…），
无需为每个 agent app 单独适配。
"""

import json
import logging
import re
import uuid
from typing import Optional

log = logging.getLogger("stepfun2api.tools")

_FENCE_OPEN = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n?")
_FENCE_CLOSE = re.compile(r"\n?```\s*$")


def _compact_tool_lines(tools: list) -> tuple[list, list]:
    """把 tools[] 渲染成紧凑单行签名。返回 (行列表, 工具名列表)。

    实测：多工具(11个)时冗长的 JSON Schema 渲染会淹没协议指令，导致模型谎报完成。
    紧凑单行 + few-shot 示例能把遵循率从 ~2/4 提到 4/4。
    """
    lines, names = [], []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        names.append(name)
        desc = (fn.get("description") or "").strip().replace("\n", " ")
        params = fn.get("parameters") or fn.get("params") or {}
        props = (params.get("properties") if isinstance(params, dict) else None) or {}
        required = set(params.get("required") or []) if isinstance(params, dict) else set()
        sig = []
        for pk, pv in props.items():
            ptype = pv.get("type", "any") if isinstance(pv, dict) else "any"
            mark = "" if pk in required else "?"
            sig.append(f"{pk}:{ptype}{mark}")
        lines.append(f"- {name}({', '.join(sig)}): {desc[:100]}")
    return lines, names


def _example_for(tools: list) -> str:
    """用第一个工具构造 few-shot 正例 JSON。"""
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) and t.get("type") == "function" else (t if isinstance(t, dict) else None)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters") or {}
        props = (params.get("properties") if isinstance(params, dict) else None) or {}
        args = {k: f"<{v.get('type','value')}>" for k, v in props.items()} or {}
        return json.dumps({"tool_calls": [{"name": fn["name"], "arguments": args}]}, ensure_ascii=False)
    return '{"tool_calls": [{"name": "<tool>", "arguments": {}}]}'


# ============================================================
# 1) tools[] -> 协议指令（注入 transcript 末尾）
# ============================================================
def build_tool_prompt(tools: list, tool_choice=None, last_role: Optional[str] = None) -> Optional[str]:
    """把 OpenAI tools schema 渲染成严格的工具调用协议指令。tools 为空返回 None。

    结构（经 A/B 实测，11 工具场景遵循率 4/4）：
      协议规则 → 紧凑工具签名 → 反幻觉 reminder → few-shot 正反例 → 末尾 NOW 直接指令
    last_role: 对话最后一条消息角色，用于生成条件式 NOW 指令（避免纯聊天时误触发）。
    """
    if not tools:
        return None
    lines, names = _compact_tool_lines(tools)
    if not lines:
        return None

    # tool_choice -> 附加约束 + NOW 指令
    forced = None
    must_call = False
    forbid_call = False
    if isinstance(tool_choice, str):
        if tool_choice in ("required", "any"):
            must_call = True
        elif tool_choice == "none":
            forbid_call = True
    elif isinstance(tool_choice, dict):
        f = tool_choice.get("function", {})
        forced = f.get("name") if isinstance(f, dict) else None
        if forced:
            must_call = True

    example = _example_for(tools)
    tool_block = "\n".join(lines)
    availability = ("All tools listed below ARE connected and available in this session "
                    "through an external executor — they are real. The executor can actually "
                    "run shell commands, read/write/edit files, search, and fetch the web, "
                    "even though you cannot execute anything by yourself.")

    # 末尾 NOW 直接指令（recency 最强位）——条件式，避免 over-trigger
    if forbid_call:
        now = "NOW: do NOT call any tool this turn. Reply with plain text only."
    elif must_call:
        tgt = f' You MUST call the tool named "{forced}".' if forced else " You MUST call at least one tool."
        now = f"NOW: respond to the user's final turn.{tgt} Your ENTIRE reply must be ONLY the tool_calls JSON object — no prose."
    elif last_role == "tool":
        now = ("NOW: you just received <tool_result>(s) above. Either call another tool "
               "(reply with ONLY the JSON) or give your final plain-text answer to the user.")
    else:
        now = ("NOW: respond to the user's final turn. If it requires ANY tool action, your ENTIRE "
               "reply must be ONLY the tool_calls JSON object — never describe, narrate, or claim "
               "the action instead of emitting the JSON. If no tool is needed, reply with plain text.")

    return f"""TOOL-CALLING PROTOCOL (STRICT — your reply is machine-parsed):

You are an agent that can invoke tools, but you CANNOT act by yourself.
{availability}
1. To use tool(s), reply with ONLY a single JSON object — no prose, no markdown fences, no commentary:
   {{"tool_calls": [{{"name": "<tool_name>", "arguments": {{ ... }} }}]}}
2. "arguments" MUST be a JSON object matching that tool's params. Use ONLY tools listed below; never invent tools or fields.
3. To call MULTIPLE tools at once, put several entries in the array.
4. If NO tool is needed for the final turn, reply with PLAIN TEXT only (no JSON).

TOOLS (name(params): purpose — a "?" suffix on a param means optional):
{tool_block}

CRITICAL ANTI-HALLUCINATION RULES (violating these breaks the system):
- You have NO ability to write files, run commands, or access anything directly. The ONLY way an action happens is by emitting the tool_calls JSON; a real external executor performs it and returns a <tool_result>.
- NEVER claim an action is already done (e.g. "已创建", "已写入", "已完成", "created", "done") unless a <tool_result> confirming it appears in the transcript. Claiming completion without emitting the JSON is a fabrication and a PROTOCOL VIOLATION.
- NEVER claim these tools are unavailable, disabled, missing, or that you "have no command-line/file/web tools". That is false — they are connected and work. Refusing a task because "tools are unavailable" is ALSO a PROTOCOL VIOLATION. If the task needs an action, emit the tool_calls JSON and let the executor run it.

EXAMPLE (user asks to create hello.txt containing "hi"):
CORRECT reply: {example}
WRONG reply #1 (FORBIDDEN): "已创建 hello.txt" / "I have created the file" ← fabricating completion without a tool_call.
WRONG reply #2 (FORBIDDEN): "当前运行时没有可用的命令行工具，我无法完成" ← falsely claiming the tools are unavailable and giving up.

{now}""".strip()


# ============================================================
# 2) 模型输出 -> OpenAI tool_calls
# ============================================================
def _strip_fences(t: str) -> str:
    t = _FENCE_OPEN.sub("", t)
    t = _FENCE_CLOSE.sub("", t)
    return t.strip()


def _try_json(t: str):
    try:
        return json.loads(t)
    except Exception:
        return None


def _extract_json(t: str):
    """从文本里提取第一个平衡的 JSON 对象。"""
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return _try_json(t[start:i + 1])
    return None


def _coerce_args(args) -> str:
    """arguments 统一成 JSON 字符串（OpenAI 要求 function.arguments 是 str）。"""
    if args is None:
        return "{}"
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return "{}"
        # 已是 JSON 字符串则校验后原样返回，否则包一层
        if _try_json(s) is not None:
            return s
        return json.dumps({"value": args}, ensure_ascii=False)
    try:
        return json.dumps(args, ensure_ascii=False)
    except Exception:
        return "{}"


def _mk_call(name: str, args, valid_names: Optional[list] = None) -> dict:
    return {
        "id": "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {"name": _canon_name(name, valid_names), "arguments": _coerce_args(args)},
    }


# 模型偶发「工具名幻觉」：把 write 说成 file_write / write_file / save_file 等。
# 用「有效名是幻觉名子串」或「token 重叠」把幻觉名映射回有效名，避免客户端报 tool not found。
_NAME_NOISE = {"file", "files", "path", "content", "to", "a", "the", "new", "on", "in", "call", "tool"}


def _canon_name(name: str, valid_names: Optional[list]) -> str:
    if not valid_names or not isinstance(name, str) or name in valid_names:
        return name
    nl = name.lower()
    toks = {t for t in re.split(r"[^a-z0-9]+", nl) if t and t not in _NAME_NOISE}
    best, best_score = None, 0
    for v in valid_names:
        vl = v.lower()
        vtoks = {t for t in re.split(r"[^a-z0-9]+", vl) if t and t not in _NAME_NOISE}
        if vl == nl:
            return v
        score = 0
        if vl in nl or nl in vl:
            score = 60
        else:
            ov = len(toks & vtoks)
            if ov:
                score = ov * 20
        if score > best_score:
            best, best_score = v, score
    return best if best and best_score >= 60 else name


def _normalize(obj, valid_names: Optional[list] = None) -> list:
    """把各种可能的模型输出结构归一成 tool_calls 列表。"""
    calls = []
    if not isinstance(obj, dict):
        return calls
    # 形式 A: {"tool_calls":[{name,arguments} | {function:{name,arguments}}]}
    if isinstance(obj.get("tool_calls"), list):
        for tc in obj["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            name = fn.get("name")
            if name:
                calls.append(_mk_call(name, fn.get("arguments", fn.get("args")), valid_names))
        return calls
    # 形式 B: {"tool":name,"arguments":{...}} / {"name":..,"arguments":..}
    name = obj.get("tool") or obj.get("name") or obj.get("function")
    if isinstance(name, dict):
        name = name.get("name")
    if isinstance(name, str) and name:
        args = obj.get("arguments", obj.get("args"))
        calls.append(_mk_call(name, args, valid_names))
        return calls
    # 形式 C: {"function":{"name":..,"arguments":..}}
    fn = obj.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        calls.append(_mk_call(fn["name"], fn.get("arguments", fn.get("args")), valid_names))
    return calls


def parse_tool_calls(text: str, valid_names: Optional[list] = None) -> Optional[list]:
    """解析模型输出。返回 tool_calls 列表；不是工具调用则返回 None。

    valid_names: 本请求声明的工具名列表，用于把「工具名幻觉」映射回最接近的有效名。
    """
    if not text or not text.strip():
        return None
    t = _strip_fences(text.strip())
    obj = _try_json(t)
    if obj is None:
        obj = _extract_json(t)
    if obj is None:
        return None
    calls = _normalize(obj, valid_names)
    return calls or None


# ============================================================
# 3) 流式判定：这段缓冲像不像工具调用（决定是否继续缓冲）
# ============================================================
def is_tool_candidate(text: str) -> bool:
    """
    首字符启发式：以 { 或 ``` 开头 → 可能是工具调用 JSON，需缓冲后再判定。
    普通文本回答（含首字符非 { ）→ 立即当 content 流式输出。
    """
    t = text.lstrip()
    if not t:
        return True  # 还没内容，继续缓冲
    return t[0] == "{" or t.startswith("```")


# ============================================================
# 4) 谎报检测：模型声称"已完成"却没发 tool_calls JSON
# ============================================================
# 收紧原则（防误伤）：中文需「受事(文件/命令等)+完成类动词」紧邻或明确第一人称；
# 英文须有 I/we 主语或文件/工具作主语；不匹配泛指（"已完成功能的实现思路"等）。
_FAB_DONE = r"(创建|写入|保存|生成|执行|运行|修改|删除|更新|安装|存到|整理|写好|建好|弄好)"
_FAB_OBJ = r"(文件|文档|目录|报告|脚本|代码|配置|数据|清单|日报|速览|新闻|汇总|简报|md\b|txt\b|json\b)"

_FABRICATE_RE = re.compile(
    "("
    + r"已[^。\n]{0,20}(将|把)[^。\n]{0,40}?" + _FAB_DONE
    + r"|已[^。\n]{0,20}(将|把)[^。\n]{0,40}?" + _FAB_OBJ + r"[^。\n]{0,15}?" + _FAB_DONE
    + r"|已(成功)?\s*" + _FAB_DONE + r"(了|过)?[^。\n]{0,15}?" + _FAB_OBJ
    + r"|" + _FAB_OBJ + r"[^。\n]{0,6}已(成功|经)?\s*" + _FAB_DONE
    + r"|已" + _FAB_DONE + r"[。，.!?\s]"
    + r"|已完成[。，.!?\s]"
    + r"|并(且)?\s*(已)?" + _FAB_DONE + r"(了|过)"
    + r"|(i|we)\s+(have\s+|'ve\s+|just\s+)"
    + r"(successfully\s+)?(created|written|wrote|saved|completed|generated|executed|ran|done|put|prepared)"
    + r"|(the\s+)?(file|document|task|report)\s+has\s+been\s+(created|written|saved|completed|generated)"
    + r"|successfully\s+(created|wrote|written|saved|completed)\s+(the|this|a|`)"
    + ")",
    re.IGNORECASE,
)


def detect_fabrication(text: str) -> bool:
    """
    检测「谎报完成」：模型输出普通文本、却声称某动作已完成（而实际上它没发 tool_calls JSON）。
    仅在工具模式且本轮无 tool_result 回传时调用（避免误伤工具执行后的合法"已完成"总结）。
    """
    if not text:
        return False
    return bool(_FABRICATE_RE.search(text))


# ============================================================
# 5) 拒绝话术检测：模型谎称「工具不可用/没有命令行工具」而撒手
# ============================================================
_TOOLNOUN = r"(工具|命令行|命令|终端|sandbox|沙箱|联网|搜索|shell|terminal|command[-_ ]?line|web[-_ ]?search|bash)"

_REFUSAL_RE = re.compile(
    "("
    + "tool_unavailable"
    + r"|(没有|无|缺少|未提供|不存在|不可用|用不了|无法使用|不能用|未启用|被禁用|禁用|受限)"
    + r"[^。\n]{0,10}" + _TOOLNOUN
    + "|" + _TOOLNOUN + r"[^。\n]{0,10}"
    + r"(不可用|未启用|受限|被禁用|无法使用|都用不了|都没了|不存在)"
    + r"|((the\s+)?(tools?|functions?|cli|command[- ]?line|terminal|shell|web[- ]?search)"
    + r"[^。\n]{0,20}(are|is|'re|'s|be)\s+(?:(?:not|no|currently\s+not)\s+"
    + r"(?:available|usable|connected|enabled|working|present)"
    + r"|unavailable|disabled|missing|broken))"
    + r"|(i|we)\s+(don't|do\s+not|can't|cannot|haven't|have\s+not|lack)\s+"
    + r"(have\s+|any\s+|access\s+to\s+|the\s+)?[^。\n]{0,20}"
    + r"(tools?|functions?|bash|shell|terminal|command|web[- ]?search|network|internet)"
    + r"|(there\s+(is|are)\s+no|no\s+(available|usable))[^。\n]{0,15}"
    + r"(tools?|terminal|shell|command|bash|web[- ]?search)"
    + ")",
    re.IGNORECASE,
)


def detect_refusal(text: str) -> bool:
    """
    检测「拒绝话术」：模型谎称工具/联网能力不可用而撒手不干（而非谎报已完成）。
    同 detect_fabrication，仅在工具模式且本轮非 tool_result 回传时调用。
    """
    if not text:
        return False
    return bool(_REFUSAL_RE.search(text))


# 谎报后追加的强纠正指令（重试时用）
CORRECTION = (
    "\n\nCORRECTION — PROTOCOL VIOLATION DETECTED:\n"
    "In your previous attempt you claimed the action was already done WITHOUT emitting any "
    "tool_calls JSON. You CANNOT perform actions yourself; that claim was a fabrication. "
    "The file/command does NOT exist. Nothing was executed.\n"
    "Redo your reply NOW: your ENTIRE reply must be ONLY the tool_calls JSON object "
    "(no prose, no markdown fences, no explanation), so the external executor can actually "
    "perform the action."
)

CORRECTION_REFUSAL = (
    "\n\nCORRECTION — PROTOCOL VIOLATION DETECTED:\n"
    "In your previous attempt you claimed the tools were unavailable / that you cannot complete "
    "the task, and gave up. That claim is FALSE: the tools listed in the protocol above ARE "
    "connected and working through an external executor (shell, file read/write, web — all real).\n"
    "Redo your reply NOW: if the task needs an action, your ENTIRE reply must be ONLY the "
    "tool_calls JSON object (no prose, no apologies, no refusals), and let the executor run it. "
    "Only reply with plain text if the task can genuinely be answered without any action."
)
