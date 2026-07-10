# DeepResearch Agent · 多智能体公司深度研究系统

基于 SenseNova API 的多 Agent 企业研究工具，真实调用大模型，完整展示 Agent 协作流程，可一键部署到 Vercel。

## 功能

- **5 个 Agent 协同工作**：规划师 → 调研员（×N）→ 写作师 → 审核师 → 事实核查
- **动态维度生成**：根据公司类型（科技/制造/消费/金融/医疗）自动适配研究维度
- **质量审核闭环**：初稿 → 审核 → 修改 → 终稿，确保报告质量
- **事实核查**：自动提取关键数据点，标注可信度
- **三层输出**：执行摘要 + 关键指标 + 完整报告
- **流程可视化**：实时展示每个 Agent 的输入、输出、工具调用、完整日志
- **SSE 实时流**：研究过程实时推送

## Agent 工作原理

### 整体流程

```
用户输入（公司名 + 研究深度）
        │
        ▼
┌─────────────────┐
│  1. Planner      │  研究规划师
│  （规划阶段）     │  · 判断公司类型
└────────┬────────┘  · 生成研究维度
         │ 研究计划（3-7个维度 + 关键问题）
         ▼
┌─────────────────┐
│  2. Researcher   │  信息搜集员（串行执行N个维度）
│  （调研阶段）     │  · 调用 web_search 工具
└────────┬────────┘  · 整理结构化调研结果
         │ 各维度调研结果
         ▼
┌─────────────────┐
│  3. Writer       │  报告撰写师
│  （成稿阶段）     │  · 整合所有维度信息
└────────┬────────┘  · 撰写完整报告初稿
         │ 报告初稿
         ▼
┌─────────────────┐
│  4. Reviewer     │  质量审核师
│  （审核阶段）     │  · 数据准确性 / 逻辑一致性
└────────┬────────┘  · 结构完整性 / 可读性
         │ 审核意见（评分 + 修改建议）
         ▼
┌─────────────────┐
│  5. Writer       │  报告撰写师（修改模式）
│  （修改阶段）     │  · 根据审核意见修改
└────────┬────────┘
         │ 终稿
         ▼
┌─────────────────┐
│  6. Fact Check   │  事实核查
│  （核查阶段）     │  · 提取关键数据点
└────────┬────────┘  · 标注可信度等级
         │
         ▼
    最终输出（摘要 + 指标 + 报告 + 核查结果）
```

### 各 Agent 详细说明

| Agent | 角色 | 输入 | 输出 | 工具 | 对应 SenseNova Skills |
|-------|------|------|------|------|----------------------|
| **Planner** | 研究总监 | 公司名 + 深度 | 公司画像 + 研究计划（维度列表） | 无 | sn-research-planning |
| **Researcher** | 信息检索专家 | 单个维度 + 关键问题 | 调研结果（摘要 + 发现 + 数据点） | web_search | sn-dimension-research |
| **Writer** | 资深行业研究员 | 所有维度调研结果 | 完整 Markdown 报告 | 无 | sn-research-report |
| **Reviewer** | 质量把控专家 | 初稿 + 调研结果 | 评分 + 问题列表 + 修改建议 | 无 | sn-quality-review |
| **Fact Check** | 数据核查员 | 终稿报告 | 关键数据点 + 可信度标注 | 无 | sn-fact-check |

### 动态维度设计

Planner 先判断公司类型，再从对应模板中选取维度：

| 公司类型 | 维度池 |
|---------|--------|
| 科技公司 | 公司概况、核心技术与产品、市场地位与竞争、商业模式与营收、财务表现、研发与创新、团队与人才 |
| 制造企业 | 公司概况、核心产品与产能、供应链与成本、市场份额与竞争、财务表现、技术与工艺、ESG与合规 |
| 消费品牌 | 公司概况、品牌力与渠道、产品矩阵与创新、用户与市场、财务表现、供应链与质量、营销与增长 |
| 金融机构 | 公司概况、业务结构与收入、资产质量与风险、监管与合规、财务表现、科技与数字化、团队与治理 |
| 医疗健康 | 公司概况、核心产品与管线、研发与临床、市场与商业化、财务表现、监管与合规、团队与人才 |

**深度对应维度数：**
- basic：3 个核心维度
- standard：5 个维度
- deep：7 个维度

### 质量审核闭环

Reviewer 从四个维度审核报告：

1. **数据准确性** — 关键数据是否有来源支撑，数字是否合理
2. **逻辑一致性** — 前后论述是否矛盾，因果是否成立
3. **结构完整性** — 章节是否齐全，重点是否突出
4. **可读性** — 语言是否通顺，表达是否清晰

输出：总分（0-100）+ 各维度评分 + 具体问题列表（含严重程度和修改建议）。

standard/deep 深度会让 Writer 根据审核意见修改一轮；basic 深度只审核不修改。

### 事实核查

自动提取报告中的关键数据点（营收、利润、增长率、市场份额、员工数、专利数等），标注可信度：

- **高可信度**：有多个来源交叉验证，来源权威
- **中可信度**：有单一来源支撑，或数据为估算值
- **低可信度**：无明确来源，数据存疑

### 工具调用

Researcher 使用 SenseNova 的 function calling 能力调用 `web_search` 工具：

1. Agent 判断需要搜索的关键词
2. 调用 `web_search(query=...)`
3. 获取搜索结果
4. 整理成结构化调研结论
5. 必要时进行多轮搜索（同一维度多次搜索补充信息）

## 快速开始

### 本地运行

```bash
pip install -r requirements.txt
python index.py
# 打开 http://localhost:5000
```

### 部署到 Vercel

1. 代码 push 到 GitHub 仓库
2. 打开 [vercel.com](https://vercel.com) → New Project → 选择仓库
3. Framework Preset 选 **Other**，其他默认
4. 点击 Deploy，1-2 分钟完成
5. 打开分配的域名，输入 SenseNova API Key 即可使用

**Vercel 部署注意事项：**
- Hobby（免费）：函数执行时间 10 秒，SSE 会频繁断开，前端自动重连 + 轮询降级
- Pro：300 秒，可完成 basic/standard 深度研究
- Serverless 是无状态的，任务存在内存中，实例切换可能丢失

## 项目结构

```
├── index.py              # Flask 入口（Vercel WSGI 应用）
├── agent_system.py       # Agent 系统核心（5 个 Agent + 编排器）
├── public/
│   └── index.html        # 前端页面（单文件）
├── api/                  # 保留目录（兼容备用）
│   ├── index.py
│   ├── agent_system.py
│   └── requirements.txt
├── vercel.json           # Vercel 部署配置
├── requirements.txt      # Python 依赖
└── README.md
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/research` | 启动研究 |
| GET | `/api/research/<id>/stream` | SSE 实时流 |
| GET | `/api/research/<id>/result` | 获取结果 |
| GET | `/api/health` | 健康检查 |

### 启动研究

```bash
curl -X POST https://your-domain.vercel.app/api/research \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "商汤科技",
    "depth": "basic",
    "api_key": "your-api-key"
  }'
```

### SSE 事件类型

| 事件 | 说明 |
|------|------|
| `progress` | 进度更新（百分比 + 阶段 + 消息） |
| `agent_start` / `agent_end` | Agent 开始/结束 |
| `tool_call` | 工具调用（名称 + 参数 + 结果） |
| `log` | 步骤日志 |
| `reviewer_end` | 审核完成（评分 + 问题数） |
| `fact_check` | 事实核查结果 |
| `complete` | 任务完成（报告 + 摘要 + 指标 + 审核结果） |
| `error` | 错误信息 |

## 技术栈

- **后端**：Python 3 + Flask + SenseNova API
- **前端**：原生 HTML/CSS/JS（单文件，无构建）
- **AI 模型**：商汤 SenseNova（sensenova-6.7-flash-lite）
- **部署**：Vercel Serverless Functions

## License

MIT
