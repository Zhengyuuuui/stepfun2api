"""
把 OpenAI messages[] 序列化成 StepFun stream 端点能吃的单条 content transcript。

背景（实测 Confirmed）：
  stream 端点只接受单条 content 字符串，不支持 messages 数组，也没有独立 system_prompt 字段。
  但服务端按 conversation_id 累积历史。为做到「无状态 + 并发安全 + 零污染」，
  本网关每个请求新建一个 conversation，把完整对话序列化成带角色标签的 transcript 一次发出，
  用完即删。模型对 <system>/<user>/<assistant> 标签遵循良好（实测能遵守 system 指令并回忆历史）。

工具模式（见 tools.py）：
  当请求带 tools 时，会把「工具协议指令」作为 <system> 块前置注入；
  历史里的 assistant.tool_calls 会以与协议一致的 JSON 形式回放；
  role=tool 的消息以 <tool_result> 块回放。这样多轮工具调用循环可正常工作。
"""

import json
from typing import Any, Optional


def _flatten_content(content: Any) -> str:
    """content 可能是字符串，也可能是 OpenAI 的多模态 parts 数组。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    # StepFun 走 asset_ids 传图；纯文本 transcript 里标注占位
                    out.append("[image attached]")
                elif "text" in part:
                    out.append(str(part["text"]))
            else:
                out.append(str(part))
        return "\n".join(x for x in out if x)
    return str(content)


def _render_tool_calls(tool_calls: list) -> str:
    """把 assistant 历史里的 OpenAI tool_calls 渲染成协议一致的 JSON 字符串。"""
    arr = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name")
        if not name:
            continue
        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except Exception:
                args = {"value": raw_args}
        else:
            args = raw_args
        arr.append({"name": name, "arguments": args})
    if not arr:
        return ""
    return json.dumps({"tool_calls": arr}, ensure_ascii=False)


def serialize_messages(messages: list[dict], tool_prompt: Optional[str] = None) -> str:
    """
    messages -> transcript 字符串。

    规则：
      - tool_prompt 非空时作为首个 <system> 块注入（工具协议指令）
      - role=system    -> <system>...</system>
      - role=user      -> <user>...</user>
      - role=assistant -> <assistant>...</assistant>
          · 若带 tool_calls，渲染成协议 JSON（模型据此理解自己上一轮发起过工具调用）
      - role=tool      -> <tool_result name="...">...</tool_result>
    最后一条 user/tool 即本轮待模型处理的输入。
    """
    blocks = []

    for m in messages:
        role = (m.get("role") or "user").lower()

        if role == "tool":
            text = _flatten_content(m.get("content"))
            name = m.get("name") or ""
            tcid = m.get("tool_call_id") or ""
            attr = f' name="{name}"' if name else ""
            if tcid and not name:
                attr = f' tool_call_id="{tcid}"'
            blocks.append(f"<tool_result{attr}>\n{text.strip()}\n</tool_result>")
            continue

        text = _flatten_content(m.get("content"))

        if role == "assistant" and m.get("tool_calls"):
            rendered = _render_tool_calls(m["tool_calls"])
            # 工具调用 + 可能的前置文本一起回放
            combined = (text.strip() + "\n" + rendered) if text.strip() else rendered
            blocks.append(f"<assistant>\n{combined.strip()}\n</assistant>")
            continue

        if not text.strip():
            continue
        blocks.append(f"<{role}>\n{text.strip()}\n</{role}>")

    # 工具协议放在 transcript【末尾】：实测 recency 效应下模型遵循率从 0/5 → 5/5。
    # 若放最前，会被 opencode 等大 system prompt 淹没，导致模型「入戏」直接谎报完成而不发 JSON。
    if tool_prompt:
        blocks.append(f"<system>\n{tool_prompt.strip()}\n</system>")

    transcript = "\n\n".join(blocks)
    header = ("Below is a conversation transcript. Respond ONLY to the final turn, "
              "honoring any <system> instructions.\n\n")
    return header + transcript
