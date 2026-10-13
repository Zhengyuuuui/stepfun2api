# stepfun2api

> **这是什么**：本项目把 **StepFun 海外版 AI Studio**（https://studio.stepfun.ai/ ，网页版 playground）
> 的私有前端协议**逆向 + 反向代理**成 **OpenAI 兼容 API**。任何 OpenAI 客户端
> （chat UI / SDK / LangChain / opencode / Cline…）可把 base_url 指向本网关使用 studio 里的模型。
>
> **这不是** stepfun 官方的开放平台 API（platform.stepfun.com 那套），两者账号、鉴权、协议完全无关。
> 本网关复用你在浏览器里登录 studio.stepfun.ai 的会话 cookie 来驱动它。

> **关键认知：studio 的模型是「半黑盒 agent」，不是纯模型 API。**
> `https://studio.stepfun.ai/` 的对话端点背后不是一个裸模型，而是一个**自带系统人设、
> 服务端累积会话历史、内置工具（原生 web_search / reasoning 链 / ai_write 等）的 agent 产品**。
> 表现为：
> - 端点只接受**单条 content 字符串**，不认 OpenAI 的 messages 数组，也没有独立 system 字段；
> - 服务端按 `conversation_id` 自己累积上下文（所以网关每请求新建再删除临时会话，做无状态化）；
> - 模型的"思考"（reasoning）与"联网搜索"是 studio 侧内建能力，以专有 SSE 事件下发；
> - 上游**没有** OpenAI 式原生 tools/function 协议——模型是被"提示词指挥去输出 JSON"的，
>   所以工具调用是网关用提示词**模拟**出来的（详见 Function Calling 章节）。
>
> 因此：**只有 `step-5-preview` 的工具调用实测可靠**，flash 系列（step-3.7-flash / step-3.5-flash-2603）
> 指令遵循不稳定，会谎报"已完成"或谎称"没有工具"，不推荐用于 agent/工具场景（它们纯聊天/带联网意图没问题）。

> 逆向依据与全部 curl 实测证据：`sessions/2026-10-11-stepfun-studio/RECON.md`

---

## Model List（studio 实际提供的模型，`GET /v1/models` 动态返回 chat 类）

studio 的 playground 配置里模型分四类，本网关目前**只反代 chat 类**：

| 类别 | 模型 | 网关支持 |
| -- | -- | -- |
| chat | `step-5-preview`（默认，工具调用可靠 ✅） | ✅ |
| chat | `step-3.7-flash`（联网意图/纯聊天可用；工具场景不稳） | ✅ |
| chat | `step-3.5-flash-2603`（同上；不支持多模态） | ✅ |
| image | `step-image-edit-2` | ❌ 未实现 |
| tts | `stepaudio-2.5-tts` / `step-tts-2` | ❌ 未实现 |
| asr | `stepaudio-2.5-asr` | ❌ 未实现 |

> `/v1/models` 从上游 `GetStudioPlaygroundConfig` 实时拉取 chat 模型，账号权限变了自动跟随。

---

## 上游协议（逆向还原，实测 Confirmed）

- 后端是 **Connect-RPC (gRPC-Web JSON)** + 一个 SSE 流式对话端点
- 认证：cookie（`Oasis-Token` / `Oasis-Webid` / `acw_tc`）+ `oasis-*` headers
  - 缺 oasis 头 → `401 auth failed: oasis-token is embezzled`
- **流式对话**：`POST /api/stdhttp/v1/studio/stream`，`text/event-stream`，Anthropic Messages 风格 SSE
- **关键约束**：stream 端点只吃单条 `content` 字符串，**不支持** messages 数组，也**没有** system_prompt 字段；
  服务端按 `conversation_id` 自动累积历史。

## 网关设计：无状态 transcript 模式

因为上游只收单条 content，本网关对每个 OpenAI 请求：

```
1. CreateConversation        → 拿新 conversation_id
2. messages[] 序列化成 transcript（<system>/<user>/<assistant> 标签）→ 单条 content
3. POST /studio/stream       → 逐条 SSE 转成 OpenAI chunk
4. DeleteConversation        → 用完即删（auto_delete，零残留，实测确认）
```

优点：**并发安全**（每请求独立会话）、**零污染**（不堆积历史）、天然支持多轮与 system。
代价：每请求多一次 Create/Delete RPC（实测 DeleteConversation 返回 200，会话即时消失）。

SSE 事件 → OpenAI 映射：

| 上游 type | → OpenAI |
| -- | -- |
| `reasoning_delta` | `delta.reasoning_content` |
| `text_delta` | `delta.content` |
| `final.usage` | `usage`（tokens_in→prompt, tokens_out→completion, cached→prompt_tokens_details） |
| `done` | `data: [DONE]` |

---

## 安装

```bash
pip install -r requirements.txt
```

## 配置

```bash
cp config.example.json config.json
```

把浏览器里 studio.stepfun.ai 的 cookie 存到 `cookie.txt`（整行 Cookie header 值）：

```
acw_tc=...; studio_panel_state=true; Oasis-Webid=...; Oasis-Token=...
```

> **获取 cookie**：浏览器登录 studio.stepfun.ai → DevTools → Network → 任意 `/api/` 请求
> → Request Headers → 复制 `Cookie:` 整行值粘贴进 `cookie.txt`。
> `Oasis-Token` 是 JWT：access token 寿命 **2 小时**（会被浏览器用 refresh token 轮换，
> refresh token 寿命约 30 天）。**已内置自动续期**，见下方「自动续期」章节，无需手动重导。

`config.json` 字段：

| 字段 | 默认 | 说明 |
| -- | -- | -- |
| `port` | 8390 | 监听端口 |
| `api_keys` | `["sk-stepfun"]` | 网关鉴权 key（客户端 Bearer）；空数组=不鉴权 |
| `cookie_file` | `cookie.txt` | cookie 文件路径 |
| `default_model` | `step-5-preview` | 未指定 model 时用 |
| `default_thinking` | `true` | 默认开启思维链 |
| `default_effort` | `medium` | low/medium/high |
| `default_max_tokens` | 8000 | |
| `auto_delete_conversation` | `true` | 请求结束删除临时会话 |
| `tool_max_retries` | `2` | 工具模式谎报检测命中后的自动纠正重试次数 |
| `proxy` | `null` | 上游代理（如 `http://127.0.0.1:7890`） |
| `language` | `en` | oasis-language 头 |

所有字段也可用大写环境变量覆盖（`PORT` / `API_KEYS` / `COOKIE_FILE` / `DEFAULT_MODEL` ...）。

## 启动

```bash
python gateway.py
# 或
PORT=8391 API_KEYS=sk-a,sk-b python gateway.py
```

---

## Web Console

启动后浏览器打开 `http://127.0.0.1:8390/` 即内置控制台（单文件 `static/index.html`，无外部依赖，
明暗双主题，布局参考同作者 minimax2api console）。四个视图：

- **总览**：**登录/Cookie 面板**（打开 Studio 登录页 → 登录 → 自动抓 cookie 热更新的闭环）、
  请求数/流式/工具模式/平均延迟/错误/token 用量；模型分布；**工具协议健康度**
  （一次通过率 + 谎报拦截/拒绝拦截/纠正重试/两段式搜索计数）；最近请求表
- **账号与额度**：上游实时额度（免费额度/5小时/周窗口进度条 + 重置时间 + credit 明细）、
  access token 剩余寿命条 + **一键续期按钮**（等效手动跑 refresh_cookie.py）、账号信息（UID/Email/订阅）
- **会话**：上游账号会话列表实时查看（验证网关用完即删、零残留）
- **日志**：网关日志环形缓冲实时增量拉取

Console API（均需 `Authorization: Bearer <网关api_key>`，与 `/v1/*` 同一套鉴权）：

| 端点 | 说明 |
| -- | -- |
| `GET /v1/console/overview` | 指标快照 + token 状态 + 配置摘要 |
| `GET /v1/console/quota` | 上游额度/订阅计划/账号信息（`QueryStudioRateLimit` 等实测端点） |
| `GET /v1/console/logs?since=<id>` | 日志增量（环形缓冲 300 条） |
| `GET /v1/console/conversations` | 上游会话列表 |
| `GET /v1/console/login-status` | 调试 Chrome 状态（CDP/studio页/登录态/token 剩余，JSON） |
| `POST /v1/console/open-login` | 在调试 Chrome 里打开/聚焦 studio 登录页 |
| `POST /v1/console/capture-cookie` | 从浏览器抓取 cookie 覆盖 cookie.txt 并热更新（登录闭环用） |
| `POST /v1/console/refresh-cookie` | **强制**轮换续期（reload 页面换发新 token 后覆盖 cookie.txt） |

> **登录即拿 cookie**：总览页「打开 Studio 登录页」→ 浏览器里正常登录 → 控制台每 2s 轮询，
> 检测到有效 token 后自动抓取 cookie.txt 并热更新网关，全程不用打开 DevTools 抄 cookie。
> 前提：Chrome 以 `--remote-debugging-port=9366` 启动（refresh token 约 30 天有效，期间可随时重抓）。
> 注意：上游 SPA 只在 token 过期/临期时才换发新 access token，「立即续期」对有效期内
> 的 token 会如实提示"已替换 cookie 但 SPA 未换发"。

---

## 使用

```bash
# 模型列表
curl http://127.0.0.1:8390/v1/models -H "Authorization: Bearer sk-stepfun"

# 非流式
curl http://127.0.0.1:8390/v1/chat/completions \
  -H "Authorization: Bearer sk-stepfun" -H "Content-Type: application/json" \
  -d '{"model":"step-5-preview","messages":[{"role":"user","content":"你好"}]}'

# 流式 + thinking + effort
curl http://127.0.0.1:8390/v1/chat/completions \
  -H "Authorization: Bearer sk-stepfun" -H "Content-Type: application/json" \
  -d '{"model":"step-5-preview","stream":true,"thinking":true,"reasoning_effort":"high",
       "messages":[{"role":"user","content":"证明根号2是无理数"}]}'
```

### 支持的模型（`GET /v1/models` 动态返回）

| model | thinking | multimodal |
| -- | -- | -- |
| `step-5-preview` | ✅ effort low/med/high | ✅ |
| `step-3.7-flash` | ✅ | ✅ |
| `step-3.5-flash-2603` | ✅ | ❌ |

### OpenAI 请求参数映射

| OpenAI | → StepFun |
| -- | -- |
| `model` | `chat.model.model_id` |
| `temperature` / `top_p` / `max_tokens` / `frequency_penalty` | `chat.model.config.*` |
| `stream` | SSE 流式 |
| `reasoning_effort`（low/medium/high） | `config.effort` |
| `thinking`（extra_body，bool） | `config.thinking` |
| `web_search`（extra_body，bool） | `enable_web_search` |
| `tools` / `tool_choice` | 提示词模拟 → 标准 `tool_calls`（见下） |

思维链输出在 `delta.reasoning_content`（流式）/ `message.reasoning_content`（非流式）。

### Function Calling（工具调用）

上游**没有**原生 tools 协议（stream 端点只吃单条 content）。网关用**提示词模拟**实现标准
OpenAI function calling：把 `tools[]` schema 渲染成严格协议指令注入 transcript，模型输出
JSON，网关再解析还原成标准 `tool_calls`。**一套标准实现即可对接所有遵循 OpenAI 协议的
agent（Cline / Continue / Roo / LangChain / OpenAI SDK / Cursor…），无需为每个 agent 单独适配。**

- 支持：`tools`、`tool_choice`（auto/none/required/指定函数）、并行多工具调用、
  多轮工具循环（`role:"tool"` 结果回传）、流式与非流式
- 响应：`finish_reason:"tool_calls"` + `message.tool_calls`（或流式 `delta.tool_calls`）
- **模型要求**：务必用 `step-5-preview`（带 tools 时网关自动强制 `thinking=true`）。
  实测 flash 系列遵循工具协议不稳定，会跑偏。

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8390/v1", api_key="sk-stepfun")
r = client.chat.completions.create(
    model="step-5-preview",
    messages=[{"role": "user", "content": "写一个文件 /tmp/hello.py，内容 print('hi')"}],
    tools=[{"type": "function", "function": {
        "name": "write_file", "description": "写文件",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}}],
    tool_choice="auto",
)
tc = r.choices[0].message.tool_calls[0]
print(tc.function.name, tc.function.arguments)  # write_file {"path":"/tmp/hello.py",...}
```

### Web Search（联网搜索，上游原生）

与模拟工具不同，StepFun Studio **原生内置 `web_search`**：开 `enable_web_search` 后，
服务端自动执行搜索（原生 `tool_call` 事件，含 `search_results`），模型直接基于真实结果作答，
**客户端无需回传搜索结果**。SSE `final.usage.web_search_call_count` 记录搜索次数。

网关把这条原生能力透传为标准 OpenAI 语义，三种启用方式：

1. **自动意图识别**（默认）：最后一条 user 消息含「搜索/最新新闻/latest news/今天的新闻…」等
   联网意图 → 自动开启原生搜索（`web_search:false` 可显式关闭）
2. **显式参数**：请求体 `web_search:true` / `enable_web_search:true` / OpenAI `web_search_options`
3. **内置工具**：`tools` 里含 `{"type":"web_search"}` 或同名 `function` → 剔除该工具并启用原生搜索

**与模拟工具共存（两段式编排）**：客户端既带 `tools[]` 又有搜索意图时（如 opencode：
write/bash + "搜新闻写文件"），单轮里原生搜索与注入的 JSON 工具协议会冲突（模型假调
`web_search`）。网关自动改为两段：先跑一轮原生搜索拿结果 → 结果注入 transcript
（`<web_search_results>`）→ 再进工具模式，模型据实写文件。已实测成功产出真实新闻的 `tool_calls`。

```python
# 无需任何参数，自动识别
r = client.chat.completions.create(
    model="step-5-preview",
    messages=[{"role": "user", "content": "搜索今天的AI新闻并写进 news.md"}],
)
# → 模型基于真实搜索结果给出 tool_calls: write("news.md", <真实新闻>)
```

> 原理与限制：这是「提示词模拟」而非原生 tool 协议，可靠性取决于模型指令遵循。
> 网关做了两层加固（见下），11 工具 + 大 system prompt 的 opencode 真实场景实测 6/6 通过。
> 建议 agent 侧 `temperature=0`。

#### 工具可靠性加固（重要）

实测发现：工具多（11 个）+ system prompt 大（opencode）时，模型易「入戏」产生两类失败——
**谎报完成**（"已创建/已写入"却不发 JSON）与**谎称工具不可用而撒手**（"没有命令行工具/
tool_unavailable"直接放弃）。temperature=0 无法根除，属模型非确定性。网关用五层机制兜底：

1. **提示词加固**：工具协议渲染在 transcript **末尾**（recency 效应）+ 紧凑工具签名 +
   few-shot 正反例（含"谎报"与"谎称无工具"两个反例）+ 反幻觉规则 + **工具可用性声明**
   （明确"这些工具都已连接、真实可用，说不可用即违规"）+ 末尾条件式 NOW 指令。
2. **谎报检测 + 自动重试**：检测到"已创建/已写入/successfully created"却没发 JSON 时，
   带纠正指令重试（`CORRECTION`，最多 `tool_max_retries` 次，默认 2）。
3. **拒绝话术检测 + 自动重试**：检测到"没有可用工具/命令行不可用/tool_unavailable/I don't
   have access to tools"等撒手话术时，带专用纠正指令重试（`CORRECTION_REFUSAL`，指出该声称
   为假、工具确实可用）。
4. **工具名幻觉归一**：模型偶把 `write` 说成 `file_write`/`create_file`，用子串+token 重叠
   映射回真实声明名，避免客户端报 tool not found。
5. **两段式搜索编排**：既带 tools 又有联网意图时，先纯检索拿真实结果 → 注入 `<web_search_results>`
   → 再进工具模式（否则模型会假调 web_search / 谎称无联网工具）。

检测均**仅在工具模式 + 非 tool_result 回传轮**触发，不误伤正常文本与工具结果总结。
触发时日志有 `工具模式谎报/拒绝检测命中，纠正重试`。
实测 opencode 真实形态（11 工具 + 大 system + 搜索写入）连续 **3/3 稳定成功**（含被污染的
历史会话 3/3），且拒绝检测网已在真实故障中捕获并纠正。

> 工具模式下流式响应为「收集式」：需缓冲完整输出以支持检测与重试，
> 故 `delta.content`/`delta.tool_calls` 在流末尾一次性发出（非逐字符）。普通对话仍实时流式。

### OpenAI 客户端接入

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8390/v1", api_key="sk-stepfun")
r = client.chat.completions.create(
    model="step-5-preview",
    messages=[{"role": "user", "content": "你好"}],
    extra_body={"thinking": True, "reasoning_effort": "high"},
)
print(r.choices[0].message.content)
```

---

## 自动续期（Token 每 2h 过期）

上游 `Oasis-Token` 由两段 JWT 组成（`access...refresh`，以 `...` 连接）：
- **access token：寿命 2 小时**（网关实际鉴权用的就是它，过期 → 401 `token is expired`）
- **refresh token：寿命约 30 天**（浏览器用它在过期时自动轮换 access token）

所以只要**本机 Chrome 保持登录 studio.stepfun.ai**，就可以定期从浏览器把最新 cookie 同步过来。
本网关实现了全自动续期闭环：

```
launchd 每 60 秒 → refresh_cookie.py（CDP 检查/抓最新 cookie → 写 cookie.txt）
                                    ↓
网关 cookie watcher（每5s 轮询 mtime）→ 内容变化 → 热更新 client（免重启）
```

### 关键机制与实测结论（为什么无法提前续期）

实测 StepFun 海外版 SPA 的换发行为有极其特殊的强约束，切勿盲目"提前续期"：
1. **无法提前续期**：SPA **仅在服务端返回 401 token-expired 时**，才会使用 refresh 段向上游换发同账号的新 access token（2h）；在 token 仍处于有效期内时，无论如何 reload 页面，SPA 都绝对不会换发新 token。
2. **伪造过期会换错账号**：如果通过本地修改时间戳等方式伪造 access token 过期（无论保留还是丢弃 refresh 段），都会导致上游签名校验失败。SPA 遭遇非法 token 时会直接清空 cookie 并触发游客重注册，生成一个**无额度、未激活（`activated=false`, `payMode=1`）的全新游客 UID**，导致原账号额度丢失不可用！
3. **闭环策略**：因此，协议层在有效期内不可能提前续期。网关采用最佳稳妥设计：
   - **真过期兜底**：launchd 定时任务每 60 秒检查一次，一旦真过期便触发页面 reload 换发新 token 并同步写入 `cookie.txt`。
   - **保持浏览器活跃**：用户日常保持调试 Chrome 打开 `studio.stepfun.ai` 标签页，SPA 在过期后发请求会自然换发，网关可在 60 秒内无缝跟进热更新。

### 组成

| 部件 | 说明 |
| -- | -- |
| `refresh_cookie.py` | 从 Chrome CDP（默认端口 9366）抓 studio.stepfun.ai 的最新 cookie 写入 `cookie.txt`；浏览器未开/未登录时静默跳过（不动现有文件） |
| 网关内置 watcher | `gateway.py` 启动时监视 `cookie.txt`，变化即热更新（**不用重启网关**） |
| `com.stepfun.cookie-refresh.plist` | launchd 定时任务：每 60s 跑一次刷新，日志写 `refresh.log` |

### 安装定时任务

```bash
cp com.stepfun.cookie-refresh.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stepfun.cookie-refresh.plist
# 确认注册成功
launchctl list | grep stepfun
```

### 手动操作

```bash
python3 refresh_cookie.py           # 立即刷新一次
python3 refresh_cookie.py --check   # 只看 token 剩余寿命，不写文件
tail -f refresh.log                 # 看定时刷新日志
```

> CDP 端口不是 9366 时：`CDP_PORT=9222 python3 refresh_cookie.py`，
> 并同步改 plist 里 `--port` 参数。
> Chrome 需以调试模式运行：`--remote-debugging-port=9366`。

### 续期失败排查

| 现象 | 原因 | 处理 |
| -- | -- | -- |
| refresh.log 有 `[warn] 无法连接 CDP` | Chrome 没开或没带调试端口 | 用调试模式启动 Chrome |
| refresh.log 有 `[warn] 没有 Oasis-Token` | 浏览器登录态掉了 | 浏览器重新登录 studio.stepfun.ai |
| 网关仍 401 | refresh token 也过期（>30天没用） | 浏览器重新登录，下次定时任务自动恢复 |

## 文件

| 文件 | 作用 |
| -- | -- |
| `gateway.py` | FastAPI 网关（`/v1/models` `/v1/chat/completions` `/healthz` + console API + cookie 热重载 watcher） |
| `static/index.html` | Web Console（总览/额度/会话/日志，单文件无依赖） |
| `stepfun_client.py` | 上游协议客户端（RPC + SSE 解析 + 鉴权头 + `update_cookie` 热更新） |
| `transcript.py` | messages[] → transcript content 序列化（含工具历史回放） |
| `tools.py` | 工具转换器：tools[] → 协议指令；模型 JSON → 标准 tool_calls |
| `refresh_cookie.py` | 从 Chrome CDP 抓最新 cookie 写入 cookie.txt（自动续期用） |
| `com.stepfun.cookie-refresh.plist` | launchd 定时任务（每 60s 刷新 cookie） |
| `config.example.json` | 配置模板 |
| `cookie.txt` | 凭证（已 gitignore） |

## 已知限制 / 待办

- **本质是反代网页版 agent**：走的是 studio.stepfun.ai 前端私有协议，依赖浏览器会话 cookie；
  studio 改版/加签/风控都可能随时打断，非官方 API，无 SLA，请自行评估使用场景与合规风险
- **自动续期依赖本机 Chrome**：需保持调试模式（CDP 9366）运行且登录 studio.stepfun.ai；refresh token 约 30 天不用也会过期，届时浏览器重登一次即可（见「自动续期」章节）
- **transcript 模式**：多轮历史由网关序列化进 content，而非复用会话；超长对话受 `max_tokens`/上下文窗口限制
- **工具调用是提示词模拟**：非上游原生协议，可靠性依赖模型指令遵循；已用五层加固（末尾协议 + 谎报检测重试 + 拒绝检测重试 + 工具名归一 + 两段式搜索），opencode 真实场景稳定 3/3，但仍非 100% 确定性，仅 `step-5-preview`+thinking 可用，flash 系列易跑偏；工具模式下流式为收集式（块在流末尾一次性发出）
- **仅实现 chat**：image/tts/asr（`kind` 字段切换）未实现，模型已在 `/v1/models` 外的 config 里可见
- **多模态输入**：图片走上游 `asset_ids`（需先传 `/api/stdhttp/v1/studio/file`），当前 transcript 只标注 `[image attached]`
