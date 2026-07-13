# DeepResearch Agent · 双架构企业深度研究系统

> 基于 sn-deep-research 设计思想的多 Agent 企业深度研究系统。
> 真实联网调研 + 证据可溯源 + 契约驱动流水线 + Vercel 一键部署。

在线 Demo：https://company-analysis-deepresearch-agent.vercel.app/

---

## 亮点速览

- **证据为核**：所有结论可溯源到真实公开来源（财报/公告/官网/新闻），零模拟数据
- **契约驱动**：每阶段输出严格 schema 化（briefing / plan / evidence / outline / review）
- **双流水线可选**：
  - `sn-deepresearch` — 忠实复刻 [sn-deep-research](https://github.com/OpenSenseNova/SenseNova-Skills/tree/main/skills/sn-deep-research) 9 角色流水线，证据 JSON + validator 校验
  - `cr-agent` — 自研轻量化流水线，EvidenceCard JSONL 增量追加 + 预算前置约束 + 内置 render
- **任意 LLM 可配**：兼容 OpenAI 协议端点（SenseNova / DeepSeek / Qwen / GPT），用户自带 Key
- **SSE 实时进度**：研究过程实时推送，前端可视化展示每个 Agent 的工具调用与产出
- **生产级容错**：维度失败降级重试 → 跳过不阻塞；writer 失败跳过章节；reviewer 不重做避免死循环
- **Vercel 零配置部署**：推到 GitHub 自动部署，只读文件系统已正确处理

---

## sn-deep-research 设计思想

本项目源自 SenseNova Skills 生态的 sn-deep-research 技能，其核心设计原则：

| 原则 | 含义 | 本项目落地 |
|------|------|-----------|
| **证据为核** | evidence 是唯一真相来源，报告每一句话都可溯源 | 所有 claim 标注 `source.url`，render 生成来源列表 |
| **契约驱动** | 每阶段输出严格 schema 化 | briefing / plan / evidence / outline 均有 schema 校验 |
| **档位解耦** | 档位选择器决定跑哪些阶段 | quick / normal / heavy 三档，影响维度数与迭代深度 |
| **角色原子化** | 每个 Agent 职责单一 | scout 只侦察，planner 只拆解，researcher 只取证…… |
| **能力降级** | 缺专业 skill 时不阻塞，用通用搜索兜底 | 专业类别（金融/财报）走通用 web_search 兜底 |
| **validator 校验** | 关键阶段用 Python 脚本校验 schema 合规性 | `validate_evidence.py` / `validate_outline.py` 自动校验 |

---

## 双流水线架构

### 架构一：sn-deepresearch（忠实复刻版）

完整复刻 sn-deep-research normal 模式 9 角色流水线：

```
scout → plan → research(×N) → evidence validator → review(子报告)
      → report-planner → outline validator → report-writer(full_outline)
      → review(终稿) → render
```

| 角色 | 职责 | 产出 |
|------|------|------|
| **scout** | 领域侦察、基本信息摸底、推荐研究档位 | briefing.json |
| **plan** | 拆解研究维度、关键问题、来源种子 | plan.json + blueprint.json |
| **research** ×N | 逐维度取证，每个维度独立派发 | sub_reports/dN.evidence.json |
| **evidence validator** | Python 脚本校验 evidence schema 合规性 | 校验结果 |
| **review** (子) | 子报告审查、缺口识别 | sub_reports/dN.review.md |
| **report-planner** | 报告大纲编排、证据分配到章节 | outline.json + sections/*.evidence_subset.json |
| **outline validator** | 校验大纲引用与证据的映射一致性 | 校验结果 |
| **report-writer** | 基于完整大纲 + 全量证据写终稿 | sections/s_full.md |
| **review** (终) | 终稿质量审查 | final_review.md |
| **render** | 引用编号替换 + TOC + 参考文献（纯 Python） | report.md + citations.json |

**核心文件**：
- [sn_agent/controller.py](sn_agent/controller.py) — 流水线编排
- [sn_agent/llm.py](sn_agent/llm.py) — function-calling 智能体循环
- [sn_agent/tools.py](sn_agent/tools.py) — 工具层（web_search / web_fetch / 文件读写）
- [sn_agent/skill/](sn_agent/skill/) — sn-deep-research 技能 spec + scripts + schemas
- [sn_agent/skill/SKILL.md](sn_agent/skill/SKILL.md) — 完整技能规范

### 架构二：cr-agent（自研轻量化版）

从 0-1 设计的精简流水线，针对 sn-deep-research 的痛点做了 4 项创新：

```
scout → planner → researcher(×N) → writer(×N sections) → reviewer → render(内置)
```

| 创新 | 解决的痛点 | 实现 |
|------|-----------|------|
| **EvidenceCard JSONL 增量追加** | sn 版一次性写大 evidence.json 易截断 | researcher 每搜到信息就 `add_card` 追加一行 JSONL |
| **预算前置约束** | sn 版事后检测"搜索太多次"已烧 token | Budget 在角色派发前设置，`consume_search()` 耗尽即拒绝 |
| **Section-by-section 写作** | sn 版 report-writer 一次性写全报告易超长 | writer 按章节逐段派发，每次只读关联维度卡片 |
| **Controller 内置 render** | LLM 做 render 会幻觉引用 | 纯 Python：正则替换 `[card:dN.cM]` → `[N]` + TOC + 去重来源 |

**核心文件**：
- [cr_agent/controller.py](cr_agent/controller.py) — 流水线编排 + 内置 render
- [cr_agent/llm.py](cr_agent/llm.py) — 智能体循环（含预算、收敛、JSON 修复）
- [cr_agent/tools.py](cr_agent/tools.py) — 工具层（含 Budget 类、业务工具）
- [cr_agent/prompts/](cr_agent/prompts/) — 5 角色 prompt

### 双流水线对比

| 维度 | sn-deepresearch | cr-agent |
|------|-----------------|----------|
| 角色数 | 9（含 2 个 validator + 2 次 review） | 5（含内置 render） |
| 证据格式 | 大 JSON（一次性写） | JSONL（增量追加） |
| 预算控制 | 事后检测 | 前置约束 + 重复收敛提示 |
| 写作策略 | 一次性写全报告 | 按章节逐段写 |
| render | Python 脚本（外部 subprocess） | Controller 内置函数 |
| Schema 校验 | ✅ validator 脚本 | ❌（轻量化取舍） |
| 适合场景 | 正式研报、强合规 | 快速调研、轻量产出 |

---

## 功能特性

- **模型选择**：支持切换 `sensenova-6.7-flash-lite` / `sensenova-6.7`，或任意 OpenAI 协议端点
- **动态研究维度**：根据公司特点生成 3-7 个研究维度
- **质量审核闭环**：初稿 → 审核 → 修改 → 终稿（支持多轮迭代）
- **SSE 实时流**：研究过程实时推送到前端，含心跳保活
- **流程可视化**：点击 Agent 节点查看输入/输出/工具调用/原始日志
- **合理的工具配置**：每个 Agent 的工具配置与其角色匹配（planner 不给搜索，reviewer 不给写作）
- **优雅降级**：维度失败降级重试 → 仍失败跳过；writer 失败跳过章节；不阻塞流水线
- **总超时保护**：40 分钟总超时，防止任务无限运行
- **Vercel 适配**：只读文件系统正确处理，写入重定向到 `/tmp`

---

## 快速开始

### 本地运行

```bash
pip install -r requirements.txt
python app.py
# 打开 http://localhost:5000/
```

### 部署到 Vercel

零配置部署：直接将代码推送到 GitHub，在 Vercel 中 Import 仓库即可。Vercel 会自动识别 Flask 应用。

`vercel.json` 已配置 `maxDuration: 300`（Pro 套餐 5 分钟执行时间）。

```
项目结构：
├── app.py                    # Flask 入口（Vercel 自动检测）
├── vercel.json               # Vercel 配置（路由 + 超时）
├── requirements.txt          # Python 依赖
├── public/
│   └── index.html            # 前端页面（SSE 订阅 + 流程可视化）
├── sn_deepresearch.py        # sn-deepresearch 适配层
├── cr_deepresearch.py        # cr-agent 适配层
├── sn_agent/                 # sn-deepresearch 完整实现
│   ├── controller.py
│   ├── llm.py
│   ├── tools.py
│   └── skill/                # sn-deep-research 技能 spec + scripts + schemas
├── cr_agent/                 # cr-agent 完整实现
│   ├── controller.py
│   ├── llm.py
│   ├── tools.py
│   └── prompts/
└── AGENT_DEV_GUIDE.md        # Agent 开发经验沉淀（含迁移示例）
```

---

## 深度档位

两套流水线共用 quick / normal / heavy 三档：

| 档位 | sn-deepresearch | cr-agent | 预计耗时 |
|------|-----------------|----------|---------|
| quick | 单维度 skim → 快速出稿 | 3 维度 / 4 次搜索每维度 | ~60秒 |
| normal | 完整 9 角色流水线 | 5 维度 / 6 次搜索每维度 | ~4-8分钟 |
| heavy | normal + 多轮 review + 深度核查 | 7 维度 / 8 次搜索每维度 | ~8-15分钟 |

> 真实联网调研耗时取决于网络状况与目标公司信息密度，上述为典型值。

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| POST | `/api/research` | 启动研究任务 |
| GET | `/api/research/<id>/stream` | SSE 实时流 |
| GET | `/api/research/<id>/result` | 获取研究结果（轮询用） |
| GET | `/api/tasks` | 任务列表（调试用） |
| GET | `/api/models` | 可用模型列表 |

### 启动研究

```bash
curl -X POST https://your-domain.vercel.app/api/research \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "商汤科技",
    "depth": "normal",
    "api_key": "your-sensenova-api-key",
    "agent_mode": "sn-deepresearch",
    "model": "sensenova-6.7-flash-lite"
  }'
```

**参数说明**：

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `company_name` | string | 公司名称 | 必填 |
| `depth` | string | 研究深度：`quick` / `normal` / `heavy` | `normal` |
| `api_key` | string | LLM API Key（或环境变量 `SENSENOVA_API_KEY`） | 必填 |
| `agent_mode` | string | 流水线：`sn-deepresearch` / `cr-agent` | `sn-deepresearch` |
| `model` | string | 模型名称 | `sensenova-6.7-flash-lite` |

### 兼容任意 LLM 端点

本项目兼容所有 OpenAI Chat Completions 协议端点。修改环境变量即可切换：

| 平台 | `SENSENOVA_BASE_URL` | `SENSENOVA_MODEL` 示例 |
|------|----------------------|------------------------|
| SenseNova | `https://token.sensenova.cn/v1` | `sensenova-6.7-flash-lite` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |

### SSE 事件类型

| 事件 | 说明 |
|------|------|
| `task_start` | 任务启动（含报告目录） |
| `phase_change` | 阶段切换（scout / plan / research / writer / reviewer / render） |
| `agent_start` | Agent 开始（含 input_summary） |
| `tool_call` | 工具调用记录（含 args + result） |
| `scout_done` / `plan_done` / `research_all_done` | 阶段完成 |
| `validator_done` | validator 校验结果 |
| `complete` | 任务完成（含完整报告） |
| `error` | 错误信息 |
| `ping` | 心跳保活（每 5 秒） |

---

## 技术栈

- **后端**：Python 3 + Flask + OpenAI SDK（兼容协议）
- **前端**：原生 HTML/CSS/JS（单文件，零构建）
- **AI 模型**：任意 OpenAI 协议端点（默认 SenseNova）
- **部署**：Vercel 零配置 Flask 部署（`@vercel/python`）
- **通信**：SSE (Server-Sent Events) + 轮询降级
- **联网**：Bing HTML 直抓 + html2text 转纯文本（零外部 API 依赖）

---

## 开发经验沉淀

[AGENT_DEV_GUIDE.md](AGENT_DEV_GUIDE.md) 沉淀了本项目开发过程中的全部经验，包含：

- LLM API 任意配置的核心模式（动态 `configure()`）
- function-calling 循环的 4 个关键坑（预算、收敛、JSON 截断、思考模式）
- Vercel 部署 5 大坑点（只读文件系统、执行时间限制、内存存储、PAT 安全等）
- 坑点速查表（11 个高频坑的根因+对策）
- **迁移示例**：如何用本架构做小红书/抖音爆款文案 Agent

---

## License

MIT
