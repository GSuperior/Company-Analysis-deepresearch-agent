# DeepResearch Agent · 多智能体公司深度研究系统

基于 SenseNova API + Flask 的多 Agent 企业研究工具，可一键部署到 Vercel。

## 功能

- **三 Agent 协同**：规划师拆解维度 → 调研员逐维取证（调用 web_search）→ 写作师整合出报告
- **三级研究深度**：基础（2 维度）/ 标准（4 维度）/ 深度（6 维度）
- **流程可视化**：实时展示每个 Agent 的输入、输出、工具调用、完整日志
- **SSE 实时流**：研究过程实时推送，断线自动重连，失败降级为轮询
- **零配置部署**：API Key 前端输入，服务端不存储

## 快速开始

### 本地运行

```bash
pip install -r requirements.txt
python api/index.py
# 打开 http://localhost:5000
```

### 部署到 Vercel

1. Fork 本仓库到你的 GitHub
2. 打开 [vercel.com](https://vercel.com) → New Project → 选择仓库
3. Framework Preset 选 **Other**，其他默认
4. 点击 Deploy，1-2 分钟完成
5. 打开分配的域名，输入 SenseNova API Key 即可使用

## 项目结构

```
├── api/
│   ├── index.py          # Flask 入口（Vercel Serverless Function）
│   ├── agent_system.py   # Agent 系统核心
│   └── requirements.txt  # API 依赖
├── public/
│   └── index.html        # 前端页面（单文件）
├── vercel.json           # Vercel 配置
├── requirements.txt      # 根级依赖
└── README.md
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/research` | 启动研究 |
| GET | `/api/research/<id>/stream` | SSE 实时流 |
| GET | `/api/research/<id>/result` | 获取结果 |
| GET | `/api/health` | 健康检查 |

## Vercel 部署说明

**执行时间限制：**
- Hobby（免费）：10 秒 → SSE 会频繁断开，前端自动重连 + 轮询降级
- Pro：300 秒 → 可完成基础/标准深度研究
- Enterprise：900 秒 → 支持深度研究

**无状态限制：**
- 任务状态存在内存中，实例切换/冷启动可能丢失
- 生产环境建议用传统服务器 + Redis 存储

## 技术栈

Python 3 · Flask · SenseNova API · 原生 HTML/CSS/JS · Vercel

## License

MIT
