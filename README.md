# DeepResearch Agent v2 · 双架构企业深度研究系统

基于 SenseNova API 的多 Agent 企业深度研究工具，包含两套 Agent 架构（经典版 + SN深度研究版），真实调用大模型 + function calling，完整展示 Agent 协作流程，零配置部署到 Vercel。

## 功能特性

### 双 Agent 架构

- **经典版（5智能体）**：Planner → Researcher (×N) → Writer → Reviewer ↔ FactChecker
- **SN深度研究版（7智能体）**：Scout → Planner → Researcher → Reviewer → ReportPlanner → ReportWriter → FactChecker
- 前端一键切换，两套架构独立运行

### 核心能力

- **模型选择**：支持切换 `sensenova-6.7-flash-lite` / `sensenova-6.7`
- **点击Agent查看详情**：点击任意Agent节点，查看其输入/输出/工具调用/原始日志
- **合理的工具配置**：每个 Agent 的工具配置与其角色匹配
- **动态研究维度**：根据公司特点生成 3-7 个研究维度
- **质量审核闭环**：初稿 → 审核 → 修改 → 终稿（支持多轮迭代）
- **事实核查机制**：自动提取关键数据点，搜索验证并标注可信度
- **SSE 实时流**：研究过程实时推送到前端
- **流程可视化**：实时展示每个 Agent 的状态、输入输出、工具调用
- **优雅降级**：每个环节都有 fallback 机制，确保流程不中断
- **总超时保护**：10分钟总超时，防止任务无限运行

## 快速开始

### 本地运行

```bash
pip install -r requirements.txt
python app.py
# 打开 http://localhost:5000/index.html
```

### 部署到 Vercel

**零配置部署**：直接将代码推送到 GitHub，在 Vercel 中 Import 仓库即可。

Vercel 会自动识别 Flask 应用，无需额外配置。

```
项目结构：
├── app.py              # Flask 入口（Vercel 自动检测）
├── requirements.txt    # Python 依赖
├── public/
│   └── index.html      # 前端页面（静态资源）
├── agent_system.py     # 经典版 Agent 系统
├── sn_deepresearch.py  # SN深度研究版 Agent 系统
├── tools.py            # 工具定义
└── vercel.json         # 函数超时配置（可选）
```

## Agent 架构对比

### 经典版（5智能体）

| Agent | 角色 | 工具 | 配置理由 |
|-------|------|------|----------|
| **Planner** | 研究总监 | `web_search` | 需要先快速了解公司，才能制定合理的研究计划 |
| **Researcher** | 信息检索专家 | `web_search`, `company_lookup`, `financial_data` | 调研是核心环节，需要多种信息获取工具 |
| **Writer** | 资深行业研究员 | 无 | 写作是整合工作，信息来自调研结果 |
| **Reviewer** | 质量审核专家 | 无 | 审核逻辑/结构/可读性，数据准确性由 FactChecker 专门负责 |
| **FactChecker** | 事实核查员 | `web_search` | 事实核查必须有外部验证能力 |

**研究流程**：
```
用户输入（公司名 + 深度）
    │
    ▼
[Planner] ← web_search（快速了解公司）
    │ 输出：公司画像 + 研究计划（维度列表）
    ▼
[Researcher] × N ← web_search / company_lookup / financial_data
    │ 输出：各维度调研结果（结构化）
    ▼
[Writer]  ← 初稿
    │
    ▼
[Reviewer] ←→ [Writer]  （多轮审核-修改循环）
    │ 输出：审核意见 + 修改后报告
    ▼
[FactChecker] ← web_search（验证关键数据）
    │ 输出：可信度评估 + 数据验证
    ▼
  最终输出
（摘要 + 指标 + 报告 + 核查结果）
```

**深度差异化**：

| 深度 | 维度数 | Review修改轮数 | FactChecker | 预计耗时 |
|------|--------|---------------|-------------|---------|
| basic | 3 | 0轮（仅审核） | 基础版（规则提取+搜索验证） | ~90秒 |
| standard | 5 | 1轮 | LLM核查+多次搜索验证 | ~3分钟 |
| deep | 7 | 2轮 | 全量核查+交叉验证 | ~5分钟 |

### SN深度研究版（7智能体）

基于 SenseNova Skills 生态的 [sn-deep-research](https://github.com/OpenSenseNova/SenseNova-Skills/tree/main/skills/sn-deep-research) 设计思想实现。

| Agent | 角色 | 工具 | 职责 |
|-------|------|------|------|
| **Scout** | 预研侦察兵 | `web_search` | 快速了解公司，推荐研究档位 |
| **Planner** | 研究规划师 | `web_search` | 维度拆解 + 关键问题设计 |
| **Researcher** | 维度取证专家 | `web_search`, `company_lookup`, `financial_data` | 按维度取证，输出标准化 evidence |
| **Reviewer** | 质量审查员 | 无 | 子报告审查 + 缺口识别 |
| **ReportPlanner** | 报告规划师 | 无 | 大纲编排 + 证据分配 |
| **ReportWriter** | 报告撰写师 | 无 | 基于 outline + evidence 写作 |
| **FactChecker** | 事实核查员 | `web_search` | 关键数据验证 + 可信度评估 |

**核心设计原则**（来自 sn-deep-research）：
- 证据为核：evidence 是唯一真相来源
- 契约驱动：每阶段输出严格 schema 化
- 档位解耦：档位选择器决定跑哪些阶段
- 角色原子化：每个 Agent 职责单一
- 能力降级：缺能力不阻塞，有兜底

**档位模式**：

| 档位 | 说明 | Agent 数量 | 预计耗时 |
|------|------|-----------|---------|
| quick | 单维度 skim → 单 writer → 快速出稿 | 3-4个 | ~60秒 |
| normal | scout → plan → 多维度 research + review → report plan → writer → 终审 | 7个 | ~4分钟 |
| heavy | normal + 多轮 review + 深度 fact check | 7个（更深度） | ~8分钟 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| POST | `/api/research` | 启动研究任务 |
| GET | `/api/research/<id>/stream` | SSE 实时流 |
| GET | `/api/research/<id>/result` | 获取研究结果（轮询用） |
| GET | `/api/tasks` | 任务列表（调试用） |

### 启动研究

```bash
curl -X POST https://your-domain.vercel.app/api/research \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "商汤科技",
    "depth": "basic",
    "api_key": "your-sensenova-api-key",
    "agent_mode": "classic",
    "model": "sensenova-6.7-flash-lite"
  }'
```

**参数说明**：

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `company_name` | string | 公司名称 | 必填 |
| `depth` | string | 研究深度 | classic: `basic`/`standard`/`deep`; sn: `quick`/`normal`/`heavy` |
| `api_key` | string | SenseNova API Key | 必填（或环境变量） |
| `agent_mode` | string | Agent 模式：`classic` / `sn-deepresearch` | `classic` |
| `model` | string | 模型名称 | `sensenova-6.7-flash-lite` |

也可以通过环境变量 `SENSENOVA_API_KEY` 配置 API Key。

### SSE 事件类型

| 事件 | 说明 |
|------|------|
| `progress` | 进度更新（百分比 + 阶段 + 消息） |
| `agent_start` / `agent_end` | Agent 开始/结束（含 input_summary / output_summary） |
| `tool_call` | 工具调用记录 |
| `log` | 步骤日志 |
| `reviewer_end` | 审核完成（评分 + 问题数） |
| `fact_check` | 事实核查结果 |
| `complete` | 任务完成（完整结果） |
| `error` | 错误信息 |
| `ping` | 心跳保活 |

## 技术栈

- **后端**：Python 3 + Flask + SenseNova API
- **前端**：原生 HTML/CSS/JS（单文件，零构建）
- **AI 模型**：商汤 SenseNova（sensenova-6.7-flash-lite / sensenova-6.7）
- **部署**：Vercel 零配置 Flask 部署
- **通信**：SSE (Server-Sent Events) + 轮询降级

## 代码质量

- 所有函数均有 docstring 文档
- 完善的错误处理和降级机制（每个 Agent 都有 fallback）
- 输入验证和超时保护（单次API调用60秒，总流程10分钟）
- 完整的日志记录
- API Key 安全处理（脱敏存储、仅内存传递、日志不泄露）
- 任务数量上限（100个）防止内存无限增长
- JSON 解析四层容错（直接解析→代码块→括号平衡→截断补全）

## License

MIT
