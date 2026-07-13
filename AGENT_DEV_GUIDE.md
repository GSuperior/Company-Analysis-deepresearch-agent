# Agent 开发经验 Skill

> 沉淀自 Company-Analysis-deepresearch-agent 的实战开发经验，供后续类似多 Agent 系统复用。
> 典型迁移目标：小红书爆款文案 Agent、抖音脚本 Agent、任意"调研 → 创作 → 审查"型 Agent。

---

## 0. 适用场景判断

本 skill 适用于满足以下特征的 Agent 系统：

- 需要**真实联网**（web_search / web_fetch），不能用 mock 数据
- 多角色流水线编排（侦察 → 规划 → 调研 → 写作 → 审查 → 渲染）
- 需要部署到 **Vercel**（Serverless），前端 SSE 实时展示进度
- LLM API **可配置**（用户自带 key，兼容任意 OpenAI 协议端点）

如果你只做单轮调用、无需联网、本地运行，本 skill 大部分内容可跳过，但 **§6 Vercel 坑点** 和 **§7 坑点速查表** 仍建议通读。

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│  前端 public/index.html (SSE 订阅 + 实时渲染)            │
└──────────────────────┬──────────────────────────────────┘
                       │ POST /api/research + SSE stream
┌──────────────────────▼──────────────────────────────────┐
│  app.py (Flask) ── Vercel 零配置入口                     │
│  - 任务内存字典 + 后台线程                                │
│  - SSE 心跳保活                                          │
└──────────────────────┬──────────────────────────────────┘
                       │ 调用
┌──────────────────────▼──────────────────────────────────┐
│  适配层 (xxx_deepresearch.py)                            │
│  - 桥接 web 接口到 controller                            │
│  - 构建 query / 执行摘要 / 错误兜底                       │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  controller.py (编排核心)                                │
│  scout → planner → researcher×N → writer×N              │
│         → reviewer → render                              │
└──────┬───────────────────────────┬──────────────────────┘
       │                           │
┌──────▼──────┐           ┌────────▼────────┐
│  llm.py     │           │  tools.py       │
│  - 动态配置  │           │  - web_search   │
│  - 工具循环  │           │  - web_fetch    │
│  - 收敛检测  │           │  - 文件读写      │
│             │           │  - 业务工具      │
└─────────────┘           └─────────────────┘
```

**关键分层原则**：
- `app.py` 只管 HTTP / 任务状态 / SSE，不碰业务逻辑
- `adapter`（适配层）只管参数转换 + 结果包装
- `controller` 只管流水线编排（派发角色、跑 validator、处理失败重试）
- `llm.py` 只管一个角色的 function-calling 循环
- `tools.py` 只管工具实现 + 预算计数

**不要让 controller 直接调 OpenAI SDK**——会导致重试/预算/事件逻辑散落各处。

---

## 2. LLM API 任意配置（核心模式）

### 2.1 动态客户端（不要在模块级实例化）

**坑**：在模块顶层 `client = OpenAI(api_key=os.environ.get(...))` 会导致：
- 用户传入的 key 无法覆盖环境变量
- 每个 worker 进程共享同一客户端，无法热切换

**正确做法**：用模块级变量 + `configure()` 函数，每次任务调用时设置。

```python
# llm.py
from openai import OpenAI

API_KEY = os.environ.get("SENSENOVA_API_KEY", "")
BASE_URL = os.environ.get("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1")
MODEL = os.environ.get("SENSENOVA_MODEL", "sensenova-6.7-flash-lite")

# 动态客户端（由 configure() 设置）
_client: Optional[OpenAI] = None
_current_model = MODEL
_current_api_key = API_KEY

def configure(api_key: str, model: str = "sensenova-6.7-flash-lite",
              base_url: str = "https://token.sensenova.cn/v1") -> None:
    """动态配置 API 客户端（web 端每次任务时调用）。"""
    global _client, _current_model, _current_api_key
    _client = OpenAI(api_key=api_key, base_url=base_url)
    _current_model = model
    _current_api_key = api_key
```

### 2.2 兼容任意 OpenAI 协议端点

只要目标 API 兼容 OpenAI Chat Completions 协议，改 `BASE_URL` + `MODEL` 即可：

| 平台 | BASE_URL | MODEL 示例 |
|------|----------|-----------|
| SenseNova | `https://token.sensenova.cn/v1` | `sensenova-6.7-flash-lite` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |

**坑**：部分国产 API 对 `assistant` 消息的 `content` 字段要求严格——当消息含 `tool_calls` 时 `content` 必须存在（可为 `null`）。转换历史时要补：

```python
def _to_dict(msg) -> dict:
    d = msg.model_dump(exclude_none=True)
    d.pop("reasoning", None)  # 思考链不回传，避免历史膨胀
    if "tool_calls" in d and "content" not in d:
        d["content"] = None  # 某些 API 要求 content 字段必须存在
    return d
```

### 2.3 思考模式（reasoning）的 token 占用

**坑**：带思考模式的模型（如 `sensenova-6.7-flash-lite`、DeepSeek-R1）的思考过程占用 `completion_tokens`。如果 `max_tokens` 给太小，**思考阶段就触发 length 截断**，返回 `content=None`。

**对策**：
- 默认 `max_tokens=16000`，需要输出大 JSON 的角色（planner / report-writer）给到 `32000`
- 把 `reasoning` 从历史消息中剔除（见上 `_to_dict`），否则历史膨胀到爆

---

## 3. Agent 编排模式

### 3.1 流水线角色拆分

通用"调研 → 创作"型 Agent 的角色模板：

| 角色 | 职责 | 工具 | 产出 |
|------|------|------|------|
| scout | 领域侦察、基本信息摸底 | web_search, web_fetch | briefing.json |
| planner | 拆解维度、设计大纲 | （无搜索）| plan.json |
| researcher×N | 逐维度调研，每个维度独立派发 | web_search, web_fetch, add_card | evidence.jsonl |
| writer×N | 逐章节创作 | read_cards, write_section | sections/sN.md |
| reviewer | 质量审查 | read_cards, web_search | review.json |
| render | 引用替换 + TOC + 来源（纯 Python，无 LLM）| — | report.md |

**小红书/抖音文案迁移示例**：

| 角色 | 职责 | 产出 |
|------|------|------|
| scout | 搜爆款笔记/视频，摸清赛道调性 | briefing.json（赛道、爆款共性）|
| planner | 拆解选题角度（痛点/反差/教程/种草）| plan.json（N 个选题方向）|
| researcher×N | 每个选题方向深挖素材、金句、钩子 | cards.jsonl（素材卡片）|
| writer×N | 每个选题写一版文案 | sections/sN.md |
| reviewer | 检查是否符合平台调性、有无违规词 | review.json |
| render | 拼装 + 标题党润色 | report.md |

### 3.2 失败处理三原则

借鉴 sn_agent 的容错策略：

1. **维度失败 → 降级重试 → 仍失败则跳过**：不阻塞后续维度
   ```python
   ok = stage_research_one(query, report_dir, dim)
   if not ok:
       dim_retry = dict(dim)
       dim_retry["depth"] = "moderate"  # 降级
       ok = stage_research_one(query, report_dir, dim_retry)
       if not ok:
           dim_ids.pop()  # 跳过，继续后续
           continue
   ```
2. **writer 失败 → 跳过该 section**：继续后续章节
3. **reviewer verdict=fail → 记录但不重做**：避免无限重试循环

### 3.3 Controller 内置 render

**坑**：把 render 也交给 LLM 做，会引入幻觉（它会"发明"引用）。**render 必须纯 Python**：正则替换 `[card:dN.cM]` → `[N]` + 生成 TOC + 去重来源列表。

---

## 4. 工具调用循环（function-calling）

### 4.1 核心循环结构

```python
def run_role(role_name, system_prompt, payload, *, max_iterations=24, max_tokens=16000, tools_enabled=None):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload},
    ]
    final_text = ""
    _search_history: dict[str, int] = {}  # 重复搜索检测

    for i in range(1, max_iterations + 1):
        msg = _call_model(role_name, messages, tool_specs, max_tokens)
        if msg is None:
            break

        # 空内容 + 无工具调用 → 瞬时失败，重试
        if not msg.tool_calls and not (msg.content or "").strip():
            continue

        messages.append(_to_dict(msg))

        # 没有工具调用 → 角色已产出最终答复
        if not msg.tool_calls:
            final_text = msg.content or ""
            break

        # 执行所有工具调用
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            result = tools.execute_tool(name, args)
            if len(result) > 24000:
                result = result[:24000] + "\n[... 工具结果已截断 ...]"
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": result})
    return final_text
```

### 4.2 预算前置约束（关键创新）

**坑**：事后检测"搜索太多次"没用——token 已经烧了。**预算必须在调用前设置，耗尽即拒绝**。

```python
class Budget:
    def __init__(self, search_budget=6, card_budget=8):
        self.search_budget = search_budget
        self.search_used = 0
    def consume_search(self) -> bool:
        if self.search_used >= self.search_budget:
            return False
        self.search_used += 1
        return True

def web_search(query, ...):
    if _budget and not _budget.consume_search():
        return json.dumps({"error": "budget_exhausted",
                           "message": f"搜索预算已用尽（{_budget.search_budget}次）"})
    # ... 真实搜索
```

### 4.3 重复搜索检测 + 收敛提示

**坑**：LLM 会反复搜索同一个关键词，陷入循环。**第 3 次相同查询时注入收敛提示**，强制它转入下一步。

```python
if search_key:
    _search_history[search_key] = _search_history.get(search_key, 0) + 1

result = tools.execute_tool(name, args)

if search_key and _search_history[search_key] >= 3:
    result += (
        f"\n\n⚠ 系统提示：该查询已被调用 {_search_history[search_key]} 次，"
        "结果已饱和。请停止重复搜索，用已收集的信息提交卡片或调用 submit。"
    )
```

### 4.4 JSON 参数被截断的修复

**坑**：模型输出长 `tool_call.arguments` 时可能被 `max_tokens` 截断，导致 `json.loads` 失败。**用正则提取已知字段**做降级修复：

```python
def _repair_json_args(args_str: str) -> dict | None:
    try:
        v = json.loads(args_str)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    result = {}
    for key in ("section_id", "title", "content"):
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)' % re.escape(key), args_str, re.DOTALL)
        if m:
            result[key] = m.group(1) + ("\n[... 截断 ...]" if key == "content" else "")
    return result or None
```

---

## 5. SSE 流式输出

### 5.1 事件回调机制

`llm.py` 维护一个全局 `_emit_callback`，controller 通过 `_emit()` 转发事件，`app.py` 把事件塞进 `queue.Queue`，SSE handler 消费：

```python
# llm.py
_emit_callback = None
def set_emit_callback(cb): global _emit_callback; _emit_callback = cb
def _emit(event_type, data):
    if _emit_callback:
        try: _emit_callback(event_type, data)
        except Exception: pass

# controller.py 派发时
_emit("phase_change", {"phase": "research", "message": f"研究阶段：{dim_name}"})
_emit("tool_call", {"agent": role_name, "tool": name, "args_summary": ...})

# app.py 后台线程
def event_callback(event_type, event_data):
    event = {"type": event_type, "data": event_data, "timestamp": ...}
    task["events"].append(event)
    task["event_queue"].put(event)
```

### 5.2 SSE 心跳保活（Vercel 必需）

**坑**：Vercel Serverless 有执行时间限制（免费 10s / Pro 300s）。长任务期间 SSE 连接会断。**每 5 秒发心跳**，前端实现重连：

```python
while True:
    try:
        event = event_queue.get(timeout=5)
        if event["type"] == "__end__":
            break
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except queue.Empty:
        # 心跳，保持连接
        yield f"data: {json.dumps({'type': 'ping', ...})}\n\n"
```

**前端必须先回放历史事件**（重连场景）：连上后先拉 `/result` 端点拿已发生的事件。

---

## 6. Vercel 部署专题（重点坑点）

### 6.1 只读文件系统（最高频坑）

**错误**：`[Errno 30] Read-only file system: '/var/task/reports/...'`

**根因**：Vercel Function 在 `/var/task/` 是只读挂载，**只有 `/tmp` 可写**。权限位可能是 writable，但实际写入会被内核拒绝（EROFS）。

**错误修复**（不可靠）：
```python
# ❌ os.access 只检查权限位，不检查挂载是否只读
if not os.access(REPO_ROOT, os.W_OK):
    return Path("/tmp/...")
```

**正确修复**（实际写入测试）：
```python
def _get_reports_root() -> Path:
    tmp_root = Path("/tmp/reports/xxx")
    local_root = REPO_ROOT / "reports" / "xxx"
    # 1. 环境变量快速判断
    if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
        return tmp_root
    # 2. 实际写入探针
    try:
        local_root.mkdir(parents=True, exist_ok=True)
        probe = local_root / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return local_root
    except (OSError, PermissionError):
        return tmp_root
```

**所有写入路径都要走 `report_dir`**（由 `make_report_dir()` 返回，自动跟随到 `/tmp`）。不要在任何地方硬编码 `REPO_ROOT / "reports"`。

### 6.2 执行时间限制

| 套餐 | 单次 Function 上限 |
|------|---------------------|
| Hobby（免费）| 10 秒 |
| Pro | 300 秒 |

**坑**：真实联网调研要 10-30 分钟，必然超时。**对策**：
- `vercel.json` 配 `maxDuration: 300`
- 后台线程跑任务，SSE 只负责推事件，断线前端重连
- 前端用 `/result` 轮询兜底（SSE 断了也能拿结果）

```json
// vercel.json
{
  "version": 2,
  "builds": [{"src": "app.py", "use": "@vercel/python",
              "config": {"maxDuration": 300}}],
  "routes": [
    {"src": "/api/(.*)", "dest": "app.py"}
  ]
}
```

### 6.3 内存存储不可靠

**坑**：Serverless 每次请求可能是不同实例，`tasks = {}` 内存字典会丢。

**对策**（生产环境）：用外部存储（Vercel KV / Upstash Redis）。本 demo 用内存字典 + 实例内线程，**同一实例内可用，跨实例会丢任务**——可接受 demo 场景。

### 6.4 静态文件服务

Vercel 不会自动 serve `public/`，需要在 `app.py` 里加路由读 `public/index.html`（见本项目 `index()` 路由）。

### 6.5 GitHub PAT 不要暴露在 push URL

**坑**：`git push https://user:token@github.com/...` 会被 GitHub 密钥扫描识别并**自动吊销 token**。

**正确做法**：用 credential helper 临时存储：
```bash
echo "https://USER:TOKEN@github.com" > /tmp/git-creds
git -c credential.helper='store --file=/tmp/git-creds' push origin main
rm /tmp/git-creds
```

---

## 7. 坑点速查表

| 现象 | 根因 | 对策 |
|------|------|------|
| `Read-only file system` | Vercel `/var/task` 只读 | 写入重定向到 `/tmp`（见 §6.1）|
| `content=None` 返回 | 思考模式耗尽 max_tokens | 提到 16000-32000，剔除 reasoning 历史 |
| LLM 反复搜同一关键词 | 无收敛机制 | 第 3 次注入收敛提示（见 §4.3）|
| `tool_call.arguments` JSON 解析失败 | 输出被截断 | 正则降级提取（见 §4.4）|
| SSE 连接断开 | Vercel 时间限制 | 5s 心跳 + 前端重连（见 §5.2）|
| 任务结果丢失 | 跨实例内存字典 | 改用外部存储（见 §6.3）|
| token 被吊销 | 暴露在 URL | credential helper（见 §6.5）|
| `assistant` 消息报错 | content 字段缺失 | 含 tool_calls 时补 `content: null`（见 §2.2）|
| render 出现幻觉引用 | LLM 做 render | render 必须纯 Python（见 §3.3）|
| 工具结果超长 | 搜索返回大网页 | 截断到 24000 字符 |
| 历史消息膨胀 | reasoning 回传 | `_to_dict` 中 `pop("reasoning")` |

---

## 8. 工具实现要点

### 8.1 web_search（不要用付费 API 的兜底）

**坑**：依赖 SerpAPI / Bing API 需要额外 key，Vercel 上配环境变量麻烦。**直接抓 Bing HTML**，配合 UA 池 + 新会话 cookie 避免反爬：

```python
_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 ...",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 ...) ...",
]
# 每次搜索用新 session，避免 cookie 退化触发 bot 检测
```

### 8.2 web_fetch（html2text 转纯文本）

```python
_H2T = html2text.HTML2Text()
_H2T.ignore_links = False
_H2T.ignore_images = True
_H2T.body_width = 0  # 不自动换行
```

### 8.3 文件通信契约

角色之间用**文件**通信，不用内存传递大 JSON：
- briefing.json / plan.json / evidence.jsonl / sections/sN.md / review.json
- payload 里只传**路径**，让角色自己 `read_file` 读取
- 避免单条消息过大触发 400

---

## 9. 迁移示例：小红书爆款文案 Agent

### 9.1 角色映射

```
scout      → 搜赛道爆款，摸清调性
planner    → 拆 5 个选题角度（痛点/反差/教程/种草/避雷）
researcher → 每个选题深挖素材（金句、钩子、评论区高频词）
writer     → 每个选题写一版文案（标题+正文+标签）
reviewer   → 检查调性、违规词、emoji 使用
render     → 拼装成可直接复制的格式
```

### 9.2 工具调整

| 保留 | 替换/新增 |
|------|----------|
| web_search, web_fetch | + `fetch_xhs_notes`（抓小红书笔记结构）|
| read_file, write_file | + `add_material_card`（素材卡片）|
| write_section | `write_copy`（写一版文案）|

### 9.3 预算调整

文案场景搜索次数少、写作多：
```python
ITER_BUDGET = {"scout": 15, "planner": 10, "researcher": 20,
                "writer": 12, "reviewer": 10}
DEPTH_SEARCH_BUDGET = {"light": 3, "moderate": 5, "deep": 7}  # 比调研场景少
```

### 9.4 Prompt 关键差异

- researcher 要抓**情绪词、钩子句式、评论区高频问题**
- writer 要遵守**字数限制**（小红书正文 ≤1000 字）、**标签数量**（5-10 个）
- reviewer 要查**违规词**（医疗/金融敏感词）、**emoji 密度**

---

## 10. 目录结构模板

```
my-agent/
├── app.py                    # Flask 入口（Vercel 零配置）
├── vercel.json
├── requirements.txt
├── public/index.html         # 前端 SSE 订阅
├── xxx_deepresearch.py       # 适配层（桥接 web → controller）
└── xxx_agent/
    ├── controller.py         # 流水线编排
    ├── llm.py                # function-calling 循环 + 动态配置
    ├── tools.py              # 工具实现 + 预算
    ├── prompts/              # 各角色 prompt
    │   ├── scout.md
    │   ├── planner.md
    │   └── ...
    └── __init__.py
```

---

## 11. 开发流程建议

1. **先跑通单角色**：scout 能搜能产出 briefing.json，再串流水线
2. **本地用 `reports/` 目录**，部署前再验证 `/tmp` 重定向
3. **每个角色单独测 prompt**：用 `python -c "from xxx_agent.llm import run_role; ..."` 直接调
4. **Vercel 部署前必做**：本地 `python app.py` 起服务，curl 触发一次完整任务
5. **真机联调**：部署后用真实公司名/选题跑一遍，看 `pipeline.log` 找卡点

---

## 附：关键代码文件索引（本项目）

- 动态 LLM 配置：[cr_agent/llm.py](file:///workspace/Company-Analysis-deepresearch-agent/cr_agent/llm.py) L26-L46
- function-calling 循环：[cr_agent/llm.py](file:///workspace/Company-Analysis-deepresearch-agent/cr_agent/llm.py) L158-L260
- 工具循环 + 收敛检测：[cr_agent/llm.py](file:///workspace/Company-Analysis-deepresearch-agent/cr_agent/llm.py) L231-L251
- 预算管理：[cr_agent/tools.py](file:////workspace/Company-Analysis-deepresearch-agent/cr_agent/tools.py) L35-L60
- 可写目录检测（正确版）：[sn_agent/controller.py](file:///workspace/Company-Analysis-deepresearch-agent/sn_agent/controller.py) L37-L59
- 流水线编排 + 失败重试：[sn_agent/controller.py](file:///workspace/Company-Analysis-deepresearch-agent/sn_agent/controller.py) L514-L620
- SSE 心跳：[app.py](file:///workspace/Company-Analysis-deepresearch-agent/app.py) L458-L471
- Vercel 配置：[vercel.json](file:///workspace/Company-Analysis-deepresearch-agent/vercel.json)
